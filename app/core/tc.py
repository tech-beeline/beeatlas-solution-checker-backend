# Copyright (c) 2024 PJSC VimpelCom
"""
Бизнес-логика этапа Technical Capability:
1. Выявление TC из FR через LLM
2. Поиск TC на ландшафте (fdm-search / search_capability_v2)
3. Управление кандидатами TC (reuse/create_new)

Все функции stateless — не хранят состояние между вызовами.
"""

import json
import logging
import time
from typing import Optional

from app.config import settings
from app.integrations.llm_client import llm_client
from app.integrations.beeatlas import search_capability_v2

logger = logging.getLogger("hld-agent")

# Максимальная длина всего поискового запроса (имя TC + rationale + описание) для fdm-search:
# длинные запросы дают пустую выдачу, а длинный URL — HTTP 413 на шлюзе.
_MAX_SEARCH_CONTEXT_CHARS = 200

# Слой 3 (компактный merge): для дедупликации кандидатов из разных чанков в
# merge-запрос уходят УРЕЗАННЫЕ description/rationale. Полные тексты в ответ
# модели не нужны (название + fr_ids достаточно для склейки дубликатов), а чем
# больше merge-ответ — тем выше вероятность упереться в лимит вывода модели
# (completion cap ~8192 токенов) и получить обрезанный JSON. После merge полные
# description/rationale возвращаются по совпадению name (см. _rehydrate_merge_result).
_TC_MERGE_DESCRIPTION_CAP = 240
_TC_MERGE_RATIONALE_CAP = 180


