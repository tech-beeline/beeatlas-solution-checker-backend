# Copyright (c) 2024 PJSC VimpelCom
"""
Бизнес-логика "живого" поиска по каталогу (эксперимент feature/exp-bc-search).

Заменяет выявление business capability через LLM-промпт с встроенным каталогом
на поиск в живом каталоге через fdm-search (`search_capability_v2` c
`entity_type="bc"`). Работает на каждый выделенный TC-кандидат:
  1. поиск BC в каталоге по тексту кандидата (entity_type="bc"),
  2. для каждого из top-N BC — поиск TC в этом BC (entity_type="tc", domain=<код BC>).

Один код BC в domain даёт авторитетную группировку: каждый найденный TC лежит
именно в том BC, под заголовком которого показывается пользователю.

Все функции stateless. Падение шлюза (fdm-search) — graceful: возвращаем пустые
списки, не роняем job.
"""

import asyncio
import logging
import time
from typing import Optional

from app.config import settings
from app.integrations.beeatlas import search_capability_v2

logger = logging.getLogger("hld-agent")

# Максимальная длина всего поискового запроса (имя + описание): длинные запросы
# дают пустую выдачу, а длинный URL провоцирует HTTP 413 на шлюзе (как core/tc.py).
_MAX_SEARCH_CONTEXT_CHARS = 200

_BC_TOP_K_MAX = 20
_TC_TOP_K_MAX = 20

# Ограничение одновременных запросов к шлюзу внутри одного кандидата.
# ВАЖНО: форма BC-пейлоада не проверена у живого шлюза — нормализация ниже
# (_normalize_bc) дефенсивная, единственное место, где решается отображение BC.
_BC_HTTP_CONCURRENCY = 5


def _clamp_top_k(value: int, maximum: int) -> int:
    return max(1, min(maximum, value))


def _build_search_text(name: str, description: str = "") -> str:
    """Имя + описание через запятую, обрезано до лимита (см. core/tc.py)."""
    parts = [name.strip()]
    if description:
        parts.append(description.strip())
    text = ", ".join(parts)[:_MAX_SEARCH_CONTEXT_CHARS]
    return text.strip()


