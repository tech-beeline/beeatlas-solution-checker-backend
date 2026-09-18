# Copyright (c) 2024 PJSC VimpelCom
"""
REST API "живого" поиска по каталогу (эксперимент feature/exp-bc-search).

Старый API `/api/bc/*` и `/api/tc/*` остаётся без изменений (используется
предыдущей версией приложения). Здесь — только новые методы:

  POST /api/landscape/analyze/start   — асинхронный анализ кандидатов TC по каталогу
  GET  /api/landscape/analyze/{id}/progress
  GET  /api/landscape/analyze/{id}/result
  POST /api/landscape/bc/search       — синхронный type-ahead поиск BC (для BC-picker)

Все эндпоинты stateless — не используют хранилище сессий.
"""

import logging
import uuid
import asyncio
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.api.task_store import cleanup_expired
from app.config import settings
from app.core.landscape import analyze_tc_candidates, search_business_capabilities

logger = logging.getLogger("hld-agent")

router = APIRouter()

# In-memory хранилище задач анализа по каталогу
# task_id -> { "created_at": float, "progress": dict, "result": dict | None, "error": str | None }
_analyze_tasks: dict[str, dict] = {}


# --- DTO: анализ кандидатов ---


class AnalyzeCandidate(BaseModel):
    name: str
    description: str = ""


class AnalyzeStartRequest(BaseModel):
    candidates: list[AnalyzeCandidate]
    # None -> берём дефолты из настроек (settings.FDM_BC_TOP_K / FDM_TC_PER_BC_TOP_K)
    bc_top_k: Optional[int] = None
    tc_top_k: Optional[int] = None


class AnalyzeStartResponse(BaseModel):
    task_id: str


class AnalyzeProgressResponse(BaseModel):
    phase: str = "starting"
    current_tc: int = 0
    total_tc: int = 0
    current_tc_name: str = ""
    current_bc: int = 0
    total_bc: int = 0
    current_bc_code: str = ""
    # Аддитивно (эксперимент мониторинга): имя BC, чей поиск TC идёт/только что завершён
    current_bc_name: str = ""
    elapsed_ms: int = 0
    done: bool = False
    error: Optional[str] = None


class LandscapeTcItem(BaseModel):
    code: str
    name: str
    description: str = ""
    score: float = 0.0
    system_code: Optional[str] = None
    system_name: Optional[str] = None


class LandscapeBcItem(BaseModel):
    code: str
    name: str
    description: str = ""
    score: float = 0.0
    tcs: list[LandscapeTcItem] = []


class CandidateAnalysisResult(BaseModel):
    candidate_name: str
    query: str = ""
    bcs: list[LandscapeBcItem] = []


class AnalyzeResultResponse(BaseModel):
    results: list[CandidateAnalysisResult]
    elapsed_ms: int = 0


# --- DTO: type-ahead поиск BC ---


class BcSearchRequest(BaseModel):
    query: str
    top_k: int = 10


class BcSearchItem(BaseModel):
    code: str
    name: str
    description: str = ""
    score: float = 0.0


class BcSearchResponse(BaseModel):
    results: list[BcSearchItem]