def _load_prompt(name: str) -> str:
    """Загрузка системного промпта из файла."""
    try:
        with open(f"app/prompts/{name}.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("Prompt file not found: app/prompts/%s.txt", name)
        return ""


async def generate_task_description(
    raw_content: str,
    progress_callback=None,
) -> str:
    """
    Генерация краткого описания задачи из исходного текста требований.

    Использует промпт summarize_task.txt для создания одного абзаца,
    описывающего общую цель и контекст задачи.
    """
    logger.info("Generating task description from %d chars of raw content", len(raw_content))

    prompt = _load_prompt("summarize_task")
    if not prompt:
        raise RuntimeError("Системный промпт summarize_task не найден в app/prompts/")

    # Подставляем полный текст (без обрезки); защита контекста — клампинг
    # max_tokens под окно модели в llm_client._max_tokens_for_request.
    prompt = prompt.replace("{input_text}", raw_content)

    if progress_callback:
        await progress_callback({
            "phase": "summarizing",
            "total_fr": 0,
            "total_candidates": 0,
            "data_chars": len(raw_content),
            "response_chars": 0,
        })

    result, attempts, _was_rate_limited = await llm_client.chat_completion_with_stats(
        system_prompt=prompt,
        user_message="Сгенерируй описание задачи на основе предоставленного текста.",
        prompt_name="summarize_task",
    )

    description = result.strip()
    logger.info(
        "Task description generated: %d chars (attempts=%d)",
        len(description),
        attempts,
    )

    if progress_callback:
        await progress_callback({
            "phase": "summarizing_done",
            "total_fr": 0,
            "total_candidates": 0,
            "data_chars": len(raw_content),
            "response_chars": len(description),
            "attempts": attempts,
        })

    return description


def _compact_for_merge(candidates: list[dict]) -> list[dict]:
    """Слой 3: сжимает кандидатов перед merge-запросом.

    Дедупликация опирается на name + fr_ids (описание вторично), поэтому в
    merge-запрос уходят урезанные description/rationale. Это уменьшает и вход,
    и — главное — ожидаемый объём ответа модели: чем больше merge-ответ, тем
    выше вероятность упереться в completion cap (~8192 токенов) и получить
    обрезанный JSON.
    """
    compact = []
    for c in candidates:
        description = (c.get("description") or "")[:_TC_MERGE_DESCRIPTION_CAP]
        rationale = (c.get("rationale") or "")[:_TC_MERGE_RATIONALE_CAP]
        compact.append({
            "name": c.get("name", ""),
            "fr_ids": c.get("fr_ids", []),
            "score": c.get("score", 0),
            "description": description,
            "rationale": rationale,
        })
    return compact


def _rehydrate_merge_result(
    merged: list[dict],
    all_candidates: list[dict],
) -> list[dict]:
    """Слой 3: возвращает полные description/rationale в итог merge.

    Merge-ответ собирался из урезанных кандидатов, поэтому после успешного
    парсинга подставляем обратно полные тексты из чанковых кандидатов,
    сопоставляя по name (merge_tc сохраняет name как есть). Совпадения по имени
    нет — оставляем текст, который вернула модель (не теряем результат).
    """
    by_name: dict[str, dict] = {}
    for cand in all_candidates:
        name = (cand.get("name") or "").strip()
        if name and (name not in by_name or len(cand.get("description") or "") >
                     len(by_name[name].get("description") or "")):
            by_name[name] = cand

    restored = 0
    for item in merged:
        name = (item.get("name") or "").strip()
        source = by_name.get(name)
        if source is None:
            continue
        source_description = source.get("description") or ""
        source_rationale = source.get("rationale") or ""
        if source_description and len(source_description) > len(item.get("description") or ""):
            item["description"] = source_description
        if source_rationale and len(source_rationale) > len(item.get("rationale") or ""):
            item["rationale"] = source_rationale
        restored += 1

    if restored:
        logger.info(
            "Merge: restored full description/rationale for %d/%d merged TC",
            restored,
            len(merged),
        )
    return merged


async def identify_tc(
    structured_requirements: list[dict],
    task_description: str = "",
    business_capabilities: Optional[list[dict]] = None,
    progress_callback=None,
) -> list[dict]:
    """
    Выявление Technical Capability из FR через LLM.

    Если FR больше TC_IDENTIFY_CHUNK_SIZE, FR разбиваются на чанки и выявление
    выполняется последовательными запросами по каждому чанку, затем кандидаты
    объединяются и дедуплицируются финальным запросом (промпт merge_tc) —
    по аналогии со структурированием требований.

    1. Собирает все FR из structured_requirements
    2. Отправляет их в LLM с промптом identify_tc (с опциональным task_description
       и бизнес-возможностями business_capabilities как дополнительным контекстом)
    3. Парсит результат и возвращает список кандидатов TC

    progress_callback: опциональная async-функция для отправки прогресса.
    Принимает dict с полями: phase, total_fr, total_candidates, data_chars,
    response_chars, attempts, elapsed_ms; в чанкованном режиме дополнительно
    current_chunk, total_chunks, chunk_size, merge_input_chars, merge_attempts.

    business_capabilities: список выбранных бизнес-возможностей
      [{"code": "...", "description": "...", ...}, ...]. Добавляется в промпт
      как дополнительный контекст. Если None или пусто — не влияет на промпт.

    Возвращает список TC-кандидатов:
      [{"name": "...", "description": "...", "rationale": "...", "score": 85, "fr_ids": ["FR-001", ...]}, ...]
    """
    fr_list = [r for r in structured_requirements if r.get("type") == "FR"]
    if not fr_list:
        raise ValueError(
            "Нет FR для выявления TC. Выполните структурирование требований."
        )

    logger.info("Identifying TC from %d FR (task_description=%d chars)", len(fr_list), len(task_description))

    start_time = time.time()
    total_fr = len(fr_list)

    # Загрузка промпта
    identify_prompt = _load_prompt("identify_tc")
    if not identify_prompt:
        raise RuntimeError("Системный промпт identify_tc не найден в app/prompts/")

    chunk_size = settings.TC_IDENTIFY_CHUNK_SIZE
    if chunk_size <= 0:
        chunk_size = total_fr

    # Одиночный запрос, если FR немного
    if total_fr <= chunk_size:
        if progress_callback:
            await progress_callback({
                "phase": "sending",
                "total_fr": total_fr,
                "total_candidates": 0,
                "data_chars": 0,
                "response_chars": 0,
            })

        fr_json = json.dumps(fr_list, ensure_ascii=False, indent=2)
        result, attempts, _was_rate_limited = await llm_client.chat_completion_with_stats(
            system_prompt=identify_prompt,
            user_message=fr_json,
            prompt_name="identify_tc",
        )
        candidates = _parse_tc_result(result)
        llm_elapsed_ms = int((time.time() - start_time) * 1000)

        if progress_callback:
            await progress_callback({
                "phase": "done",
                "total_fr": total_fr,
                "total_candidates": len(candidates),
                "data_chars": len(fr_json),
                "response_chars": len(result),
                "attempts": attempts,
                "elapsed_ms": llm_elapsed_ms,
                "current_chunk": 1,
                "total_chunks": 1,
            })

        logger.info(
            "TC identification complete: %d FR processed, %d TC candidates in %dms (attempts=%d)",
            total_fr, len(candidates), llm_elapsed_ms, attempts,
        )
        return candidates

    # --- Чанкованный режим: последовательные запросы по чанкам + дедупликация ---
    merge_prompt = _load_prompt("merge_tc")
    if not merge_prompt:
        raise RuntimeError("Системный промпт merge_tc не найден в app/prompts/")

    chunks = _chunk_fr_list(fr_list, chunk_size)
    total_chunks = len(chunks)
    logger.info(
        "Identify TC (chunked): %d FR -> %d chunks of %d",
        total_fr, total_chunks, chunk_size,
    )

    all_candidates: list[dict] = []
    total_attempts = 0
    total_data_chars = 0

    for i, chunk in enumerate(chunks):
        chunk_num = i + 1
        chunk_json = json.dumps(chunk, ensure_ascii=False, indent=2)
        total_data_chars += len(chunk_json)

        if progress_callback:
            await progress_callback({
                "phase": "processing",
                "current_chunk": chunk_num,
                "total_chunks": total_chunks,
                "total_fr": total_fr,
                "total_candidates": len(all_candidates),
                "data_chars": total_data_chars,
                "response_chars": 0,
                "chunk_size": len(chunk),
                "attempts": total_attempts,
            })

        result, attempts, _was_rate_limited = await llm_client.chat_completion_with_stats(
            system_prompt=identify_prompt,
            user_message=chunk_json,
            prompt_name="identify_tc",
        )
        total_attempts += attempts
        chunk_candidates = _parse_tc_result(result)
        all_candidates.extend(chunk_candidates)

        if progress_callback:
            await progress_callback({
                "phase": "processing",
                "current_chunk": chunk_num,
                "total_chunks": total_chunks,
                "total_fr": total_fr,
                "total_candidates": len(all_candidates),
                "data_chars": total_data_chars,
                "response_chars": len(result),
                "chunk_size": len(chunk),
                "attempts": total_attempts,
            })

    # Дедупликация и объединение кандидатов из всех чанков.
    # Слой 3: в merge уходят компактные кандидаты (урезанные description/rationale),
    # чтобы ответ модели не упирался в completion cap (~8192 токенов) и не приходил
    # обрезанным. Полные тексты потом возвращаются в _rehydrate_merge_result.
    merge_compact = _compact_for_merge(all_candidates)
    merge_input = json.dumps(merge_compact, ensure_ascii=False, indent=2)

    if progress_callback:
        await progress_callback({
            "phase": "merging",
            "current_chunk": total_chunks,
            "total_chunks": total_chunks,
            "total_fr": total_fr,
            "total_candidates": len(all_candidates),
            "data_chars": total_data_chars,
            "response_chars": 0,
            "attempts": total_attempts,
            "merge_input_chars": len(merge_input),
        })

    merged, merge_attempts, _was_rate_limited = await llm_client.chat_completion_with_stats(
        system_prompt=merge_prompt,
        user_message=merge_input,
        prompt_name="merge_tc",
    )
    total_attempts += merge_attempts

    candidates = _parse_tc_result(merged)

    # Слой 3: возвращаем полные description/rationale (merge получал урезанные).
    candidates = _rehydrate_merge_result(candidates, all_candidates)

    # Слой защиты от "схлопывания" результата: большой merge-запрос может упереться
    # в лимит вывода модели (completion cap ~8192 токенов), JSON-массив приходит
    # обрезанным, и _parse_tc_result возвращает пустой список. В этом случае НЕ теряем
    # уже накопленные кандидаты из чанков — возвращаем их (до дедупликации), чтобы
    # пользователь не получил "0 TC" при том, что TC реально были выявлены.
    if not candidates and all_candidates:
        logger.warning(
            "merge_tc вернул пустой/непарсируемый результат (merge_input=%d chars, "
            "merge_response=%d chars) — fallback на %d накопленных кандидатов из чанков",
            len(merge_input), len(merged), len(all_candidates),
        )
        candidates = all_candidates

    llm_elapsed_ms = int((time.time() - start_time) * 1000)

    if progress_callback:
        await progress_callback({
            "phase": "done",
            "current_chunk": total_chunks,
            "total_chunks": total_chunks,
            "total_fr": total_fr,
            "total_candidates": len(candidates),
            "data_chars": total_data_chars,
            "response_chars": len(merged),
            "attempts": total_attempts,
            "elapsed_ms": llm_elapsed_ms,
            "merge_input_chars": len(merge_input),
            "merge_attempts": merge_attempts,
        })

    logger.info(
        "TC identification complete (chunked): %d FR -> %d chunks -> %d TC candidates in %dms (attempts=%d)",
        total_fr, total_chunks, len(candidates), llm_elapsed_ms, total_attempts,
    )

    return candidates


def _chunk_fr_list(fr_list: list[dict], chunk_size: int) -> list[list[dict]]:
    """Разбивает список FR на последовательные чанки размером chunk_size."""
    return [fr_list[i : i + chunk_size] for i in range(0, len(fr_list), chunk_size)]




async def search_tc_on_landscape(
    tc_candidate_name: str,
    tc_description: str = "",
    tc_rationale: str = "",
    domain: str = "",
) -> tuple[list[dict], int, bool]:
    """
    Поиск TC на ландшафте через fdm-search (search_capability_v2).

    Формирует поисковый запрос как конкатенацию:
      tc_candidate_name + ", " + tc_rationale + ", " + tc_description

    Порядок важен: description может быть длинным и съедать весь лимит запроса,
    поэтому rationale (где по правилам identify_tc живут тех-детали: сервисы,
    каналы) ставится сразу после имени, чтобы попасть в запрос и помочь найти
    существующую TC на ландшафте. task_description в поисковый запрос не попадает.

    domain: фильтр по бизнес-возможностям (BC) — коды через запятую,
    передаётся в параметр domain search_capability_v2. Пустая строка — без фильтра.

    Возвращает кортеж (results, llm_attempts, was_rate_limited).
    Поиск не использует LLM напрямую, поэтому llm_attempts=0, was_rate_limited=False.
    """
    # Формируем поисковый запрос: имя TC + rationale + описание TC.
    # Весь запрос обрезается до _MAX_SEARCH_CONTEXT_CHARS: fdm-search возвращает пустую
    # выдачу на длинных запросах, а длинный URL провоцирует HTTP 413 на шлюзе.
    parts = [tc_candidate_name.strip()]
    if tc_rationale:
        parts.append(tc_rationale.strip())
    if tc_description:
        parts.append(tc_description.strip())

    text = ", ".join(parts)[:_MAX_SEARCH_CONTEXT_CHARS]
    if not text:
        logger.warning("Empty search query, skipping landscape search")
        return [], 0, False

    logger.info("Searching TC on landscape (fdm-search): %s (domain=%s)", text, domain or "-")

    try:
        results = await search_capability_v2(
            query=text,
            top_k=settings.FDM_SEARCH_TOP_K,
            exclude_systems=settings.FDM_SEARCH_EXCLUDE_SYSTEMS or None,
            parents=domain or None,
        )
    except Exception as e:
        logger.warning(
            "FDM search failed for '%s': %s", tc_candidate_name, str(e)
        )
        return [], 0, False

    logger.info(
        "Landscape search found %d results for '%s' (top_k=%d, exclude_systems=%s, domain=%s)",
        len(results),
        text,
        settings.FDM_SEARCH_TOP_K,
        settings.FDM_SEARCH_EXCLUDE_SYSTEMS or "-",
        domain or "-",
    )

    return results, 0, False


async def mass_search_tc(
    tc_candidates: list[dict],
    domain: str = "",
    progress_callback=None,
) -> dict:
    """
    Массовый поиск TC на ландшафте.
    Обрабатывает кандидатов последовательно, вызывая progress_callback
    для каждого. Возвращает словарь с результатами и статистикой.

    domain: фильтр по бизнес-возможностям (BC) — коды через запятую,
    прокидывается во все поисковые запросы.

    Возвращает:
    {
        "results": {tc_name: [list of result dicts], ...},
        "elapsed_ms": int,
    }
    """
    total = len(tc_candidates)
    all_results: dict[str, list[dict]] = {}
    start_ts = time.time()

    for i, candidate in enumerate(tc_candidates):
        name = candidate.get("name", "")
        description = candidate.get("description", "")
        rationale = candidate.get("rationale", "")

        if progress_callback:
            await progress_callback({
                "phase": "searching",
                "current_tc": i,
                "total_tc": total,
                "current_tc_name": name,
                "elapsed_ms": int((time.time() - start_ts) * 1000),
            })

        logger.info(
            "Mass search: processing %d/%d TC '%s'",
            i + 1, total, name,
        )

        results, _llm_attempts, _rate_limited = await search_tc_on_landscape(
            tc_candidate_name=name,
            tc_description=description,
            tc_rationale=rationale,
            domain=domain,
        )

        all_results[name] = results

    elapsed_ms = int((time.time() - start_ts) * 1000)

    if progress_callback:
        await progress_callback({
            "phase": "done",
            "current_tc": total,
            "total_tc": total,
            "current_tc_name": "",
            "elapsed_ms": elapsed_ms,
        })

    logger.info(
        "Mass search complete: %d TC processed, %dms",
        total, elapsed_ms,
    )

    return {
        "results": all_results,
        "elapsed_ms": elapsed_ms,
    }


# confirm_tc удалён — логика генерации кодов TC перенесена на фронтенд


def _parse_tc_result(result: str) -> list[dict]:
    """
    Парсинг JSON-ответа от LLM для TC (TC-centric формат).

    Ожидаемый формат от LLM:
      [{"name": "...", "description": "...", "rationale": "...", "score": 85, "fr_ids": ["FR-001", ...]}, ...]
    """
    raw = result.strip()

    first_bracket = raw.find("[")
    if first_bracket == -1:
        logger.warning(
            "No JSON array found in TC LLM response.\n"
            "First 500 chars: %s\nLast 500 chars: %s",
            raw[:500],
            raw[-500:],
        )
        return []

    # Хвост от открывающей [ до конца — основная рабочая область.
    tail = raw[first_bracket:]

    candidates_list = []

    # 1) Основной кандидат: от [ до последней ] в ответе. Подходит, когда JSON
    #    полный (закрывающая ] есть и массив валиден).
    last_bracket = tail.rfind("]")
    if last_bracket != -1:
        candidates_list.append(tail[: last_bracket + 1])

    # 2) «Ремонтные» кандидаты на случай обрыва по max_tokens: закрывающей ]
    #    главного массива нет (или она не последняя — модель оборвалась внутри
    #    объекта, а найденная ] принадлежит вложенному fr_ids). Тогда отрезаем
    #    незавершённый хвост по последнему полному объекту } и закрываем массив.
    #    Аналог восстановления в intake._parse_llm_result.
    last_obj = tail.rfind("}")
    if last_obj != -1 and "{" in tail[: last_obj + 1]:
        recovered = tail[: last_obj + 1]
        for candidate in (
            recovered + "]",
            recovered.rstrip().rstrip(",") + "]",
        ):
            if candidate not in candidates_list:
                candidates_list.append(candidate)

    # 3) Запасной вариант: все внутренние массивы без вложенности (regex).
    import re

    for match in re.finditer(r"\[[^\[\]]*\]", raw):
        candidate = match.group(0)
        if candidate not in candidates_list:
            candidates_list.append(candidate)

    if not candidates_list:
        logger.warning(
            "No JSON array found in TC LLM response.\n"
            "First 500 chars: %s\nLast 500 chars: %s",
            raw[:500],
            raw[-500:],
        )
        return []

    # Пробуем от самого длинного (наиболее полного) к короткому.
    # Кандидат считается успешным, только если из него извлечён хотя бы один
    # валидный TC (dict с name). Если список распарсился, но TC в нём нет
    # (например, regex поймал вложенный массив строк fr_ids) — НЕ возвращаем
    # пусто, а продолжаем перебор: более длинный кандидат может быть настоящим.
    candidates_list.sort(key=len, reverse=True)
    for candidate in candidates_list:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            continue

        normalized = _normalize_tc_items(data)
        if normalized:
            logger.info(
                "Parsed %d TC candidates from LLM response (candidate len=%d)",
                len(normalized),
                len(candidate),
            )
            return normalized
        # распарсилось в список, но без валидных TC — пробуем следующий кандидат

    logger.warning(
        "Failed to parse any JSON candidate from TC LLM response.\n"
        "Found %d candidates (longest=%d chars).\n"
        "First 500 chars: %s\nLast 500 chars: %s",
        len(candidates_list),
        len(candidates_list[0]) if candidates_list else 0,
        raw[:500],
        raw[-500:],
    )
    return []


def _normalize_tc_items(data: list) -> list[dict]:
    """Нормализует список сырых элементов ответа LLM в TC-кандидатов.

    Убирает не-dict'ы и объекты без name, гарантирует fr_ids как список,
    score нормализует в float (битые значения -> 0). Возвращает только
    валидные TC; если валидных нет — пустой список.
    """
    normalized = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            score = float(item.get("score", 0) or 0)
        except (ValueError, TypeError):
            score = 0
        norm = {
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "code": item.get("code", ""),
            "score": score,
            "rationale": item.get("rationale", ""),
            "fr_ids": item.get("fr_ids", []),
        }
        if not isinstance(norm["fr_ids"], list):
            # Если LLM вернула строку вместо списка — оборачиваем
            if isinstance(norm["fr_ids"], str) and norm["fr_ids"]:
                norm["fr_ids"] = [norm["fr_ids"]]
            else:
                norm["fr_ids"] = []
        if norm["name"]:
            normalized.append(norm)
    return normalized
