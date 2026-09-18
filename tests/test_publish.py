# Copyright (c) 2024 PJSC VimpelCom
"""
Тесты для Publish API (stateless).
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_export(client):
    """POST /api/publish/export -> 200 + markdown file."""
    response = await client.post(
        "/api/publish/export",
        json={
            "title": "Test Session",
            "source": "text",
            "structured_requirements": [
                {"id": "FR-001", "type": "FR", "title": "Test", "description": "Test desc"}
            ],
            "impact_tcs": [
                {"code": "TC-001", "name": "Test TC", "description": "TC desc", "action": "reuse", "source": "landscape", "fr_ids": ["FR-001"], "system": {"code": "SYS-001", "name": "Test System", "endpoints": ["/api/test"]}}
            ],
        },
    )
    assert response.status_code == 200
    assert "text/markdown" in response.headers.get("content-type", "")
    assert "hld-report" in response.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_confluence_no_pat(client):
    """POST /api/publish/confluence без PAT -> 404 (нет parent_page_url)."""
    response = await client.post(
        "/api/publish/confluence",
        json={
            "title": "Test Session",
            "source": "text",
        },
    )
    # Confluence API не настроен, ожидаем 404 (ValueError при попытке публикации)
    assert response.status_code == 404


# --- create_or_update_page: поиск дочерней страницы по имени ---

CONF_BASE = "https://confluence.example"
PARENT_ID = "123456"
TARGET_TITLE = "HLD Report: Session"
PAT = "test-pat"


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Фейковый httpx.AsyncClient: хендлеры для GET/PUT/POST + журнал вызовов."""

    def __init__(self, get_handler=None, put_handler=None, post_handler=None):
        self._get = get_handler or (lambda url, params: FakeResponse(200, {"results": []}))
        self._put = put_handler or (lambda url: FakeResponse(200))
        self._post = post_handler or (lambda url: FakeResponse(200))
        self.calls: list[tuple[str, str]] = []
        self.post_bodies: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, *, params=None, headers=None):
        self.calls.append(("GET", url))
        return self._get(url, params or {})

    async def put(self, url, *, json=None, headers=None):
        self.calls.append(("PUT", url))
        return self._put(url)

    async def post(self, url, *, json=None, headers=None):
        self.calls.append(("POST", url))
        self.post_bodies.append(json or {})
        return self._post(url)


def _patch_confluence_http(monkeypatch, fake: FakeClient):
    from app.integrations import confluence as conf_module

    monkeypatch.setattr(conf_module.httpx, "AsyncClient", lambda *_a, **_k: fake)
    return conf_module


@pytest.mark.asyncio
async def test_update_matches_child_by_title(monkeypatch):
    """Перезаписывается та дочерняя страница, чей title совпадает (не первая попавшаяся)."""
    fake = FakeClient(
        get_handler=lambda url, params: FakeResponse(200, {
            "results": [
                {"id": "111", "title": "Другая страница", "version": {"number": 1}},
                {"id": "222", "title": TARGET_TITLE, "version": {"number": 2}},
            ],
        }),
        put_handler=lambda url: FakeResponse(200, {"id": "222"}),
    )
    conf_module = _patch_confluence_http(monkeypatch, fake)

    result = await conf_module.confluence_client.create_or_update_page(
        title=TARGET_TITLE,
        body="<p>content</p>",
        parent_page_id=PARENT_ID,
        pat=PAT,
        confluence_base_url=CONF_BASE,
        space_key="TEST",
    )

    assert result == "222"
    # PUT ушёл именно на совпавшую страницу 222, а не на первую (111)
    assert ("PUT", f"{CONF_BASE}/rest/api/content/222") in fake.calls
    assert not any(m == "PUT" and "111" in url for m, url in fake.calls)


