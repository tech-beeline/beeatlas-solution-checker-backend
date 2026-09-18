# Copyright (c) 2024 PJSC VimpelCom
"""
REST API этапа Business Capability — выявление BC из текста задачи.
Все эндпоинты stateless — не используют хранилище сессий.
Выявление BC через LLM — асинхронное с long polling прогресса.
"""

import logging
import uuid
import asyncio
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.api.task_store import cleanup_expired
from app.core.bc import identify_business_capabilities

logger = logging.getLogger("hld-agent")

router = APIRouter()

# In-memory хранилище задач выявления BC
# task_id -> { "created_at": float, "progress": dict, "result": list[dict] | None, "error": str | None }
_bc_tasks: dict[str, dict] = {}


class BCIdentifyRequest(BaseModel):
    task_text: str


class BCCandidate(BaseModel):
    code: str
    description: str = ""
    relevance: int = 0
    reason: str = ""


class BCIdentifyStartResponse(BaseModel):
    task_id: str


class BCProgressResponse(BaseModel):
    phase: str = "identifying"
    data_chars: int = 0
    response_chars: int = 0
    attempts: int = 0
    elapsed_ms: int = 0
    done: bool = False
    error: Optional[str] = None


class BCIdentifyResponse(BaseModel):
    candidates: list[BCCandidate]


@router.post("/identify/start", response_model=BCIdentifyStartResponse)
async def api_bc_identify_start(req: BCIdentifyRequest):
    """
    Запуск выявления BC из текста задачи через LLM в фоне.
    Возвращает task_id для long polling прогресса.
    """
    logger.info("POST /api/bc/identify/start | %d chars", len(req.task_text))

    if not req.task_text or not req.task_text.strip():
        raise HTTPException(status_code=400, detail="Нет текста задачи для выявления BC")

    # Удаляем истёкшие задачи до запуска новой (защита от роста памяти)
    cleanup_expired(_bc_tasks)

    task_id = str(uuid.uuid4())

    # Инициализируем задачу
    _bc_tasks[task_id] = {
        "created_at": time.time(),
        "progress": {
            "phase": "identifying",
            "data_chars": 0,
            "response_chars": 0,
        },
        "result": None,
        "error": None,
    }

    async def progress_callback(progress: dict):
        _bc_tasks[task_id]["progress"] = progress

    async def run_identify():
        try:
            candidates = await identify_business_capabilities(
                task_text=req.task_text,
                progress_callback=progress_callback,
            )
            _bc_tasks[task_id]["result"] = candidates
        except Exception as e:
            logger.error("BC identify task %s failed: %s", task_id, str(e))
            _bc_tasks[task_id]["error"] = str(e)

    # Запускаем в фоне
    asyncio.create_task(run_identify())

    return BCIdentifyStartResponse(task_id=task_id)


@router.get("/identify/{task_id}/progress", response_model=BCProgressResponse)
async def api_bc_identify_progress(task_id: str):
    """
    Получение прогресса выявления BC (long polling).
    """
    task = _bc_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    progress = task["progress"]
    error = task.get("error")
    result = task.get("result")

    return BCProgressResponse(
        phase=progress.get("phase", "identifying"),
        data_chars=progress.get("data_chars", 0),
        response_chars=progress.get("response_chars", 0),
        attempts=progress.get("attempts", 0),
        elapsed_ms=progress.get("elapsed_ms", 0),
        done=result is not None or error is not None,
        error=error,
    )


@router.get("/identify/{task_id}/result", response_model=BCIdentifyResponse)
async def api_bc_identify_result(task_id: str):
    """
    Получение результата выявления BC.
    """
    task = _bc_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    error = task.get("error")
    if error:
        raise HTTPException(status_code=400, detail=error)

    result = task.get("result")
    if result is None:
        raise HTTPException(status_code=425, detail="Задача ещё не завершена")

    return BCIdentifyResponse(
        candidates=[
            BCCandidate(
                code=c.get("code", ""),
                description=c.get("description", ""),
                relevance=int(c.get("relevance", 0) or 0),
                reason=c.get("reason", ""),
            )
            for c in result
        ]
    )
