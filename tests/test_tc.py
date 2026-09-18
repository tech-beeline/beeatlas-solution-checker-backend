# Copyright (c) 2024 PJSC VimpelCom
"""
Тесты для Technical Capability API (stateless).
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


SAMPLE_FR = [
    {"id": "FR-001", "type": "FR", "title": "Управление заказами", "description": "Система должна позволять создавать и управлять заказами клиентов"},
    {"id": "FR-002", "type": "FR", "title": "Обработка платежей", "description": "Система должна обрабатывать платежи через внешний шлюз"},
    {"id": "NFR-001", "type": "NFR", "title": "Производительность", "description": "Время ответа не более 2 секунд"},
]


@pytest.mark.asyncio
async def test_identify_start_no_fr(client):
    """POST /api/tc/identify/start без FR -> 400."""
    response = await client.post(
        "/api/tc/identify/start", json={"structured_requirements": []}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_identify_start_with_fr(client):
    """POST /api/tc/identify/start с FR -> 200 + task_id."""
    response = await client.post(
        "/api/tc/identify/start",
        json={"structured_requirements": SAMPLE_FR},
    )
    assert response.status_code == 200
    data = response.json()
    assert "task_id" in data
    assert len(data["task_id"]) > 0


@pytest.mark.asyncio
async def test_identify_progress_not_found(client):
    """GET /api/tc/identify/{task_id}/progress с несуществующим task_id -> 404."""
    response = await client.get("/api/tc/identify/non-existent/progress")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_identify_result_not_found(client):
    """GET /api/tc/identify/{task_id}/result с несуществующим task_id -> 404."""
    response = await client.get("/api/tc/identify/non-existent/result")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_identify_full_flow(client):
    """
    Полный цикл: start -> progress -> result.
    """
    # 1. Start
    response = await client.post(
        "/api/tc/identify/start",
        json={"structured_requirements": SAMPLE_FR},
    )
    assert response.status_code == 200
    task_id = response.json()["task_id"]

    # 2. Poll progress until done
    import asyncio
    max_attempts = 30
    for _ in range(max_attempts):
        resp = await client.get(f"/api/tc/identify/{task_id}/progress")
        assert resp.status_code == 200
        data = resp.json()
        if data["done"]:
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("TC identify task did not complete in time")

    # 3. Get result
    resp = await client.get(f"/api/tc/identify/{task_id}/result")
    assert resp.status_code == 200
    data = resp.json()
    # New TC-centric format: response has "candidates" (list of TC entities with fr_ids)
    assert "candidates" in data
    assert isinstance(data["candidates"], list)
    if len(data["candidates"]) > 0:
        candidate = data["candidates"][0]
        assert "name" in candidate
        assert "description" in candidate
        assert "fr_ids" in candidate
        assert isinstance(candidate["fr_ids"], list)


@pytest.mark.asyncio
async def test_search_no_session(client):
    """POST /api/tc/search -> 200 (fdm-search returns results or empty)."""
    response = await client.post(
        "/api/tc/search",
        json={"tc_candidate_name": "Управление заказами"},
    )
    # fdm-search returns results if configured in .env, or empty list
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert isinstance(data["results"], list)


def test_domain_from_business_capabilities():
    """Преобразование списка BC в строку domain для fdm-search."""
    from app.api.tc import _domain_from_business_capabilities

    assert _domain_from_business_capabilities([]) == ""
    assert (
        _domain_from_business_capabilities(
            [{"code": "BC-019652", "description": "x"}, {"code": "DMN.092"}]
        )
        == "BC-019652,DMN.092"
    )
    # Пустые и не-dict коды пропускаются
    assert _domain_from_business_capabilities([{"code": ""}, {"code": "BC-1"}]) == "BC-1"
    assert _domain_from_business_capabilities([None, "BC-2"]) == ""


@pytest.mark.asyncio
async def test_search_tc_truncates_long_context(monkeypatch):
    """Весь поисковый запрос (имя + описание) обрезается до 200 символов."""
    from app.core import tc as tc_module

    captured: dict = {}

    async def fake_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(tc_module, "search_capability_v2", fake_search)

    long_desc = "А" * 1000
    await tc_module.search_tc_on_landscape("Название TC очень длинное и подробное", tc_description=long_desc)
    query = captured.get("query", "")
    assert len(query) <= 200


@pytest.mark.asyncio
async def test_search_tc_ignores_task_description(monkeypatch):
    """task_description не попадает в поисковый запрос — только имя и описание TC."""
    from app.core import tc as tc_module

    captured: dict = {}

    async def fake_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(tc_module, "search_capability_v2", fake_search)

    await tc_module.search_tc_on_landscape(
        "Название TC",
        tc_description="Описание TC",
    )
    query = captured.get("query", "")
    assert query == "Название TC, Описание TC"


@pytest.mark.asyncio
async def test_search_tc_includes_rationale(monkeypatch):
    """rationale попадает в поисковый запрос сразу после имени (до описания)."""
    from app.core import tc as tc_module

    captured: dict = {}

    async def fake_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(tc_module, "search_capability_v2", fake_search)

    await tc_module.search_tc_on_landscape(
        "Название TC",
        tc_description="Описание TC",
        tc_rationale="Обоснование TC: задействованы сервисы X",
    )
    query = captured.get("query", "")
    assert query.startswith("Название TC, Обоснование TC: задействованы сервисы X,")


@pytest.mark.asyncio
async def test_mass_search_forwards_rationale(monkeypatch):
    """mass_search_tc передаёт rationale кандидата в поисковый запрос."""
    from app.core import tc as tc_module

    captured: dict = {}

    async def fake_search_on_landscape(**kwargs):
        captured.update(kwargs)
        return [], 0, False

    monkeypatch.setattr(
        tc_module, "search_tc_on_landscape", fake_search_on_landscape
    )

    result = await tc_module.mass_search_tc(
        [{"name": "TC-A", "description": "Описание A", "rationale": "Обоснование A"}]
    )

    assert captured.get("tc_rationale") == "Обоснование A"
    assert result["results"]["TC-A"] == []


@pytest.mark.asyncio
async def test_search_tc_passes_domain(monkeypatch):
    """search_tc_on_landscape передаёт domain в search_capability_v2."""
    from app.core import tc as tc_module

    captured: dict = {}

    async def fake_search(**kwargs):
        captured.update(kwargs)
        return []

    # core/tc.py импортирует search_capability_v2 напрямую — мокаем атрибут модуля
    monkeypatch.setattr(tc_module, "search_capability_v2", fake_search)

    await tc_module.search_tc_on_landscape("Управление заказами", domain="BC-019652,DMN.092")
    assert captured.get("domain") == "BC-019652,DMN.092"


@pytest.mark.asyncio
async def test_search_tc_empty_domain_not_passed(monkeypatch):
    """При пустом domain параметр не передаётся (None)."""
    from app.core import tc as tc_module

    captured: dict = {}

    async def fake_search(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(tc_module, "search_capability_v2", fake_search)

    await tc_module.search_tc_on_landscape("Управление заказами", domain="")
    assert "domain" in captured
    assert captured.get("domain") is None


def test_max_tokens_clamped(monkeypatch):
    """max_tokens клампируется до контекста модели с учётом размера входа."""
    from app.integrations.llm_client import (
        llm_client,
        _MODEL_CONTEXT_TOKENS,
        _CHARS_PER_TOKEN,
    )

    monkeypatch.setattr(llm_client, "max_tokens", 500000)
    # Без входа: max_tokens не может превысить контекст модели
    assert llm_client._max_tokens_for_request() == _MODEL_CONTEXT_TOKENS

    # Вход размером ~весь контекст: выход сводится к минимуму (1 токен)
    big_input = _MODEL_CONTEXT_TOKENS * _CHARS_PER_TOKEN
    assert llm_client._max_tokens_for_request(input_chars=big_input) == 1

    monkeypatch.setattr(llm_client, "max_tokens", 16000)
    assert llm_client._max_tokens_for_request() == 16000


def test_chunk_fr_list():
    from app.core.tc import _chunk_fr_list

    frs = [{"id": f"FR-{i}"} for i in range(1, 8)]
    chunks = _chunk_fr_list(frs, 3)
    assert [len(c) for c in chunks] == [3, 3, 1]
    assert chunks[0][0]["id"] == "FR-1"
    assert chunks[2][0]["id"] == "FR-7"


def test_parse_tc_result_score():
    """_parse_tc_result извлекает score и нормализует битые/отсутствующие значения в 0."""
    from app.core.tc import _parse_tc_result

    raw = (
        '[{"name":"TC-1","description":"d","rationale":"r","score":87,"fr_ids":["FR-1"]},'
        '{"name":"TC-2","description":"d","rationale":"r","score":"95%","fr_ids":["FR-2"]},'
        '{"name":"TC-3","description":"d","rationale":"r","fr_ids":["FR-3"]}]'
    )
    result = _parse_tc_result(raw)
    assert len(result) == 3
    assert result[0]["score"] == 87.0
    # "95%" — не число: не падаем, нормализуем в 0
    assert result[1]["score"] == 0.0
    # поле отсутствует — дефолт 0
    assert result[2]["score"] == 0.0


@pytest.mark.asyncio
async def test_identify_tc_single_when_small(monkeypatch):
    """FR меньше порога — один запрос identify, без чанков и дедупликации."""
    from app.core.tc import identify_tc
    from app.integrations.llm_client import llm_client

    calls: list[str] = []

    async def fake_llm(system_prompt, user_message, prompt_name="unknown"):
        calls.append(prompt_name)
        return (
            '[{"name":"TC-1","description":"d","rationale":"r","fr_ids":["FR-1"]}]',
            1,
            False,
        )

    monkeypatch.setattr(llm_client, "chat_completion_with_stats", fake_llm)

    result = await identify_tc(structured_requirements=SAMPLE_FR)
    assert calls == ["identify_tc"]
    assert len(result) == 1


@pytest.mark.asyncio
async def test_identify_tc_chunked(monkeypatch):
    """FR больше порога — чанки по 30 + финальная дедупликация (merge_tc)."""
    from app.core import tc as tc_module
    from app.integrations.llm_client import llm_client

    frs = [
        {"id": f"FR-{i}", "type": "FR", "title": f"T{i}", "description": f"D{i}"}
        for i in range(1, 66)  # 65 FR -> 30 + 30 + 5
    ]

    calls: list[str] = []

    async def fake_llm(system_prompt, user_message, prompt_name="unknown"):
        calls.append(prompt_name)
        if prompt_name == "identify_tc":
            return (
                '[{"name":"TC raw","description":"d","rationale":"r","fr_ids":["FR-1"]}]',
                1,
                False,
            )
        # merge_tc
        return (
            '[{"name":"TC merged","description":"d","rationale":"r","fr_ids":["FR-1","FR-2"]}]',
            1,
            False,
        )

    monkeypatch.setattr(llm_client, "chat_completion_with_stats", fake_llm)
    monkeypatch.setattr(tc_module.settings, "TC_IDENTIFY_CHUNK_SIZE", 30)

    result = await tc_module.identify_tc(structured_requirements=frs)

    assert calls.count("identify_tc") == 3
    assert calls.count("merge_tc") == 1
    assert len(result) == 1
    assert result[0]["name"] == "TC merged"


def test_parse_tc_result_truncated_recovers_complete_objects():
    """_parse_tc_result спасает завершённые объекты из обрезанного JSON-массива."""
    from app.core.tc import _parse_tc_result

    # Модель упёрлась в completion cap: массив обрывается на середине 3-го объекта.
    truncated = (
        '[{"name":"TC-1","description":"d1","rationale":"r","fr_ids":["FR-1"],"score":70},'
        '{"name":"TC-2","description":"d2","rationale":"r","fr_ids":["FR-2"],"score":71},'
        '{"name":"TC-3","description":"d3",'
    )
    result = _parse_tc_result(truncated)
    # Оба полностью завершённых объекта восстановлены; незавершённый отброшен.
    assert [c["name"] for c in result] == ["TC-1", "TC-2"]


def test_parse_tc_result_ignores_inner_fr_ids_array():
    """Вложенный массив строк fr_ids не выдаётся за TC при битом внешнем JSON."""
    from app.core.tc import _parse_tc_result

    # Форма из реального лога: merge-ответ обрезан, внешний массив не завершён,
    # но внутри есть парсибельный массив строк fr_ids (давал ложное «Parsed 6 TC»).
    broken = (
        '[{"name":"A","fr_ids":'
        '["FR-11","FR-22","FR-33","FR-44","FR-55","FR-66"],'
    )
    # Внешний объект не закрыт -> главный кандидат не парсится; восстанавливать нечего
    # (нет завершённого }; массив строк не является TC) -> пусто, а не «6 кандидатов».
    assert _parse_tc_result(broken) == []


def test_merge_compact_and_rehydrate():
    """_compact_for_merge урезает тексты, _rehydrate_merge_result возвращает полные."""
    from app.core.tc import (
        _TC_MERGE_DESCRIPTION_CAP,
        _TC_MERGE_RATIONALE_CAP,
        _compact_for_merge,
        _rehydrate_merge_result,
    )

    source = [
        {
            "name": "TC-1",
            "description": "д" * 1000,
            "rationale": "р" * 1000,
            "score": 85,
            "fr_ids": ["FR-1", "FR-2"],
        }
    ]
    compact = _compact_for_merge(source)
    assert len(compact[0]["description"]) == _TC_MERGE_DESCRIPTION_CAP
    assert len(compact[0]["rationale"]) == _TC_MERGE_RATIONALE_CAP

    # Модель вернула урезанные тексты; реагидрация должна вернуть исходные полные.
    merged = [dict(compact[0])]
    out = _rehydrate_merge_result(merged, source)
    assert len(out[0]["description"]) == 1000
    assert len(out[0]["rationale"]) == 1000


@pytest.mark.asyncio
async def test_identify_tc_merge_fallback_to_chunks(monkeypatch):
    """merge_tc вернул пусто/бито — результат собирается из накопленных кандидатов чанков."""
    from app.core import tc as tc_module
    from app.integrations.llm_client import llm_client

    frs = [
        {"id": f"FR-{i}", "type": "FR", "title": f"T{i}", "description": f"D{i}"}
        for i in range(1, 66)  # 65 FR -> 30 + 30 + 5
    ]

    async def fake_llm(system_prompt, user_message, prompt_name="unknown"):
        if prompt_name == "identify_tc":
            return (
                '[{"name":"TC raw","description":"d","rationale":"r","fr_ids":["FR-1"]}]',
                1,
                False,
            )
        # merge_tc: невалидный обрезанный JSON
        return '[{"name":"TC-1","description":"', 1, False

    monkeypatch.setattr(llm_client, "chat_completion_with_stats", fake_llm)
    monkeypatch.setattr(tc_module.settings, "TC_IDENTIFY_CHUNK_SIZE", 30)

    result = await tc_module.identify_tc(structured_requirements=frs)
    # Fallback на кандидатов чанков (3 чанка по 1 кандидату); дубликаты не дедуплицируются
    assert len(result) == 3
    assert all(c["name"] == "TC raw" for c in result)


