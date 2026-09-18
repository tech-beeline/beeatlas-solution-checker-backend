# Copyright (c) 2024 PJSC VimpelCom
"""
Тесты для Business Capability API (stateless) и core-логики выявления BC.
"""

import asyncio

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.bc import _parse_bc_result
from app.integrations.llm_client import llm_client


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_bc_identify_start_empty_text(client):
    """POST /api/bc/identify/start без task_text -> 400."""
    response = await client.post("/api/bc/identify/start", json={"task_text": ""})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_bc_identify_progress_not_found(client):
    """GET /api/bc/identify/{task_id}/progress с несуществующим task_id -> 404."""
    response = await client.get("/api/bc/identify/non-existent/progress")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_bc_identify_result_not_found(client):
    """GET /api/bc/identify/{task_id}/result с несуществующим task_id -> 404."""
    response = await client.get("/api/bc/identify/non-existent/result")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_bc_identify_full_flow(client, monkeypatch):
    """Полный цикл с мокнутым LLM: start -> progress -> result."""

    async def fake_llm(system_prompt, user_message, prompt_name="unknown"):
        # Проверяем, что текст задачи подставлен в промпт вместо плейсхолдера
        assert "Тестовая задача по кадровому учёту" in system_prompt
        return (
            '[{"code":"BC-019652","description":"Кадровый учёт",'
            '"relevance":95,"reason":"Релевантно задаче"}]',
            1,
            False,
        )

    monkeypatch.setattr(llm_client, "chat_completion_with_stats", fake_llm)

    response = await client.post(
        "/api/bc/identify/start",
        json={"task_text": "Тестовая задача по кадровому учёту"},
    )
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    assert len(task_id) > 0

    # Poll progress until done
    max_attempts = 10
    for _ in range(max_attempts):
        resp = await client.get(f"/api/bc/identify/{task_id}/progress")
        assert resp.status_code == 200
        data = resp.json()
        if data["done"]:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("BC identify task did not complete in time")

    # Result
    resp = await client.get(f"/api/bc/identify/{task_id}/result")
    assert resp.status_code == 200
    data = resp.json()
    assert "candidates" in data
    assert len(data["candidates"]) == 1
    cand = data["candidates"][0]
    assert cand["code"] == "BC-019652"
    assert cand["relevance"] == 95
    assert "reason" in cand


def test_parse_bc_result():
    """Парсинг валидного JSON-ответа; relevance из строки нормализуется в int."""
    result = (
        '[{"code":"BC-019652","description":"Кадровый учёт",'
        '"relevance":95,"reason":"Релевантно"}, '
        '{"code":"DMN.092","description":"Домен","relevance":"80","reason":"Смежный"}]'
    )
    parsed = _parse_bc_result(result)
    assert len(parsed) == 2
    assert parsed[0]["code"] == "BC-019652"
    assert parsed[0]["relevance"] == 95
    assert parsed[1]["relevance"] == 80


def test_parse_bc_result_skips_empty_code():
    """Кандидаты без code отбрасываются."""
    result = (
        '[{"code":"","description":"x","relevance":1,"reason":""},'
        '{"code":"BC-1","description":"y","relevance":2,"reason":""}]'
    )
    parsed = _parse_bc_result(result)
    assert len(parsed) == 1
    assert parsed[0]["code"] == "BC-1"


def test_parse_bc_result_no_json():
    """Ответ без JSON-массива -> пустой список."""
    assert _parse_bc_result("не JSON") == []