@pytest.mark.asyncio
async def test_create_new_when_no_title_match(monkeypatch):
    """Если страницы с таким названием нет ни среди дочерних, ни в space — создаётся новая."""
    def get_handler(url, params):
        if "child/page" in url:
            return FakeResponse(200, {
                "results": [
                    {"id": "111", "title": "Другая страница", "version": {"number": 1}},
                ],
            })
        # space-поиск (GET /rest/api/content) — пусто
        return FakeResponse(200, {"results": []})

    fake = FakeClient(
        get_handler=get_handler,
        put_handler=lambda url: FakeResponse(500),  # PUT не должен вызываться
        post_handler=lambda url: FakeResponse(200, {"id": "333"}),
    )
    conf_module = _patch_confluence_http(monkeypatch, fake)

    result = await conf_module.confluence_client.create_or_update_page(
        title=TARGET_TITLE,
        body="<p>content</p>",
        parent_page_id=PARENT_ID,
        pat=PAT,
        confluence_base_url=CONF_BASE,
        space_key="TEST",
    )

    assert result == "333"
    assert ("POST", f"{CONF_BASE}/rest/api/content") in fake.calls
    assert not any(m == "PUT" for m, _ in fake.calls)


@pytest.mark.asyncio
async def test_create_with_unique_title_when_no_child_match(monkeypatch):
    """Если среди дочерних совпадения нет, но название занято в space — создаётся
    новая страница с уникальным именем (к базовому добавляется « Impact»)."""
    def get_handler(url, params):
        if "child/page" in url:
            return FakeResponse(200, {
                "results": [
                    {"id": "111", "title": "Другая страница", "version": {"number": 1}},
                ],
            })
        # space-поиск: базовое название занято, "<base> Impact" — свободно
        if params.get("title") == TARGET_TITLE:
            return FakeResponse(200, {
                "results": [{"id": "999", "title": TARGET_TITLE}],
            })
        return FakeResponse(200, {"results": []})

    fake = FakeClient(
        get_handler=get_handler,
        put_handler=lambda url: FakeResponse(500),  # PUT не должен вызываться
        post_handler=lambda url: FakeResponse(200, {"id": "333"}),
    )
    conf_module = _patch_confluence_http(monkeypatch, fake)

    result = await conf_module.confluence_client.create_or_update_page(
        title=TARGET_TITLE,
        body="<p>content</p>",
        parent_page_id=PARENT_ID,
        pat=PAT,
        confluence_base_url=CONF_BASE,
        space_key="TEST",
    )

    assert result == "333"
    assert ("POST", f"{CONF_BASE}/rest/api/content") in fake.calls
    # Создана страница с уникальным названием "<base> Impact"
    assert fake.post_bodies and fake.post_bodies[-1].get("title") == f"{TARGET_TITLE} Impact"
    assert not any(m == "PUT" for m, _ in fake.calls)


@pytest.mark.asyncio
async def test_update_paginates_to_find_match(monkeypatch):
    """Поиск по имени работает через несколько страниц дочерних страниц (limit=100)."""
    first_page = {
        "results": [
            {"id": f"p{i}", "title": f"Page {i}", "version": {"number": 1}}
            for i in range(100)
        ]
    }
    second_page = {"results": [{"id": "222", "title": TARGET_TITLE, "version": {"number": 2}}]}

    def get_handler(url, params):
        if int(params.get("start", 0) or 0) == 100:
            return FakeResponse(200, second_page)
        return FakeResponse(200, first_page)

    fake = FakeClient(
        get_handler=get_handler,
        put_handler=lambda url: FakeResponse(200, {"id": "222"}),
    )
    conf_module = _patch_confluence_http(monkeypatch, fake)

    result = await conf_module.confluence_client.create_or_update_page(
        title=TARGET_TITLE,
        body="<p>content</p>",
        parent_page_id=PARENT_ID,
        pat=PAT,
        confluence_base_url=CONF_BASE,
        space_key="TEST",
    )

    assert result == "222"
    # Было минимум два запроса GET (пагинация)
    assert sum(1 for m, url in fake.calls if m == "GET") >= 2
    assert ("PUT", f"{CONF_BASE}/rest/api/content/222") in fake.calls
