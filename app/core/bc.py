# Copyright (c) 2024 PJSC VimpelCom
"""
Бизнес-логика этапа Business Capability:
выявление BC из текста задачи через LLM (промпт prompt_top10_with_catalog.txt).

Все функции stateless — не хранят состояние между вызовами.
"""

import json
import logging
import time

from app.integrations.llm_client import llm_client

logger = logging.getLogger("hld-agent")

# Плейсхолдер текста задачи в промпте prompt_top10_with_catalog.txt
# (каталог бизнес-возможностей уже зашит в файл, заполнять нужно только задачу)
_TASK_PLACEHOLDER = "[вставьте сюда текст задачи]"


def _load_prompt(name: str) -> str:
    """Загрузка системного промпта из файла."""
    try:
        with open(f"app/prompts/{name}.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("Prompt file not found: app/prompts/%s.txt", name)
        return ""


async def identify_business_capabilities(
    task_text: str,
    progress_callback=None,
) -> list[dict]:
    """
    Выявление business capability из текста задачи через LLM.

    1. Загружает промпт prompt_top10_with_catalog.txt (каталог BC зашит в файле)
    2. Подставляет текст задачи в плейсхолдер
    3. Вызывает LLM, парсит JSON-массив
    4. Нормализует результат [{code, description, relevance, reason}]

    progress_callback: опциональная async-функция для отправки прогресса.
    Принимает dict с полями: phase, data_chars, response_chars, attempts, elapsed_ms.
    """
    if not task_text or not task_text.strip():
        raise ValueError("Нет текста задачи для выявления BC")

    prompt = _load_prompt("prompt_top10_with_catalog")
    if not prompt:
        raise RuntimeError(
            "Системный промпт prompt_top10_with_catalog не найден в app/prompts/"
        )

    # Подставляем полный текст задачи (без обрезки); защита контекста — клампинг
    # max_tokens под окно модели в llm_client._max_tokens_for_request.
    prompt = prompt.replace(_TASK_PLACEHOLDER, task_text)

    logger.info(
        "Identifying BC from task text (%d chars)",
        len(task_text),
    )

    start_time = time.time()

    if progress_callback:
        await progress_callback({
            "phase": "sending",
            "data_chars": len(task_text),
            "response_chars": 0,
        })

    result, attempts, _was_rate_limited = await llm_client.chat_completion_with_stats(
        system_prompt=prompt,
        user_message="Выяви business capability для приведённой задачи.",
        prompt_name="identify_bc",
    )

    llm_elapsed_ms = int((time.time() - start_time) * 1000)

    # Парсинг результата
    candidates = _parse_bc_result(result)

    if progress_callback:
        await progress_callback({
            "phase": "done",
            "data_chars": len(task_text),
            "response_chars": len(result),
            "attempts": attempts,
            "elapsed_ms": llm_elapsed_ms,
        })

    logger.info(
        "BC identification complete: %d candidates in %dms (attempts=%d)",
        len(candidates),
        llm_elapsed_ms,
        attempts,
    )

    return candidates


def _parse_bc_result(result: str) -> list[dict]:
    """
    Парсинг JSON-ответа от LLM для BC.

    Ожидаемый формат от LLM:
      [{"code": "...", "description": "...", "relevance": 92, "reason": "..."}, ...]

    Универсальный алгоритм (как в _parse_tc_result):
    1. Находит первую [ и последнюю ] — основной кандидат
    2. Если последняя ] отсутствует — добавляет закрывающие скобки
    3. Ищет вложенные массивы через regex
    4. Сортирует кандидатов по длине, пробует json.loads
    5. Нормализует: гарантирует relevance как int
    """
    raw = result.strip()

    first_bracket = raw.find("[")
    last_bracket = raw.rfind("]")

    candidates_list = []

    if first_bracket != -1:
        if last_bracket != -1 and last_bracket > first_bracket:
            main_candidate = raw[first_bracket : last_bracket + 1]
            candidates_list.append(main_candidate)
        else:
            truncated = raw[first_bracket:]
            candidates_list.append(truncated)
            candidates_list.append(truncated + "]")
            candidates_list.append(truncated + "]}")
            candidates_list.append(truncated + "]}]")

        import re

        for match in re.finditer(r"\[[^\[\]]*\]", raw):
            candidate = match.group(0)
            if candidate not in candidates_list:
                candidates_list.append(candidate)

    if not candidates_list:
        logger.warning(
            "No JSON array found in BC LLM response.\n"
            "First 500 chars: %s\nLast 500 chars: %s",
            raw[:500],
            raw[-500:],
        )
        return []

    candidates_list.sort(key=len, reverse=True)
    for candidate in candidates_list:
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                logger.info(
                    "Parsed %d BC candidates from LLM response (candidate len=%d)",
                    len(data),
                    len(candidate),
                )
                normalized = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    code = item.get("code", "")
                    if not code:
                        continue
                    # relevance может прийти числом или строкой — нормализуем в int
                    try:
                        relevance = int(float(item.get("relevance", 0) or 0))
                    except (ValueError, TypeError):
                        relevance = 0
                    normalized.append({
                        "code": code,
                        "description": item.get("description", ""),
                        "relevance": relevance,
                        "reason": item.get("reason", ""),
                    })
                return normalized
        except json.JSONDecodeError:
            continue

    logger.warning(
        "Failed to parse any JSON candidate from BC LLM response.\n"
        "Found %d candidates (longest=%d chars).\n"
        "First 500 chars: %s\nLast 500 chars: %s",
        len(candidates_list),
        len(candidates_list[0]) if candidates_list else 0,
        raw[:500],
        raw[-500:],
    )
    return []
