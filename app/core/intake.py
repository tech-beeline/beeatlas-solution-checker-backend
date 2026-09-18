# Copyright (c) 2024 PJSC VimpelCom
"""
Бизнес-логика этапа Intake: импорт, чанкинг, структуризация через LLM,
объединение и дедупликация результатов.

Все функции stateless — не хранят состояние между вызовами.
"""

import json
import logging
from typing import Optional

from app.integrations.llm_client import llm_client
from app.integrations.confluence import confluence_client

logger = logging.getLogger("hld-agent")


def _load_prompt(name: str) -> str:
    """Загрузка системного промпта из файла."""
    try:
        with open(f"app/prompts/{name}.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("Prompt file not found: app/prompts/%s.txt", name)
        return ""


async def import_requirements(
    text: Optional[str] = None,
    confluence_url: Optional[str] = None,
    include_child_pages: bool = False,
    confluence_pat: Optional[str] = None,
) -> dict:
    """
    Импорт требований из текста или Confluence.
    Возвращает словарь с raw_text, source, title.
    """
    if confluence_url:
        logger.info("Importing from Confluence: %s", confluence_url)
        raw_text = await confluence_client.get_page_content(
            confluence_url, include_child_pages, pat=confluence_pat
        )
        source = "confluence"
        # Извлекаем заголовок страницы из Confluence
        try:
            page_title = await confluence_client.get_page_title(
                confluence_url, pat=confluence_pat
            )

            
            title = (
                page_title.strip()
                if page_title.strip()
                else f"Confluence: {confluence_url.split('/')[-1] or confluence_url}"
            )
        except Exception:
            title = f"Confluence: {confluence_url.split('/')[-1] or confluence_url}"

    elif text:
        raw_text = text
        source = "text"
        title = text.split("\n")[0][:60] or "Новая сессия"
    else:
        raise ValueError("Не указан источник требований: text или confluence_url")

    logger.info(
        "Import complete: source=%s | text_len=%d | title=%s",
        source,
        len(raw_text),
        title,
    )

    return {
        "raw_text": raw_text,
        "source": source,
        "title": title,
        "source_url": confluence_url,
    }


async def structure_requirements(
    raw_text: str,
    progress_callback=None,
) -> list[dict]:
    """
    Структурирование требований через LLM:
    1. Чанкинг текста по границам абзацев
    2. LLM-структуризация каждого чанка
    3. Объединение и дедупликация результатов через LLM

    progress_callback: опциональная async-функция для отправки прогресса.
    Принимает dict с полями: phase, current_chunk, total_chunks, chars_sent,
    last_response_chars, chunk_size, attempts, merge_attempts.

    Возвращает список structured_requirements.
    """
    if not raw_text:
        raise ValueError("Нет текста для структурирования")

    # Шаг 1: Чанкинг
    chunks = _chunk_text(raw_text)
    total_chunks = len(chunks)
    total_chars = len(raw_text)
    logger.info("Chunking: %d chunks from %d chars", total_chunks, total_chars)

    if progress_callback:
        await progress_callback({
            "phase": "chunking",
            "current_chunk": 0,
            "total_chunks": total_chunks,
            "chars_sent": 0,
            "last_response_chars": 0,
        })

    # Шаг 2: Загрузка промптов
    structure_prompt = _load_prompt("structure_chunk")
    merge_prompt = _load_prompt("merge_dedup")

    if not structure_prompt or not merge_prompt:
        raise RuntimeError("Системные промпты не найдены в app/prompts/")

    # Шаг 3: Структуризация каждого чанка через LLM
    chunk_results = []
    total_chars_sent = 0
    total_attempts = 0
    for i, chunk in enumerate(chunks):
        chunk_num = i + 1
        chunk_chars = len(chunk)
        logger.info(
            "Structuring chunk %d/%d (%d chars)", chunk_num, total_chunks, chunk_chars
        )

        if progress_callback:
            await progress_callback({
                "phase": "processing",
                "current_chunk": chunk_num,
                "total_chunks": total_chunks,
                "chars_sent": total_chars_sent,
                "last_response_chars": 0,
                "chunk_size": chunk_chars,
                "attempts": total_attempts,
            })

        result, attempts, _was_rate_limited = await llm_client.chat_completion_with_stats(
            system_prompt=structure_prompt,
            user_message=chunk,
            prompt_name="structure_chunk",
        )
        total_attempts += attempts
        chunk_results.append(result)
        total_chars_sent += chunk_chars

        if progress_callback:
            await progress_callback({
                "phase": "processing",
                "current_chunk": chunk_num,
                "total_chunks": total_chunks,
                "chars_sent": total_chars_sent,
                "last_response_chars": len(result),
                "chunk_size": chunk_chars,
                "attempts": total_attempts,
            })

    # Шаг 4: Объединение и дедупликация через LLM
    logger.info(
        "Merging and deduplicating %d chunk results...", len(chunk_results)
    )

    # Пустые ответы чанков (модель не вернула content) не отдаём в merge — они только
    # зашумляют вход и увеличивают ожидаемый выход модели.
    merge_sources = [r for r in chunk_results if r.strip()]
    if len(merge_sources) < len(chunk_results):
        logger.warning(
            "Чанков без валидного ответа: %d из %d — исключены из merge-запроса",
            len(chunk_results) - len(merge_sources),
            len(chunk_results),
        )

    merge_input = json.dumps(merge_sources, ensure_ascii=False, indent=2)
    merge_input_chars = len(merge_input)

    if progress_callback:
        await progress_callback({
            "phase": "merging",
            "current_chunk": total_chunks,
            "total_chunks": total_chunks,
            "chars_sent": total_chars_sent,
            "last_response_chars": 0,
            "chunk_size": 0,
            "attempts": total_attempts,
            "merge_input_chars": merge_input_chars,
        })

    merged, merge_attempts, _was_rate_limited = await llm_client.chat_completion_with_stats(
        system_prompt=merge_prompt,
        user_message=merge_input,
        prompt_name="merge_dedup",
    )
    total_attempts += merge_attempts

    # Шаг 5: Парсинг результата
    requirements = _parse_llm_result(merged)

    # Защита от "схлопывания" результата: merge может вернуть пустой ответ (например,
    # модель истратила весь бюджет вывода на reasoning) — тогда не теряем уже
    # структурированные требования чанков, а собираем их напрямую (дедуп по type+title).
    if not requirements and merge_sources:
        logger.warning(
            "merge_dedup вернул пустой/непарсируемый результат "
            "(merge_input=%d chars, merge_response=%d chars) — fallback на требования из чанков",
            merge_input_chars,
            len(merged),
        )
        requirements = _collect_chunk_requirements(merge_sources)

    # Шаг 6: Присваиваем ID требованиям (FR-1, FR-2, ..., NFR-1, ..., OQ-1, ...)
    requirements = _assign_ids(requirements)

    if progress_callback:
        await progress_callback({
            "phase": "done",
            "current_chunk": total_chunks,
            "total_chunks": total_chunks,
            "chars_sent": total_chars_sent,
            "last_response_chars": len(merged),
            "chunk_size": 0,
            "attempts": total_attempts,
            "merge_input_chars": merge_input_chars,
            "merge_attempts": merge_attempts,
        })

    logger.info(
        "Structuring complete: %d requirements (FR=%d, NFR=%d, OQ=%d)",
        len(requirements),
        sum(1 for r in requirements if r.get("type") == "FR"),
        sum(1 for r in requirements if r.get("type") == "NFR"),
        sum(1 for r in requirements if r.get("type") == "OQ"),
    )

    return requirements


def _chunk_text(
    text: str, max_chunk_size: int = 3000, overlap: int = 200
) -> list[str]:
    """
    Разбивка текста на чанки по границам абзацев.
    - max_chunk_size: максимальный размер чанка в символах
    - overlap: перекрытие между чанками в символах
    """
    paragraphs = text.split("\n\n")
    chunks = []
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if (
            len(current_chunk) + len(para) + 2 > max_chunk_size
            and current_chunk
        ):
            chunks.append(current_chunk.strip())
            overlap_text = (
                current_chunk[-overlap:]
                if len(current_chunk) > overlap
                else current_chunk
            )
            current_chunk = overlap_text + "\n\n" + para
        else:
            if current_chunk:
                current_chunk += "\n\n" + para
            else:
                current_chunk = para

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


def _parse_llm_result(result: str) -> list[dict]:
    """
    Парсинг JSON-ответа от LLM.
    Универсальный алгоритм:
    1. Находит первую [ и последнюю ] — основной кандидат
    2. Если последняя ] отсутствует (ответ обрезан по max_tokens) — находит
       последний завершённый объект и закрывает массив вручную
    3. Пробует распарсить через json.loads
    4. Если не получилось — пробует найти вложенные массивы через regex
    5. Если ничего не подошло — логирует для диагностики
    """
    raw = result.strip()

    first_bracket = raw.find("[")
    last_bracket = raw.rfind("]")

    candidates = []

    if first_bracket != -1:
        if last_bracket != -1 and last_bracket > first_bracket:
            # Нормальный случай: есть и [ и ]
            candidates.append(raw[first_bracket : last_bracket + 1])
        else:
            # Ответ обрезан — нет закрывающей ]
            # Пробуем восстановить JSON, найдя последний завершённый объект
            truncated = raw[first_bracket:]

            # Стратегия 1: добавить закрывающие скобки как есть
            candidates.append(truncated + "]")
            candidates.append(truncated.strip().rstrip(",") + "]")

            # Стратегия 2: найти последнюю полную пару } и закрыть массив после неё
            # Ищем последнее вхождение '}', перед которым есть '{'
            last_close = truncated.rfind("}")
            if last_close != -1:
                # Берём всё от [ до последнего } включительно
                recovered = truncated[: last_close + 1]
                # Убедимся, что перед } есть открывающая {
                if "{" in recovered:
                    candidates.append(recovered + "]")
                    # Также пробуем без висящей запятой перед последним объектом
                    candidates.append(recovered.rstrip().rstrip(",") + "]")

            # Стратегия 3: найти последний полный объект по границе },
            # отбросив незавершённый последний объект
            last_complete = truncated.rfind("},")
            if last_complete != -1:
                recovered = truncated[: last_complete + 1]
                candidates.append(recovered + "]")

        # Дополнительно: найти все внутренние JSON-массивы через regex
        import re
        for match in re.finditer(r"\[[^\[\]]*\]", raw):
            candidate = match.group(0)
            if candidate not in candidates:
                candidates.append(candidate)

    if not candidates:
        logger.warning(
            "No JSON array found in LLM response.\n"
            "First 500 chars: %s\nLast 500 chars: %s",
            raw[:500],
            raw[-500:],
        )
        return []

    # Пробуем каждый кандидат (сначала самые длинные — они наиболее полные)
    candidates.sort(key=len, reverse=True)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                logger.info(
                    "Parsed %d requirements from LLM response (candidate len=%d)",
                    len(data),
                    len(candidate),
                )
                return data
        except json.JSONDecodeError:
            continue

    # Если ничего не подошло — логируем для диагностики
    logger.warning(
        "Failed to parse any JSON candidate from LLM response.\n"
        "Found %d candidates (longest=%d chars).\n"
        "First 500 chars: %s\nLast 500 chars: %s",
        len(candidates),
        len(candidates[0]) if candidates else 0,
        raw[:500],
        raw[-500:],
    )
    return []


def _collect_chunk_requirements(chunk_results: list[str]) -> list[dict]:
    """Сборка требований из ответов чанков без LLM (fallback, если merge не удался).

    Штатно дедупликацию делает merge_dedup; здесь нужна простая защита от потери
    данных: склеиваем разобранные чанки и убираем дубликаты по (type, title)
    без учёта регистра. ID присваиваются позже в _assign_ids.
    """
    collected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    unparsed_chunks = 0

    for chunk_result in chunk_results:
        chunk_items = _parse_llm_result(chunk_result)
        if not chunk_items:
            unparsed_chunks += 1
            continue
        for item in chunk_items:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("type", "")).strip().upper(),
                str(item.get("title", "")).strip().casefold(),
            )
            if key in seen:
                continue
            seen.add(key)
            collected.append(item)

    if unparsed_chunks:
        logger.warning(
            "Fallback-сборка: %d чанк(ов) без парсибельного результата", unparsed_chunks
        )
    logger.info(
        "Fallback-сборка из чанков: %d требований из %d чанков",
        len(collected),
        len(chunk_results),
    )
    return collected


def _assign_ids(requirements: list[dict]) -> list[dict]:
    """
    Присваивает ID требованиям по типам:
    FR-1, FR-2, ... для FR
    NFR-1, NFR-2, ... для NFR
    OQ-1, OQ-2, ... для OQ

    Если ID уже есть (от LLM) — оставляет как есть.
    """
    counters: dict[str, int] = {}
    result = []
    for req in requirements:
        req_type = req.get("type", "")
        if req.get("id"):
            # ID уже есть — оставляем
            result.append(req)
            continue
        counters.setdefault(req_type, 0)
        counters[req_type] += 1
        req["id"] = f"{req_type}-{counters[req_type]}"
        result.append(req)
    return result

