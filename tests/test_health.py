# Copyright (c) 2024 PJSC VimpelCom
"""
Тесты для health-check endpoint.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    """GET /api/health должен возвращать status=ok или degraded (если внешние сервисы недоступны)."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("ok", "degraded")
    assert "timestamp" in data
    assert data["version"] == "0.1.0"
    # Проверяем, что интеграции имеют корректные статусы
    assert "integrations" in data
    assert data["integrations"]["backend"] == "ok"


@pytest.mark.asyncio
async def test_health_probe_disables_thinking(client, monkeypatch):
    """Health-проба ходит в LLM напрямую — reasoning должен быть выключен и там.

    Иначе все 10 токенов пробы уходят в thinking, content приходит пустым, и проба
    маскирует деградацию модели (смотрит только на HTTP-статус).
    """
    from app.api import health as health_module
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_API_KEY", "test-key")
    # Внешние проверки отключаем, чтобы тест не ходил в сеть
    monkeypatch.setattr(settings, "BEEATLAS_API_URL", "")
    monkeypatch.setattr(settings, "BEEATLAS_API_KEY", "")

    bodies: list[dict] = []

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            bodies.append(json)
            return _FakeResponse()

    monkeypatch.setattr(health_module.httpx, "AsyncClient", _FakeAsyncClient)

    response = await client.get("/api/health")

    assert response.status_code == 200
    assert bodies, "LLM-проба должна была выполниться"
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
