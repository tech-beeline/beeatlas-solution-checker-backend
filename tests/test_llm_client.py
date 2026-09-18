# Copyright (c) 2024 PJSC VimpelCom
"""
Тесты LLM-клиента: отключение reasoning во всех запросах и обработка пустого ответа.

Пустой content при status=ok (модель истратила весь бюджет max_tokens на reasoning)
раньше считался успехом и молча превращался в «0 требований» на фронте —
инцидент 2026-09-11.
"""

import pytest

from app.config import settings
from app.integrations import llm_client as llm_module
from app.integrations.llm_client import (
    _CHARS_PER_TOKEN,
    _MODEL_CONTEXT_TOKENS,
    disable_thinking_params,
    is_thinking_enabled_for,
    llm_client,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Фейковый httpx.AsyncClient: отдаёт заранее заданные ответы, пишет тела запросов."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.bodies: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.bodies.append(json)
        return _FakeResponse(self.outcomes.pop(0))


def _payload(content, finish_reason="stop", completion_tokens=10, reasoning=None):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens},
    }


def _patch_client(monkeypatch, outcomes):
    fake = _FakeAsyncClient(outcomes)
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", lambda *a, **k: fake)
    return fake


async def _no_sleep(_seconds):
    """Заглушка asyncio.sleep, чтобы ретраи в тестах не ждали реально."""
    return None


def test_disable_thinking_params_default(monkeypatch):
    """Обычные промпты уходят с выключенным reasoning."""
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", False)
    assert disable_thinking_params("structure_chunk") == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    # health-проба тоже без reasoning
    assert disable_thinking_params("health") == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_disable_thinking_params_when_thinking_enabled(monkeypatch):
    """При LLM_ENABLE_THINKING=true ничего не добавляем (thinking остаётся)."""
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", True)
    assert disable_thinking_params() == {}


def test_thinking_enabled_for_dedup_prompts(monkeypatch):
    """Дедупликация — исключение: reasoning у неё остаётся включённым."""
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", False)
    monkeypatch.setattr(settings, "LLM_THINKING_PROMPTS", "merge_dedup,merge_tc")

    assert disable_thinking_params("merge_dedup") == {}
    assert disable_thinking_params("merge_tc") == {}
    assert is_thinking_enabled_for("merge_dedup") is True
    # остальные промпты по-прежнему без reasoning
    assert is_thinking_enabled_for("identify_tc") is False


def test_thinking_prompts_list_is_configurable(monkeypatch):
    """Список промптов с reasoning настраивается через env."""
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", False)
    monkeypatch.setattr(settings, "LLM_THINKING_PROMPTS", " identify_tc , merge_tc ")

    assert is_thinking_enabled_for("identify_tc") is True   # пробелы обрезаются
    assert is_thinking_enabled_for("merge_tc") is True
    assert is_thinking_enabled_for("merge_dedup") is False  # убрали из списка

    monkeypatch.setattr(settings, "LLM_THINKING_PROMPTS", "")
    assert is_thinking_enabled_for("merge_tc") is False


def test_thinking_prompts_get_larger_budget(monkeypatch):
    """Для промптов с reasoning бюджет вывода задаётся с запасом под рассуждения."""
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", False)
    monkeypatch.setattr(settings, "LLM_THINKING_PROMPTS", "merge_dedup,merge_tc")
    monkeypatch.setattr(settings, "LLM_THINKING_MAX_TOKENS", 32768)
    monkeypatch.setattr(llm_client, "max_tokens", 16000)

    assert llm_client._max_tokens_for_prompt("merge_dedup", 0) == 32768
    assert llm_client._max_tokens_for_prompt("structure_chunk", 0) == 16000

    # Уже настроенный большой бюджет не понижаем (в проде LLM_MAX_TOKENS может быть больше)
    monkeypatch.setattr(llm_client, "max_tokens", 50000)
    assert llm_client._max_tokens_for_prompt("merge_dedup", 0) == 50000
    monkeypatch.setattr(llm_client, "max_tokens", 16000)

    # 0 — не переопределять бюджет
    monkeypatch.setattr(settings, "LLM_THINKING_MAX_TOKENS", 0)
    assert llm_client._max_tokens_for_prompt("merge_dedup", 0) == 16000

    # и всё равно клампится под контекст модели при большом входе
    monkeypatch.setattr(settings, "LLM_THINKING_MAX_TOKENS", 32768)
    big_input = _MODEL_CONTEXT_TOKENS * _CHARS_PER_TOKEN
    assert llm_client._max_tokens_for_prompt("merge_dedup", big_input) == 1


