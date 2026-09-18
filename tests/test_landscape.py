# Copyright (c) 2024 PJSC VimpelCom
"""
Тесты для "живого" поиска по каталогу (app/core/landscape.py + app/api/landscape.py).

Эксперимент feature/exp-bc-search: поиск BC (entity_type="bc") и TC в их домене.
Старый API /api/bc и /api/tc этими тестами не затрагивается.
"""

import asyncio

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core import landscape as landscape_core


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# --- Хелпер: фейковый search_capability_v2, записывающий kwargs ---


def _make_fake_search(by_domain):
    """Возвращает async-функцию, раздающую выдачу по entity_type/domain.

    by_domain: {"BC-1": [tc_items...], ...} — выдача для entity_type="tc".
    Для entity_type="bc" возвращает константный список из двух BC.
    """
    async def fake(query="", top_k=10, code=None, exclude_systems=None,
                   domain=None, llm_rerank=False, entity_type="tc"):
        if entity_type == "bc":
            return [
                {"code": "BC-1", "name": "", "description": "Заказы",
                 "score": 0.9, "system_id": None, "system_name": None,
                 "system_alias": None, "domain_codes": []},
                {"code": "BC-2", "name": "Доставка", "description": "Логистика",
                 "score": 0.8, "system_id": None, "system_name": None,
                 "system_alias": None, "domain_codes": []},
            ]
        return list(by_domain.get(domain, []))
    return fake


def _tc(code, name, system_code=None, system_name=None, score=0.9):
    return {
        "code": code, "name": name, "description": "",
        "score": score,
        "system_id": None,
        "system_name": system_name,
        "system_alias": system_code,
        "domain_codes": [],
    }


# --- Unit: search_business_capabilities ---


@pytest.mark.asyncio
async def test_search_business_capabilities_passes_entity_type_bc(monkeypatch):
    captured = {}

    async def fake(query="", top_k=10, code=None, exclude_systems=None,
                   domain=None, llm_rerank=False, entity_type="tc"):
        captured.update(
            query=query, top_k=top_k, domain=domain,
            exclude_systems=exclude_systems, entity_type=entity_type,
        )
        return []

    monkeypatch.setattr(landscape_core, "search_capability_v2", fake)

    await landscape_core.search_business_capabilities("Управление заказами", top_k=5)
    assert captured["entity_type"] == "bc"
    assert captured["top_k"] == 5
    assert captured["domain"] is None
    assert captured["exclude_systems"] is None


