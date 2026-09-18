# Copyright (c) 2024 PJSC VimpelCom
"""
HTTP-клиент для вызова LLM (OpenAI-compatible API).
С детальным логированием запросов и ответов.
"""

import json
import logging
import time
import asyncio
from typing import Optional
import httpx

from app.config import settings

logger = logging.getLogger("hld-agent")

# Максимальная длина логируемого тела запроса/ответа (символов)
_MAX_LOG_BODY_LENGTH = 2000

# Контекстное окно модели (токены). Это верхний предел max_tokens: запрошенный выход
# не может превышать контекст модели, иначе провайдер вернёт HTTP 400.
_MODEL_CONTEXT_TOKENS = 262194

# Консервативная оценка входных токенов: ~3 символа на токен (кириллица/латиница).
# Завышение оценки действует как резерв безопасности — гарантирует, что вход + выход
# умещаются в контекст модели.
_CHARS_PER_TOKEN = 3


def _truncate(text: str, max_len: int = _MAX_LOG_BODY_LENGTH) -> str:
    """Обрезает текст до max_len символов, добавляя '... (truncated)' если нужно."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... (truncated, total {len(text)} chars)"


def is_thinking_enabled_for(prompt_name: str = "") -> bool:
    """Включён ли reasoning для конкретного промпта.

    True, если reasoning разрешён глобально (`LLM_ENABLE_THINKING`) или промпт
    перечислен в `LLM_THINKING_PROMPTS` (по умолчанию — только дедупликация:
    `merge_dedup`, `merge_tc`), где рассуждения повышают качество слияния дубликатов.
    """
    if settings.LLM_ENABLE_THINKING:
        return True
    allowed = {
        name.strip()
        for name in (settings.LLM_THINKING_PROMPTS or "").split(",")
        if name.strip()
    }
    return prompt_name in allowed


def disable_thinking_params(prompt_name: str = "") -> dict:
    """Параметры запроса, отключающие reasoning у thinking-моделей (Qwen3 и т.п.).

    Токены рассуждений расходуются из того же max_tokens, что и ответ: на больших
    входах весь бюджет уходит в thinking, finish_reason приходит =length, а content —
    ПУСТОЙ строкой при status=ok (инцидент 2026-09-11: пустые требования/TC на фронте).
    Поэтому reasoning выключен везде, кроме промптов из `LLM_THINKING_PROMPTS`.

    Единая точка правды для ВСЕХ обращений к LLM: используется и в
    chat_completion_with_stats, и в health-пробе. Для промптов с разрешённым
    reasoning возвращает {} — thinking остаётся включённым.

    chat_template_kwargs — параметр vLLM-совместимых шлюзов; если шлюз его не
    принимает (HTTP 400), выключить LLM_ENABLE_THINKING в окружении.
    """
    if is_thinking_enabled_for(prompt_name):
        return {}
    return {"chat_template_kwargs": {"enable_thinking": False}}


class LLMClient:
    """Клиент для вызова LLM через HTTP (OpenAI-compatible)."""

    def __init__(self):
        self.api_url = settings.LLM_API_URL
        self.model = settings.LLM_MODEL
        self.api_key = settings.LLM_API_KEY
        self.timeout = settings.LLM_TIMEOUT_SECONDS
        self.max_tokens = settings.LLM_MAX_TOKENS
        self.temperature = settings.LLM_TEMPERATURE
        self.retry_count = settings.LLM_RETRY_COUNT

    def _max_tokens_for_request(self, input_chars: int = 0, desired: int | None = None) -> int:
        """max_tokens для запроса с учётом контекста модели.

        max_tokens не может превышать контекст модели (_MODEL_CONTEXT_TOKENS), а входные
        токены (грубая оценка: input_chars / _CHARS_PER_TOKEN) занимают часть контекста —
        поэтому max_tokens клампируется, чтобы вход + запрошенный выход умещались в окно.

        desired — явно запрошенный бюджет (например, увеличенный для промптов с
        reasoning, где рассуждения делят лимит с ответом); None — использовать self.max_tokens.
        """
        requested = self.max_tokens if desired is None else desired
        input_tokens = input_chars // _CHARS_PER_TOKEN
        if input_tokens >= _MODEL_CONTEXT_TOKENS:
            logger.warning(
                "LLM prompt may exceed model context: ~%d input tokens >= context %d",
                input_tokens,
                _MODEL_CONTEXT_TOKENS,
            )
        available = max(1, _MODEL_CONTEXT_TOKENS - input_tokens)
        effective = min(requested, available)
        if effective != requested:
            logger.warning(
                "LLM max_tokens clamped: %d -> %d (context=%d, ~input_tokens=%d)",
                requested,
                effective,
                _MODEL_CONTEXT_TOKENS,
                input_tokens,
            )
        return effective

    def _max_tokens_for_prompt(self, prompt_name: str, input_chars: int) -> int:
        """Бюджет вывода для промпта: для reasoning-промптов — не меньше заданного запаса.

        Берём max(LLM_MAX_TOKENS, LLM_THINKING_MAX_TOKENS): запас нужен, чтобы
        рассуждения не съели весь лимит, но уже настроенный большой бюджет (например,
        в проде) понижать нельзя.
        """
        if is_thinking_enabled_for(prompt_name) and settings.LLM_THINKING_MAX_TOKENS > 0:
            desired = max(self.max_tokens, settings.LLM_THINKING_MAX_TOKENS)
            return self._max_tokens_for_request(input_chars=input_chars, desired=desired)
        return self._max_tokens_for_request(input_chars=input_chars)

    async def chat_completion_with_stats(
        self,
        system_prompt: str,
        user_message: str,
        prompt_name: str = "unknown",
    ) -> tuple[str, int, bool]:
        """
        Вызов LLM с возвратом статистики: (content, attempts_count, was_rate_limited).

        Возвращает кортеж (текст ответа, количество попыток, был ли rate limit 429).
        При 429 использует Retry-After и увеличивает количество ретраев.
        """
        last_error: Optional[Exception] = None
        attempts_made = 0
        was_rate_limited = False
        # Для 429 делаем больше попыток с более длинными паузами
        max_429_retries = max(self.retry_count, 6)
        # Пустой content при status=ok — отдельный класс сбоя (модель истратила весь
        # бюджет вывода на reasoning). Повторяем, но ограниченно: каждый такой ответ
        # генерируется долго, поэтому пара попыток, а не полный цикл ретраев.
        empty_content_retries = 0
        max_empty_content_retries = 2

        input_chars = len(system_prompt) + len(user_message)
        thinking_on = is_thinking_enabled_for(prompt_name)

        request_body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": self._max_tokens_for_prompt(prompt_name, input_chars),
            "temperature": self.temperature,
        }

        # Reasoning выключен везде, кроме промптов из LLM_THINKING_PROMPTS
        # (по умолчанию — дедупликация: merge_dedup, merge_tc) — см. disable_thinking_params().
        request_body.update(disable_thinking_params(prompt_name))

        logger.info(
            "LLM REQUEST | prompt=%s | model=%s | thinking=%s | max_tokens=%d | "
            "system_prompt_len=%d | user_message_len=%d\n"
            "--- REQUEST BODY ---\n%s\n--- END REQUEST ---",
            prompt_name,
            self.model,
            thinking_on,
            request_body["max_tokens"],
            len(system_prompt),
            len(user_message),
            _truncate(json.dumps(request_body, ensure_ascii=False, indent=2)),
        )

        for attempt in range(1, max_429_retries + 1):
            attempts_made = attempt
            start_time = time.time()
            try:
                async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
                    response = await client.post(
                        f"{self.api_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=request_body,
                    )
                    response.raise_for_status()
                    data = response.json()

                    duration_ms = int((time.time() - start_time) * 1000)
                    usage = data.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)

                    choice = data["choices"][0]
                    message = choice.get("message", {})
                    content = message.get("content") or ""
                    finish_reason = choice.get("finish_reason")
                    # У thinking-моделей рассуждения приходят отдельным полем
                    reasoning_chars = len(message.get("reasoning_content") or "")

                    logger.info(
                        "LLM RESPONSE | prompt=%s | attempt=%d/%d | status=ok | "
                        "duration=%dms | prompt_tokens=%d | completion_tokens=%d | "
                        "finish_reason=%s | reasoning_chars=%d | rate_limited=%s\n"
                        "--- RESPONSE BODY ---\n%s\n--- END RESPONSE ---",
                        prompt_name,
                        attempt,
                        max_429_retries,
                        duration_ms,
                        prompt_tokens,
                        completion_tokens,
                        finish_reason,
                        reasoning_chars,
                        was_rate_limited,
                        _truncate(content),
                    )

                    if not content.strip():
                        # Пустой ответ при status=ok: раньше считался успешным и молча
                        # превращался в "0 требований" у потребителя.
                        last_error = RuntimeError(
                            f"empty content (finish_reason={finish_reason}, "
                            f"completion_tokens={completion_tokens}, "
                            f"reasoning_chars={reasoning_chars})"
                        )
                        if (
                            empty_content_retries < max_empty_content_retries
                            and attempt < max_429_retries
                        ):
                            empty_content_retries += 1
                            wait = 2 ** empty_content_retries
                            logger.warning(
                                "LLM EMPTY CONTENT | prompt=%s | attempt=%d/%d | "
                                "finish_reason=%s | completion_tokens=%d | "
                                "reasoning_chars=%d | retry=%d/%d in %ds",
                                prompt_name,
                                attempt,
                                max_429_retries,
                                finish_reason,
                                completion_tokens,
                                reasoning_chars,
                                empty_content_retries,
                                max_empty_content_retries,
                                wait,
                            )
                            await asyncio.sleep(wait)
                            continue
                        # Попытки исчерпаны — возвращаем пустой ответ: вызывающий код
                        # имеет fallback (сборка из чанков), исключение его бы лишило.
                        logger.warning(
                            "LLM EMPTY CONTENT | prompt=%s | attempts exhausted (%d) | "
                            "finish_reason=%s | completion_tokens=%d | reasoning_chars=%d | "
                            "returning empty response",
                            prompt_name,
                            attempts_made,
                            finish_reason,
                            completion_tokens,
                            reasoning_chars,
                        )
                        return content, attempts_made, was_rate_limited

                    return content, attempts_made, was_rate_limited

            except httpx.HTTPStatusError as e:
                duration_ms = int((time.time() - start_time) * 1000)
                status_code = e.response.status_code if hasattr(e, "response") else 0

                # Специфичная обработка 429 Too Many Requests
                if status_code == 429:
                    was_rate_limited = True
                    retry_after = 0
                    # Пробуем извлечь Retry-After из заголовка
                    if hasattr(e, "response") and e.response.headers:
                        try:
                            retry_after = int(e.response.headers.get("Retry-After", "0"))
                        except (ValueError, TypeError):
                            retry_after = 0
                    # Ждём не менее 10 секунд для rate limit,
                    # но не более 30 — чтобы не превысить таймаут nginx 60s
                    wait = min(max(retry_after, 10), 30)
                    logger.warning(
                        "LLM RATE LIMITED (429) | prompt=%s | attempt=%d/%d | "
                        "duration=%dms | retry_after=%ds | waiting=%ds",
                        prompt_name,
                        attempt,
                        max_429_retries,
                        duration_ms,
                        retry_after,
                        wait,
                    )
                    last_error = e
                    if attempt < max_429_retries:
                        await asyncio.sleep(wait)
                    continue

                # Другие HTTP ошибки
                logger.warning(
                    "LLM HTTP ERROR | prompt=%s | attempt=%d/%d | status=%d | "
                    "duration=%dms | error=%s",
                    prompt_name,
                    attempt,
                    max_429_retries,
                    status_code,
                    duration_ms,
                    str(e),
                )
                last_error = e
                if attempt < max_429_retries:
                    wait = 2 ** attempt
                    await asyncio.sleep(wait)

            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                logger.warning(
                    "LLM RESPONSE | prompt=%s | attempt=%d/%d | status=error | "
                    "duration=%dms | error=%s",
                    prompt_name,
                    attempt,
                    max_429_retries,
                    duration_ms,
                    str(e),
                )
                last_error = e
                if attempt < max_429_retries:
                    wait = 2 ** attempt
                    await asyncio.sleep(wait)

        raise RuntimeError(
            f"LLM call failed after {max_429_retries} attempts (rate_limited={was_rate_limited}): {last_error}"
        )


# Singleton
llm_client = LLMClient()
