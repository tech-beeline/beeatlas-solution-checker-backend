# Copyright (c) 2024 PJSC VimpelCom
"""
REST API этапа Technical Capability — выявление и поиск TC.
Все эндпоинты stateless — не используют хранилище сессий.
Выявление TC через LLM — асинхронное с long polling прогресса.
"""

import logging
import uuid
import asyncio
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.api.task_store import cleanup_expired
from app.core.tc import identify_tc, search_tc_on_landscape, mass_search_tc, generate_task_description
from app.integrations.beeatlas import get_systems

logger = logging.getLogger("hld-agent")

router = APIRouter()

# In-memory хранилище задач идентификации TC
# task_id -> { "progress": dict, "result": list[dict] | None, "error": str | None }
_identify_tasks: dict[str, dict] = {}

# In-memory хранилище задач массового поиска TC
_search_tasks: dict[str, dict] = {}


class IdentifyRequest(BaseModel):
    structured_requirements: list[dict]
    task_description: str = ""
    # Выбранные бизнес-возможности (BC): [{"code": "...", "description": "..."}]
    business_capabilities: list[dict] = []


class DescribeTaskRequest(BaseModel):
    raw_content: str


class DescribeTaskResponse(BaseModel):
    task_description: str


class TCCandidate(BaseModel):
    name: str
    description: str
    rationale: str
    # Скоринг соответствия TC задаче и покрываемым FR (0-100, от LLM)
    score: float = 0.0
    fr_ids: list[str]


class IdentifyResponse(BaseModel):
    candidates: list[TCCandidate]


class IdentifyStartResponse(BaseModel):
    task_id: str


class IdentifyProgressResponse(BaseModel):
    phase: str = "identifying"
    total_fr: int = 0
    total_candidates: int = 0
    data_chars: int = 0
    response_chars: int = 0
    attempts: int = 0
    elapsed_ms: int = 0
    # Чанкованный режим: текущий чанк / всего чанков и статистика дедупликации
    current_chunk: int = 0
    total_chunks: int = 0
    merge_input_chars: int = 0
    merge_attempts: int = 0
    done: bool = False
    error: Optional[str] = None


class SearchRequest(BaseModel):
    tc_candidate_name: str
    tc_description: str = ""
    # Обоснование TC-кандидата: попадает в поисковый запрос (сервисы/каналы
    # из identify_tc живут в rationale и помогают найти существующую TC).
    tc_rationale: str = ""
    # Выбранные бизнес-возможности (BC) — уходят в параметр domain fdm-search
    business_capabilities: list[dict] = []


class SearchResultItem(BaseModel):
    code: str
    name: str
    description: str
    score: float
    system_code: Optional[str] = None
    system_name: Optional[str] = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultItem]


def _domain_from_business_capabilities(business_capabilities: list[dict]) -> str:
    """
    Преобразует список выбранных BC (list[dict] с полем code) в строку domain
    для параметра fdm-search: коды через запятую. Пусто — если BC не выбраны.
    """
    codes = [
        bc.get("code", "")
        for bc in business_capabilities
        if isinstance(bc, dict) and bc.get("code")
    ]
    return ",".join(codes)


class SystemItem(BaseModel):
    """Система (продукт) ландшафта для назначения создаваемым TC."""
    code: str
    name: str
    description: str = ""


class SystemsResponse(BaseModel):
    systems: list[SystemItem]


@router.get("/systems", response_model=SystemsResponse)
async def api_get_systems(query: str = ""):
    """
    Список систем (продуктов) ландшафта из BeeAtlas для назначения create_new TC.
    query — необязательный фильтр по названию/коду (case-insensitive).
    """
    logger.info("GET /api/tc/systems | query=%s", query or "-")
    systems = await get_systems(query=query)
    return SystemsResponse(
        systems=[
            SystemItem(
                code=s.get("code", ""),
                name=s.get("name", ""),
                description=s.get("description", ""),
            )
            for s in systems
        ]
    )


@router.post("/describe-task", response_model=DescribeTaskResponse)
async def api_describe_task(req: DescribeTaskRequest):
    """
    Генерация краткого описания задачи из исходного текста требований.
    """
    logger.info("POST /api/tc/describe-task | %d chars", len(req.raw_content))

    if not req.raw_content or not req.raw_content.strip():
        raise HTTPException(status_code=400, detail="Нет текста для генерации описания задачи")

    try:
        description = await generate_task_description(raw_content=req.raw_content)
        return DescribeTaskResponse(task_description=description)
    except Exception as e:
        logger.error("Task description generation failed: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Ошибка генерации описания задачи: {str(e)}")