def _normalize_bc(item: dict) -> dict:
    """
    Приводит результат fdm-search к BC-форме {code, name, description, score, ...}.

    search_capability_v2 отдаёт нормализованные записи {code, name, description,
    score, system_*, domain_codes}. У BC-сущностей системных полей нет (None),
    а текстовое наполнение каталога BC живёт в description (в каталоге записи
    вида code + description + parent + path). Поэтому здесь, если name пуст —
    используем description как заголовок, и наоборот.
    """
    code = (item.get("code") or "").strip()
    if not code:
        return None

    name = (item.get("name") or "").strip()
    description = (item.get("description") or "").strip()
    if not name and description:
        name = description
    elif not description and name:
        description = name

    try:
        score = float(item.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0

    return {
        "code": code,
        "name": name,
        "description": description,
        "score": score,
        "synonyms": item.get("synonyms", []),
        "domain_codes": item.get("domain_codes", []),
    }


def _normalize_tc(item: dict) -> dict:
    """TC-запись ландшафта: {code, name, description, score, system_code, system_name}."""
    code = (item.get("code") or "").strip()
    if not code:
        return None

    name = (item.get("name") or "").strip()
    description = (item.get("description") or "").strip()

    return {
        "code": code,
        "name": name or description,
        "description": description,
        "score": float(item.get("score") or 0),
        "system_code": item.get("system_alias") or item.get("system_code"),
        "system_name": item.get("system_name"),
    }


async def search_business_capabilities(
    query: str = "",
    top_k: int = 10,
) -> list[dict]:
    """
    Поиск business capability в живом каталоге через fdm-search (entity_type="bc").

    domain/exclude_systems не передаются — для BC-сущностей они бессмысленны.
    """
    query = (query or "").strip()
    if not query:
        logger.warning("Empty BC search query, skipping fdm-search")
        return []

    top_k = _clamp_top_k(top_k, _BC_TOP_K_MAX)
    logger.info("Searching BC catalog (fdm-search): %s (top_k=%d)", query, top_k)

    try:
        results = await search_capability_v2(
            query=query,
            top_k=top_k,
            entity_type="bc",
        )
    except Exception as e:
        logger.warning("FDM BC search failed for '%s': %s", query, str(e))
        return []

    # Защита от кривого ответа шлюза: None/не список/не dict-элементы пропускаем.
    bcs: list[dict] = []
    for raw in (results or []):
        if not isinstance(raw, dict):
            continue
        bc = _normalize_bc(raw)
        if bc is not None:
            bcs.append(bc)

    logger.info("BC catalog search found %d BC for '%s'", len(bcs), query)
    return bcs


async def search_tc_for_bc(
    candidate_name: str,
    candidate_description: str,
    bc_code: str,
    top_k: int = 5,
) -> list[dict]:
    """
    Поиск TC в конкретном BC (domain=bc_code, entity_type="tc").

    domain = один код BC — авторитетная группировка: вернувшиеся TC принадлежат
    этому BC (та же семантика, что и _domain_from_business_capabilities в api/tc.py).
    """
    text = candidate_description #_build_search_text(candidate_name, candidate_description)
    if not text:
        logger.warning("Empty search query, skipping TC search for BC %s", bc_code)
        return []

    top_k = _clamp_top_k(top_k, _TC_TOP_K_MAX)
    logger.info(
        "Searching TC in BC %s (fdm-search): %s (top_k=%d)",
        bc_code, text, top_k,
    )

    try:
        results = await search_capability_v2(
            query=text,
            top_k=top_k,
            exclude_systems=settings.FDM_SEARCH_EXCLUDE_SYSTEMS or None,
            parents=bc_code,
        )
    except Exception as e:
        logger.warning("FDM TC search for BC %s failed: %s", bc_code, str(e))
        return []

    # Защита от кривого ответа шлюза: None/не список/не dict-элементы пропускаем.
    raw_tcs: list[dict] = []
    for raw in (results or []):
        if not isinstance(raw, dict):
            continue
        tc = _normalize_tc(raw)
        if tc is not None:
            raw_tcs.append(tc)

    # Дедуп по коду в рамках одной BC-группы (вход уже отсортирован по score desc).
    seen: set[str] = set()
    unique_tcs: list[dict] = []
    for t in raw_tcs:
        if t["code"] in seen:
            continue
        seen.add(t["code"])
        unique_tcs.append(t)

    logger.info("Found %d TC in BC %s for '%s'", len(unique_tcs), bc_code, text)
    return unique_tcs


async def analyze_tc_candidates(
    candidates: list[dict],
    bc_top_k: int = 10,
    tc_top_k: int = 5,
    progress_callback=None,
) -> dict:
    """
    Полный анализ кандидатов по живому каталогу:
      кандидат -> поиск BC (entity_type="bc") -> для top-N BC поиск их TC.

    Кандидаты обрабатываются последовательно (прогресс осмысленный, нагрузка на
    шлюз ограничена). Внутри кандидата BC-группы ищутся конкурентно
    (asyncio.Semaphore ограничивает одновременные запросы к шлюзу).

    Возвращает:
    {
      "results": [
        {
          "candidate_name": str,
          "query": str,
          "bcs": [ {code, name, description, score, synonyms, domain_codes,
                    tcs: [ {code, name, description, score, system_code, system_name} ]} ]
        }
      ],
      "elapsed_ms": int,
    }
    """
    bc_top_k = _clamp_top_k(bc_top_k, _BC_TOP_K_MAX)
    tc_top_k = _clamp_top_k(tc_top_k, _TC_TOP_K_MAX)

    total = len(candidates)
    results: list[dict] = []
    start_ts = time.time()
    semaphore = asyncio.Semaphore(_BC_HTTP_CONCURRENCY)

    async def _search_bc_group(bc: dict, name: str, description: str) -> dict:
        async with semaphore:
            tcs = await search_tc_for_bc(name, description, bc["code"], top_k=tc_top_k)
        group = dict(bc)
        group["tcs"] = tcs
        return group

    for i, candidate in enumerate(candidates):
        name = (candidate.get("name") or "").strip()
        description = (candidate.get("description") or "").strip()
        query = _build_search_text(name, description)

        if not name:
            logger.warning("Skip candidate %d: empty name", i)
            continue

        if progress_callback:
            await progress_callback({
                "phase": "analyzing",
                "current_tc": i,
                "total_tc": total,
                "current_tc_name": name,
                "current_bc": 0,
                "total_bc": 0,
                "current_bc_code": "",
                "current_bc_name": "",
                "elapsed_ms": int((time.time() - start_ts) * 1000),
            })

        logger.info(
            "Landscape analyze: %d/%d candidate '%s' — searching BC catalog",
            i + 1, total, name,
        )

        bcs = await search_business_capabilities(query=query, top_k=bc_top_k)

        if not bcs:
            logger.warning(
                "Landscape analyze: no BC found for candidate '%s'", name,
            )
            results.append({"candidate_name": name, "query": query, "bcs": []})
            continue

        # BC найдены: сначала сообщаем план (total_bc известен до поиска TC), затем
        # тикаем на КАЖДУЮ завершённую BC-группу — фронт видит живой прогресс
        # «проверено BC N из M». Инкремент счётчика и эмиссия — строго под lock,
        # чтобы при конкурентном завершении групп прогресс был строго монотонным
        # (без «регресса»: последний эмитченный тик несёт done_bc == total_bc).
        total_bc = len(bcs)
        done_bc = 0
        emit_lock = asyncio.Lock()

        if progress_callback:
            await progress_callback({
                "phase": "tc_searching",
                "current_tc": i,
                "total_tc": total,
                "current_tc_name": name,
                "current_bc": 0,
                "total_bc": total_bc,
                "current_bc_code": "",
                "current_bc_name": "",
                "elapsed_ms": int((time.time() - start_ts) * 1000),
            })

        async def _search_one(bc: dict) -> dict:
            nonlocal done_bc
            # _search_bc_group сам берёт общий semaphore (ограничение запросов к шлюзу)
            group = await _search_bc_group(bc, name, description)
            async with emit_lock:
                done_bc += 1
                if progress_callback:
                    await progress_callback({
                        "phase": "tc_searching",
                        "current_tc": i,
                        "total_tc": total,
                        "current_tc_name": name,
                        "current_bc": done_bc,
                        "total_bc": total_bc,
                        "current_bc_code": bc.get("code", ""),
                        "current_bc_name": bc.get("name", ""),
                        "elapsed_ms": int((time.time() - start_ts) * 1000),
                    })
            return group

        groups = await asyncio.gather(*(_search_one(bc) for bc in bcs))

        logger.info(
            "Landscape analyze: candidate '%s' -> %d BC groups (%dms)",
            name, len(groups), int((time.time() - start_ts) * 1000),
        )

        results.append({"candidate_name": name, "query": query, "bcs": groups})

    elapsed_ms = int((time.time() - start_ts) * 1000)

    if progress_callback:
        await progress_callback({
            "phase": "done",
            "current_tc": total,
            "total_tc": total,
            "current_tc_name": "",
            "current_bc": 0,
            "total_bc": 0,
            "current_bc_code": "",
            "current_bc_name": "",
            "elapsed_ms": elapsed_ms,
        })

    logger.info("Landscape analyze complete: %d candidates, %dms", total, elapsed_ms)

    return {
        "results": results,
        "elapsed_ms": elapsed_ms,
    }