@pytest.mark.asyncio
async def test_search_business_capabilities_defensive_normalization(monkeypatch):
    """Пустой name -> берём description; записи без code отбрасываются."""
    async def fake(query="", top_k=10, **kwargs):
        return [
            {"code": "BC-1", "name": "", "description": "Кадровый учёт", "score": 0.9},
            {"code": "BC-2", "name": "Закупки", "description": "", "score": 0.7},
            {"code": "", "name": "Без кода", "description": "отброс", "score": 0.5},
        ]

    monkeypatch.setattr(landscape_core, "search_capability_v2", fake)

    bcs = await landscape_core.search_business_capabilities("кадры", top_k=5)
    assert [b["code"] for b in bcs] == ["BC-1", "BC-2"]
    assert bcs[0]["name"] == "Кадровый учёт"  # fallback name из description
    assert bcs[0]["description"] == "Кадровый учёт"
    assert bcs[1]["name"] == "Закупки"
    assert bcs[1]["description"] == "Закупки"  # fallback description из name
    assert bcs[0]["score"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_search_tc_for_bc_passes_domain_and_maps_system(monkeypatch):
    captured = {}

    async def fake(query="", top_k=10, code=None, exclude_systems=None,
                   domain=None, llm_rerank=False, entity_type="tc"):
        captured.update(query=query, top_k=top_k, domain=domain,
                        exclude_systems=exclude_systems)
        return [_tc("TC-A1", "Управление заказами", system_code="ORD",
                    system_name="Система заказов")]

    monkeypatch.setattr(landscape_core, "search_capability_v2", fake)

    tcs = await landscape_core.search_tc_for_bc(
        "Управление заказами", "", "BC-1", top_k=4
    )
    assert captured["domain"] == "BC-1"
    assert captured["top_k"] == 4
    assert len(tcs) == 1
    assert tcs[0]["code"] == "TC-A1"
    assert tcs[0]["system_code"] == "ORD"
    assert tcs[0]["system_name"] == "Система заказов"


# --- Unit: analyze_tc_candidates ---


@pytest.mark.asyncio
async def test_analyze_tc_candidates_structure_and_domain(monkeypatch):
    """Каждый bcs[i].tcs получен поиском с domain == bcs[i].code."""
    by_domain = {
        "BC-1": [_tc("TC-A1", "Управление заказами", system_code="ORD")],
        "BC-2": [_tc("TC-D1", "Доставка", system_code="DEL")],
    }
    monkeypatch.setattr(
        landscape_core, "search_capability_v2", _make_fake_search(by_domain)
    )

    result = await landscape_core.analyze_tc_candidates(
        [{"name": "Управление заказами", "description": "D"}],
        bc_top_k=2, tc_top_k=3,
    )
    assert len(result["results"]) == 1
    entry = result["results"][0]
    assert entry["candidate_name"] == "Управление заказами"
    assert len(entry["bcs"]) == 2
    assert [b["code"] for b in entry["bcs"]] == ["BC-1", "BC-2"]
    assert [b["code"] for b in entry["bcs"][0]["tcs"]] == ["TC-A1"]
    assert [b["code"] for b in entry["bcs"][1]["tcs"]] == ["TC-D1"]
    assert entry["bcs"][0]["tcs"][0]["system_code"] == "ORD"
    assert "elapsed_ms" in result


@pytest.mark.asyncio
async def test_analyze_tc_candidates_dedups_tc_by_code(monkeypatch):
    """Один и тот же код TC в одной BC-группе не дублируется."""

    async def fake(query="", top_k=10, **kwargs):
        if kwargs.get("entity_type") == "bc":
            return [{"code": "BC-1", "name": "", "description": "Заказы",
                     "score": 0.9}]
        return [
            _tc("TC-A1", "Управление заказами", system_code="ORD"),
            _tc("TC-A1", "Управление заказами", system_code="ORD"),
            _tc("TC-A2", "История заказов", system_code="ORD"),
        ]

    monkeypatch.setattr(landscape_core, "search_capability_v2", fake)

    result = await landscape_core.analyze_tc_candidates(
        [{"name": "Заказы", "description": ""}], bc_top_k=1, tc_top_k=5
    )
    tcs = result["results"][0]["bcs"][0]["tcs"]
    assert [t["code"] for t in tcs] == ["TC-A1", "TC-A2"]


@pytest.mark.asyncio
async def test_analyze_tc_candidates_malformed_payloads(monkeypatch):
    """None/не список/не dict от шлюза -> пустые группы, без исключений."""

    async def fake(query="", top_k=10, **kwargs):
        if kwargs.get("entity_type") == "bc":
            return None  # шлюз вернул None
        return ["junk", None, {"code": "TC-X", "name": "X", "score": 0.5}]

    monkeypatch.setattr(landscape_core, "search_capability_v2", fake)

    result = await landscape_core.analyze_tc_candidates(
        [{"name": "Кадры", "description": ""}], bc_top_k=5, tc_top_k=5
    )
    assert len(result["results"]) == 1
    assert result["results"][0]["bcs"] == []


@pytest.mark.asyncio
async def test_analyze_tc_candidates_live_bc_ticks(monkeypatch):
    """На каждую найденную BC-группу уходит отдельный тик прогресса:
    current_bc строго монотонен 1->2->3 с именем текущего BC; total_bc известен заранее."""
    events = []

    async def fake(query="", top_k=10, **kwargs):
        if kwargs.get("entity_type") == "bc":
            return [
                {"code": "BC-1", "name": "", "description": "Заказы", "score": 0.9},
                {"code": "BC-2", "name": "Доставка", "description": "", "score": 0.8},
                {"code": "BC-3", "name": "Возвраты", "description": "", "score": 0.7},
            ]
        return {
            "BC-1": [_tc("TC-A1", "a")],
            "BC-2": [_tc("TC-D1", "d")],
            "BC-3": [],
        }.get(kwargs.get("domain"), [])

    monkeypatch.setattr(landscape_core, "search_capability_v2", fake)

    async def cb(progress: dict):
        events.append(dict(progress))

    result = await landscape_core.analyze_tc_candidates(
        [{"name": "Заказы", "description": ""}], bc_top_k=5, tc_top_k=3,
        progress_callback=cb,
    )
    assert len(result["results"]) == 1
    assert len(result["results"][0]["bcs"]) == 3

    # pre-tick поиска BC: total_bc ещё неизвестен (0)
    bc_search = [e for e in events if e["phase"] == "analyzing"]
    assert len(bc_search) == 1
    assert bc_search[0]["total_bc"] == 0
    assert bc_search[0]["current_bc_name"] == ""

    # тик «BC найдены»: план известен, работа ещё не начата
    found = [e for e in events if e["phase"] == "tc_searching" and e["current_bc"] == 0]
    assert len(found) == 1
    assert found[0]["total_bc"] == 3

    # тики на группы: строго монотонный current_bc + имя текущего BC
    ticks = [e for e in events if e["phase"] == "tc_searching" and e["current_bc"] > 0]
    assert [t["current_bc"] for t in ticks] == [1, 2, 3]
    assert all(t["total_bc"] == 3 for t in ticks)
    assert all(t["current_tc_name"] == "Заказы" for t in ticks)
    names = {t["current_bc_code"]: t["current_bc_name"] for t in ticks}
    assert names["BC-1"] == "Заказы"    # fallback name из description
    assert names["BC-2"] == "Доставка"
    assert names["BC-3"] == "Возвраты"

    # у всех событий есть новое поле; финальный done эмитится
    assert all("current_bc_name" in e for e in events)
    done = [e for e in events if e["phase"] == "done"]
    assert len(done) == 1
    assert done[0]["total_tc"] == 1


@pytest.mark.asyncio
async def test_analyze_tc_candidates_no_bc_no_tc_ticks(monkeypatch):
    """BC не найдены -> кандидат с пустым bcs, тиков tc_searching нет, done есть."""

    async def fake(query="", top_k=10, **kwargs):
        if kwargs.get("entity_type") == "bc":
            return []
        return []

    monkeypatch.setattr(landscape_core, "search_capability_v2", fake)

    events = []

    async def cb(progress: dict):
        events.append(dict(progress))

    result = await landscape_core.analyze_tc_candidates(
        [{"name": "Заказы", "description": ""}], bc_top_k=5, tc_top_k=3,
        progress_callback=cb,
    )
    assert result["results"][0]["bcs"] == []
    assert not any(e["phase"] == "tc_searching" for e in events)
    assert any(e["phase"] == "done" for e in events)


# --- HTTP: /api/landscape/analyze/* lifecycle ---


@pytest.mark.asyncio
async def test_analyze_endpoint_full_flow(client, monkeypatch):
    by_domain = {
        "BC-1": [_tc("TC-A1", "Управление заказами", system_code="ORD",
                     system_name="Система заказов")],
        "BC-2": [],
    }
    monkeypatch.setattr(
        landscape_core, "search_capability_v2", _make_fake_search(by_domain)
    )

    resp = await client.post(
        "/api/landscape/analyze/start",
        json={"candidates": [{"name": "Управление заказами", "description": "D"}],
              "bc_top_k": 2, "tc_top_k": 3},
    )
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    assert task_id

    max_attempts = 20
    for _ in range(max_attempts):
        pr = await client.get(f"/api/landscape/analyze/{task_id}/progress")
        assert pr.status_code == 200
        if pr.json()["done"]:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("Landscape analyze task did not complete in time")

    rr = await client.get(f"/api/landscape/analyze/{task_id}/result")
    assert rr.status_code == 200
    data = rr.json()
    assert "elapsed_ms" in data
    assert len(data["results"]) == 1
    entry = data["results"][0]
    assert entry["candidate_name"] == "Управление заказами"
    assert len(entry["bcs"]) == 2
    bc1 = entry["bcs"][0]
    assert bc1["code"] == "BC-1"
    assert bc1["name"] == "Заказы"  # fallback из description
    assert bc1["tcs"][0]["code"] == "TC-A1"
    assert bc1["tcs"][0]["system_code"] == "ORD"
    assert entry["bcs"][1]["tcs"] == []


@pytest.mark.asyncio
async def test_analyze_start_empty_candidates(client):
    resp = await client.post(
        "/api/landscape/analyze/start", json={"candidates": []}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_analyze_start_empty_names(client):
    resp = await client.post(
        "/api/landscape/analyze/start",
        json={"candidates": [{"name": "  ", "description": ""}]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_analyze_progress_and_result_not_found(client):
    assert (await client.get("/api/landscape/analyze/nope/progress")).status_code == 404
    assert (await client.get("/api/landscape/analyze/nope/result")).status_code == 404


# --- HTTP: /api/landscape/bc/search (sync type-ahead) ---


@pytest.mark.asyncio
async def test_bc_search_sync(client, monkeypatch):
    from app.api import landscape as landscape_api

    async def fake_bc_search(query="", top_k=10):
        return [
            {"code": "BC-1", "name": "Кадровый учёт", "description": "x", "score": 0.9},
            {"code": "BC-2", "name": "Закупки", "description": "y", "score": 0.7},
        ]

    # api.landscape импортирует search_business_capabilities в своё пространство имён —
    # патчим привязку роутера, а не модуль ядра.
    monkeypatch.setattr(landscape_api, "search_business_capabilities", fake_bc_search)

    resp = await client.post(
        "/api/landscape/bc/search", json={"query": "кадры", "top_k": 8}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 2
    assert data["results"][0]["code"] == "BC-1"
    assert data["results"][0]["score"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_bc_search_empty_query(client):
    resp = await client.post("/api/landscape/bc/search", json={"query": "  "})
    assert resp.status_code == 400