@router.post("/identify/start", response_model=IdentifyStartResponse)
async def api_identify_tc_start(req: IdentifyRequest):
    """
    Запуск выявления TC из FR через LLM в фоне.
    Возвращает task_id для long polling прогресса.
    """
    logger.info("POST /api/tc/identify/start | %d requirements, task_desc=%d chars",
                len(req.structured_requirements), len(req.task_description))

    if not req.structured_requirements:
        raise HTTPException(status_code=400, detail="Нет требований для выявления TC")

    # Удаляем истёкшие задачи до запуска новой (защита от роста памяти)
    cleanup_expired(_identify_tasks)

    task_id = str(uuid.uuid4())

    # Инициализируем задачу
    _identify_tasks[task_id] = {
        "created_at": time.time(),
        "progress": {
            "phase": "identifying",
            "total_fr": 0,
            "total_candidates": 0,
            "data_chars": 0,
            "response_chars": 0,
        },
        "result": None,
        "error": None,
    }

    async def progress_callback(progress: dict):
        _identify_tasks[task_id]["progress"] = progress

    async def run_identify():
        try:
            mappings = await identify_tc(
                structured_requirements=req.structured_requirements,
                task_description=req.task_description,
                business_capabilities=req.business_capabilities or None,
                progress_callback=progress_callback,
            )
            _identify_tasks[task_id]["result"] = mappings
        except Exception as e:
            logger.error("Identify TC task %s failed: %s", task_id, str(e))
            _identify_tasks[task_id]["error"] = str(e)

    # Запускаем в фоне
    asyncio.create_task(run_identify())

    return IdentifyStartResponse(task_id=task_id)


@router.get("/identify/{task_id}/progress", response_model=IdentifyProgressResponse)
async def api_identify_tc_progress(task_id: str):
    """
    Получение прогресса выявления TC (long polling).
    """
    task = _identify_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    progress = task["progress"]
    error = task.get("error")
    result = task.get("result")

    return IdentifyProgressResponse(
        phase=progress.get("phase", "identifying"),
        total_fr=progress.get("total_fr", 0),
        total_candidates=progress.get("total_candidates", 0),
        data_chars=progress.get("data_chars", 0),
        response_chars=progress.get("response_chars", 0),
        attempts=progress.get("attempts", 0),
        elapsed_ms=progress.get("elapsed_ms", 0),
        current_chunk=progress.get("current_chunk", 0),
        total_chunks=progress.get("total_chunks", 0),
        merge_input_chars=progress.get("merge_input_chars", 0),
        merge_attempts=progress.get("merge_attempts", 0),
        done=result is not None or error is not None,
        error=error,
    )


@router.get("/identify/{task_id}/result", response_model=IdentifyResponse)
async def api_identify_tc_result(task_id: str):
    """
    Получение результата выявления TC.
    """
    task = _identify_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    error = task.get("error")
    if error:
        raise HTTPException(status_code=400, detail=error)

    result = task.get("result")
    if result is None:
        raise HTTPException(status_code=425, detail="Задача ещё не завершена")


    return IdentifyResponse(
        candidates=[
            TCCandidate(
                name=c.get("name", ""),
                description=c.get("description", ""),
                rationale=c.get("rationale", ""),
                score=c.get("score", 0.0),
                fr_ids=c.get("fr_ids", []),
            )
            for c in result
        ]
    )


@router.post("/search", response_model=SearchResponse)
async def api_search_tc(req: SearchRequest):
    """Поиск TC на ландшафте через fdm-search (search_capability_v2)."""
    logger.info("POST /api/tc/search | query=%s", req.tc_candidate_name)

    domain = _domain_from_business_capabilities(req.business_capabilities or [])
    if domain:
        logger.info("TC search domain filter: %s", domain)

    try:
        results, _llm_attempts, _rate_limited = await search_tc_on_landscape(
            tc_candidate_name=req.tc_candidate_name,
            tc_description=req.tc_description or "",
            tc_rationale=req.tc_rationale or "",
            domain=domain,
        )

        return SearchResponse(
            query=req.tc_candidate_name,
            results=[
                SearchResultItem(
                    code=r.get("code", ""),
                    name=r.get("name", ""),
                    description=r.get("description", ""),
                    score=r.get("score", 0.0),
                    system_code=r.get("system_alias") or r.get("system_code"),
                    system_name=r.get("system_name"),
                )
                for r in results
            ],
        )
    except Exception as e:
        logger.error("TC search failed: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Ошибка поиска TC: {str(e)}")


