# Copyright (c) 2024 PJSC VimpelCom
"""
Тесты для Intake API (stateless).
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_import_text(client):
    """POST /api/intake/import с текстом -> 200 + raw_text."""
    response = await client.post("/api/intake/import", json={"text": "test requirement"})
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "text"
    assert data["raw_text"] == "test requirement"
    assert data["title"] == "test requirement"


@pytest.mark.asyncio
async def test_import_empty(client):
    """POST /api/intake/import без текста и URL -> 400."""
    response = await client.post("/api/intake/import", json={})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_structure_start_no_text(client):
    """POST /api/intake/structure/start без текста -> 400."""
    response = await client.post(
        "/api/intake/structure/start", json={"raw_text": ""}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_structure_start_and_progress(client):
    """POST /api/intake/structure/start -> task_id, затем GET progress."""
    response = await client.post(
        "/api/intake/structure/start",
        json={"raw_text": "Система должна управлять заказами клиентов"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "task_id" in data
    task_id = data["task_id"]

    # Проверяем прогресс
    progress_response = await client.get(f"/api/intake/structure/{task_id}/progress")
    assert progress_response.status_code == 200
    progress_data = progress_response.json()
    assert "phase" in progress_data
    assert "done" in progress_data


@pytest.mark.asyncio
async def test_structure_progress_not_found(client):
    """GET /api/intake/structure/{unknown}/progress -> 404."""
    response = await client.get("/api/intake/structure/unknown-task-id/progress")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_structure_result_not_found(client):
    """GET /api/intake/structure/{unknown}/result -> 404."""
    response = await client.get("/api/intake/structure/unknown-task-id/result")
    assert response.status_code == 404


# Текст из нескольких абзацев, который _chunk_text разрезает на несколько чанков
MULTI_CHUNK_TEXT = "\n\n".join(f"Пункт {i} " + "A" * 900 for i in range(8))


def test_collect_chunk_requirements_dedups_and_skips_unparsed():
    """Fallback-сборка: склейка чанков, дедуп по type+title, пропуск пустых ответов."""
    from app.core.intake import _collect_chunk_requirements

    chunk_1 = '[{"type":"FR","title":"Управление заказами","description":"d1"},' \
              '{"type":"NFR","title":"Скорость","description":"d2"}]'
    chunk_2 = ""  # модель вернула пустой content
    chunk_3 = '[{"type":"fr","title":"управление заказами","description":"дубль"},' \
              '{"type":"OQ","title":"Нужно уточнить","description":"d3"}]'

    result = _collect_chunk_requirements([chunk_1, chunk_2, chunk_3])

    # Дубликат по title (регистр не важен) отброшен, пустой чанк пропущен
    assert [(r["type"], r["title"]) for r in result] == [
        ("FR", "Управление заказами"),
        ("NFR", "Скорость"),
        ("OQ", "Нужно уточнить"),
    ]


@pytest.mark.asyncio
async def test_structure_falls_back_to_chunks_when_merge_empty(monkeypatch):
    """merge_dedup вернул пусто (reasoning съел бюджет) — требования берём из чанков."""
    from app.core import intake as intake_module
    from app.integrations.llm_client import llm_client

    calls: list[str] = []

    async def fake_llm(system_prompt, user_message, prompt_name="unknown"):
        calls.append(prompt_name)
        if prompt_name == "structure_chunk":
            return (
                '[{"type":"FR","title":"TC raw","description":"d"}]',
                1,
                False,
            )
        # merge_dedup: пустой content (finish_reason=length, весь бюджет ушёл в reasoning)
        return "", 3, False

    monkeypatch.setattr(llm_client, "chat_completion_with_stats", fake_llm)

    result = await intake_module.structure_requirements(MULTI_CHUNK_TEXT)

    assert calls.count("merge_dedup") == 1
    assert len(result) >= 1
    assert all(r["type"] == "FR" for r in result)
    assert all(r["title"] == "TC raw" for r in result)
    # ID присвоены после fallback
    assert result[0]["id"] == "FR-1"


@pytest.mark.asyncio
async def test_structure_merge_input_excludes_empty_chunks(monkeypatch):
    """Пустые ответы чанков не попадают в merge-запрос (только зашумляют вход)."""
    from app.core import intake as intake_module
    from app.integrations.llm_client import llm_client

    chunk_calls = 0
    merge_user_message = ""

    async def fake_llm(system_prompt, user_message, prompt_name="unknown"):
        nonlocal chunk_calls, merge_user_message
        if prompt_name == "structure_chunk":
            chunk_calls += 1
            # Чётные чанки — пустой ответ
            if chunk_calls % 2 == 0:
                return "", 1, False
            return '[{"type":"FR","title":"T","description":"d"}]', 1, False
        merge_user_message = user_message
        return '[{"type":"FR","title":"T","description":"d"}]', 1, False

    monkeypatch.setattr(llm_client, "chat_completion_with_stats", fake_llm)

    result = await intake_module.structure_requirements(MULTI_CHUNK_TEXT)

    assert chunk_calls >= 3
    # В merge ушли только непустые результаты чанков: ни одной пустой строки в массиве
    import json

    passed = json.loads(merge_user_message)
    assert passed, "merge-вход не должен быть пустым"
    assert all(isinstance(item, str) and item.strip() for item in passed)
    assert len(result) == 1