@router.post("/analyze/start", response_model=AnalyzeStartResponse)
async def api_landscape_analyze_start(req: AnalyzeStartRequest):
    """
    Запуск анализа кандидатов TC по живому каталогу в фоне.
    Для каждого кандидата: поиск BC (entity_type="bc") -> top-N BC -> их TC.
    Возвращает task_id для long polling прогресса.
    """
    if not req.candidates:
        raise HTTPException(status_code=400, detail="Нет кандидатов TC для анализа")

    names = [c.name for c in req.candidates if c.name and c.name.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="Кандидаты TC не должны быть пустыми")

    bc_top_k = req.bc_top_k or settings.FDM_BC_TOP_K
    tc_top_k = req.tc_top_k or settings.FDM_TC_PER_BC_TOP_K

    logger.info(
        "POST /api/landscape/analyze/start | %d candidates | bc_top_k=%d tc_top_k=%d",
        len(names), bc_top_k, tc_top_k,
    )

    # Удаляем истёкшие задачи до запуска новой (защита от роста памяти)
    cleanup_expired(_analyze_tasks)

    task_id = str(uuid.uuid4())

    _analyze_tasks[task_id] = {
        "created_at": time.time(),
        "progress": {
            "phase": "starting",
            "current_tc": 0,
            "total_tc": len(req.candidates),
            "current_tc_name": "",
            "current_bc": 0,
            "total_bc": 0,
            "current_bc_code": "",
        },
        "result": None,
        "error": None,
    }

    async def progress_callback(progress: dict):
        _analyze_tasks[task_id]["progress"] = progress

    async def run_analyze():
        try:
            result = await analyze_tc_candidates(
                candidates=[c.model_dump() for c in req.candidates],
                bc_top_k=bc_top_k,
                tc_top_k=tc_top_k,
                progress_callback=progress_callback,
            )
            _analyze_tasks[task_id]["result"] = result
        except Exception as e:
            logger.error("Landscape analyze task %s failed: %s", task_id, str(e))
            _analyze_tasks[task_id]["error"] = str(e)

    asyncio.create_task(run_analyze())

    return AnalyzeStartResponse(task_id=task_id)


@router.get("/analyze/{task_id}/progress", response_model=AnalyzeProgressResponse)
async def api_landscape_analyze_progress(task_id: str):
    """Прогресс анализа по каталогу (long polling)."""
    task = _analyze_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    progress = task["progress"]
    error = task.get("error")
    result = task.get("result")

    return AnalyzeProgressResponse(
        phase=progress.get("phase", "starting"),
        current_tc=progress.get("current_tc", 0),
        total_tc=progress.get("total_tc", 0),
        current_tc_name=progress.get("current_tc_name", ""),
        current_bc=progress.get("current_bc", 0),
        total_bc=progress.get("total_bc", 0),
        current_bc_code=progress.get("current_bc_code", ""),
        current_bc_name=progress.get("current_bc_name", ""),
        elapsed_ms=progress.get("elapsed_ms", 0),
        done=result is not None or error is not None,
        error=error,
    )


@router.get("/analyze/{task_id}/result", response_model=AnalyzeResultResponse)
async def api_landscape_analyze_result(task_id: str):
    """Результат анализа по каталогу."""
    task = _analyze_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    error = task.get("error")
    if error:
        raise HTTPException(status_code=400, detail=error)

    result = task.get("result")
    if result is None:
        raise HTTPException(status_code=425, detail="Задача ещё не завершена")

    return AnalyzeResultResponse(
        results=[
            CandidateAnalysisResult(
                candidate_name=r.get("candidate_name", ""),
                query=r.get("query", ""),
                bcs=[
                    LandscapeBcItem(
                        code=bc.get("code", ""),
                        name=bc.get("name", ""),
                        description=bc.get("description", ""),
                        score=float(bc.get("score") or 0),
                        tcs=[
                            LandscapeTcItem(
                                code=tc.get("code", ""),
                                name=tc.get("name", ""),
                                description=tc.get("description", ""),
                                score=float(tc.get("score") or 0),
                                system_code=tc.get("system_code"),
                                system_name=tc.get("system_name"),
                            )
                            for tc in bc.get("tcs", [])
                        ],
                    )
                    for bc in r.get("bcs", [])
                ],
            )
            for r in result.get("results", [])
        ],
        elapsed_ms=result.get("elapsed_ms", 0),
    )


@router.post("/bc/search", response_model=BcSearchResponse)
async def api_landscape_bc_search(req: BcSearchRequest):
    """
    Синхронный поиск BC в живом каталоге (type-ahead для BC-picker нового TC).
    """
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Нет запроса для поиска BC")

    logger.info(
        "POST /api/landscape/bc/search | '%s' (%d chars, top_k=%d)",
        query, len(query), req.top_k,
    )

    bcs = await search_business_capabilities(query=query, top_k=req.top_k)

    return BcSearchResponse(
        results=[
            BcSearchItem(
                code=b.get("code", ""),
                name=b.get("name", ""),
                description=b.get("description", ""),
                score=float(b.get("score") or 0),
            )
            for b in bcs
        ]
    )