class MassSearchCandidate(BaseModel):
    name: str
    description: str = ""


class MassSearchStartRequest(BaseModel):
    tc_candidates: list[MassSearchCandidate]
    # Выбранные бизнес-возможности (BC) — уходят в параметр domain fdm-search
    business_capabilities: list[dict] = []


class MassSearchStartResponse(BaseModel):
    task_id: str


class MassSearchProgressResponse(BaseModel):
    phase: str = "searching"
    current_tc: int = 0
    total_tc: int = 0
    current_tc_name: str = ""
    elapsed_ms: int = 0
    done: bool = False
    error: Optional[str] = None


class MassSearchResultItem(BaseModel):
    code: str
    name: str
    description: str
    score: float
    system_code: Optional[str] = None
    system_name: Optional[str] = None


class MassSearchResultResponse(BaseModel):
    results: dict[str, list[MassSearchResultItem]]


@router.post("/search/start", response_model=MassSearchStartResponse)
async def api_mass_search_start(req: MassSearchStartRequest):
    """Запуск массового поиска TC на ландшафте в фоне."""
    logger.info("POST /api/tc/search/start | %d candidates", len(req.tc_candidates))

    if not req.tc_candidates:
        raise HTTPException(status_code=400, detail="Нет кандидатов для поиска")

    # Удаляем истёкшие задачи до запуска новой (защита от роста памяти)
    cleanup_expired(_search_tasks)

    task_id = str(uuid.uuid4())

    _search_tasks[task_id] = {
        "created_at": time.time(),
        "progress": {
            "phase": "starting",
            "current_tc": 0,
            "total_tc": len(req.tc_candidates),
            "current_tc_name": "",
            "elapsed_ms": 0,
        },
        "result": None,
        "error": None,
    }

    async def progress_callback(progress: dict):
        _search_tasks[task_id]["progress"] = progress

    async def run_search():
        try:
            domain = _domain_from_business_capabilities(req.business_capabilities or [])
            results = await mass_search_tc(
                tc_candidates=[c.model_dump() for c in req.tc_candidates],
                domain=domain,
                progress_callback=progress_callback,
            )
            _search_tasks[task_id]["result"] = results
        except Exception as e:
            logger.error("Mass search task %s failed: %s", task_id, str(e))
            _search_tasks[task_id]["error"] = str(e)

    asyncio.create_task(run_search())

    return MassSearchStartResponse(task_id=task_id)


@router.get("/search/{task_id}/progress", response_model=MassSearchProgressResponse)
async def api_mass_search_progress(task_id: str):
    """Получение прогресса массового поиска TC (long polling)."""
    task = _search_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    progress = task["progress"]
    error = task.get("error")
    result = task.get("result")

    return MassSearchProgressResponse(
        phase=progress.get("phase", "searching"),
        current_tc=progress.get("current_tc", 0),
        total_tc=progress.get("total_tc", 0),
        current_tc_name=progress.get("current_tc_name", ""),
        elapsed_ms=progress.get("elapsed_ms", 0),
        done=result is not None or error is not None,
        error=error,
    )


@router.get("/search/{task_id}/result", response_model=MassSearchResultResponse)
async def api_mass_search_result(task_id: str):
    """Получение результата массового поиска TC."""
    task = _search_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    error = task.get("error")
    if error:
        raise HTTPException(status_code=400, detail=error)

    result = task.get("result")
    if result is None:
        raise HTTPException(status_code=425, detail="Задача ещё не завершена")


    raw_results = result.get("results", {})
    formatted_results: dict[str, list[MassSearchResultItem]] = {}
    for tc_name, items in raw_results.items():
        formatted_results[tc_name] = [
            MassSearchResultItem(
                code=r.get("code", ""),
                name=r.get("name", ""),
                description=r.get("description", ""),
                score=r.get("score", 0.0),
                system_code=r.get("system_alias") or r.get("system_code"),
                system_name=r.get("system_name"),
            )
            for r in items
        ]

    return MassSearchResultResponse(
        results=formatted_results,
    )