@pytest.mark.asyncio
async def test_request_disables_thinking(monkeypatch):
    """Каждый запрос к LLM уходит с выключенным reasoning."""
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", False)
    fake = _patch_client(monkeypatch, [_payload('{"ok":true}')])

    content, _, _ = await llm_client.chat_completion_with_stats(
        system_prompt="sys", user_message="usr", prompt_name="test"
    )

    assert content == '{"ok":true}'
    assert fake.bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_dedup_request_keeps_thinking(monkeypatch):
    """Запрос дедупликации уходит с reasoning и увеличенным бюджетом вывода."""
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", False)
    monkeypatch.setattr(settings, "LLM_THINKING_PROMPTS", "merge_dedup,merge_tc")
    monkeypatch.setattr(settings, "LLM_THINKING_MAX_TOKENS", 32768)
    monkeypatch.setattr(llm_client, "max_tokens", 16000)

    fake = _patch_client(monkeypatch, [_payload('{"ok":true}')])

    await llm_client.chat_completion_with_stats(
        system_prompt="sys", user_message="usr", prompt_name="merge_dedup"
    )

    body = fake.bodies[0]
    assert "chat_template_kwargs" not in body   # thinking не выключаем
    assert body["max_tokens"] == 32768          # бюджет с запасом под рассуждения

    # для сравнения: обычный промпт — reasoning выключен и бюджет прежний
    fake2 = _patch_client(monkeypatch, [_payload('{"ok":true}')])
    await llm_client.chat_completion_with_stats(
        system_prompt="sys", user_message="usr", prompt_name="structure_chunk"
    )
    assert fake2.bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert fake2.bodies[0]["max_tokens"] == 16000


@pytest.mark.asyncio
async def test_empty_content_is_retried(monkeypatch):
    """Пустой content при status=ok переигрывается, а не отдаётся как успех."""
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", False)
    monkeypatch.setattr(llm_module.asyncio, "sleep", _no_sleep)

    empty = _payload(
        "", finish_reason="length", completion_tokens=16384, reasoning="x" * 500
    )
    good = _payload('{"ok":true}')
    fake = _patch_client(monkeypatch, [empty, good])

    content, attempts, _ = await llm_client.chat_completion_with_stats(
        system_prompt="sys", user_message="usr", prompt_name="test"
    )

    assert content == '{"ok":true}'
    assert attempts == 2
    assert len(fake.bodies) == 2


@pytest.mark.asyncio
async def test_empty_content_returned_after_retries_exhausted(monkeypatch):
    """После исчерпания повторов возвращаем пустой ответ, а не бросаем исключение.

    Исключение превратило бы задачу в error и лишило вызывающий код fallback
    (сборка требований из чанков).
    """
    monkeypatch.setattr(settings, "LLM_ENABLE_THINKING", False)
    monkeypatch.setattr(llm_module.asyncio, "sleep", _no_sleep)

    empty = _payload("", finish_reason="length", completion_tokens=16384)
    fake = _patch_client(monkeypatch, [empty, dict(empty), dict(empty)])

    content, attempts, _ = await llm_client.chat_completion_with_stats(
        system_prompt="sys", user_message="usr", prompt_name="test"
    )

    assert content == ""
    # 1 попытка + 2 повтора (max_empty_content_retries)
    assert attempts == 3
    assert len(fake.bodies) == 3
