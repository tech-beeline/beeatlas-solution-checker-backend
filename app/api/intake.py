# Copyright (c) 2024 PJSC VimpelCom
"""
REST API этапа Intake — импорт и структурирование требований.
Все эндпоинты stateless — не используют хранилище сессий.
Структурирование через LLM — асинхронное с long polling прогресса.
"""

import logging
import uuid
import asyncio
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.api.task_store import cleanup_expired
from app.core.intake import import_requirements, structure_requirements

logger = logging.getLogger("hld-agent")

router = APIRouter()

# In-memory хранилище задач структурирования
# task_id -> { "progress": dict, "result": list[dict] | None, "error": str | None }
_structure_tasks: dict[str, dict] = {}


class ImportRequest(BaseModel):
    text: Optional[str] = None
    confluence_url: Optional[str] = None
    include_child_pages: bool = False
    confluence_pat: Optional[str] = None


class ImportResponse(BaseModel):
    raw_text: str
    source: str
    title: str
    source_url: Optional[str] = None


class StructureRequest(BaseModel):
    raw_text: str


class StructuredRequirement(BaseModel):
    id: str
    type: str
    title: str
    description: str


class StructureResponse(BaseModel):
    requirements: list[StructuredRequirement]


class StructureStartResponse(BaseModel):
    task_id: str


class StructureProgressResponse(BaseModel):
    phase: str
    current_chunk: int = 0
    total_chunks: int = 0
    chars_sent: int = 0
    last_response_chars: int = 0
    chunk_size: int = 0
    attempts: int = 0
    merge_input_chars: int = 0
    merge_attempts: int = 0
    done: bool = False
    error: Optional[str] = None


@router.post("/import", response_model=ImportResponse)
async def import_text(req: ImportRequest):
    """Импорт требований из текста или Confluence."""
    logger.info("POST /api/intake/import")

    if not req.text and not req.confluence_url:
        raise HTTPException(
            status_code=400,
            detail="Не указан источник требований: text или confluence_url",
        )

    try:
        result = await import_requirements(
            text=req.text,
            confluence_url=req.confluence_url,
            include_child_pages=req.include_child_pages,
            confluence_pat=req.confluence_pat,
        )
    except ValueError as e:
        logger.warning("Import failed (400): %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Import failed (500): %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Внутренняя ошибка сервера при импорте: {str(e)}",
        )

    return ImportResponse(
        raw_text=result["raw_text"],
        source=result["source"],
        title=result["title"],
        source_url=result.get("source_url"),
    )


@router.post("/structure/start", response_model=StructureStartResponse)
async def structure_start(req: StructureRequest):
    """
    Запуск структурирования требований через LLM в фоне.
    Возвращает task_id для long polling прогресса.
    """
    logger.info("POST /api/intake/structure/start")

    if not req.raw_text:
        raise HTTPException(status_code=400, detail="Нет текста для структурирования")

    # Удаляем истёкшие задачи до запуска новой (защита от роста памяти)
    cleanup_expired(_structure_tasks)

    task_id = str(uuid.uuid4())

    # Инициализируем задачу
    _structure_tasks[task_id] = {
        "created_at": time.time(),
        "progress": {
            "phase": "chunking",
            "current_chunk": 0,
            "total_chunks": 0,
            "chars_sent": 0,
            "last_response_chars": 0,
            "chunk_size": 0,
            "attempts": 0,
            "merge_input_chars": 0,
            "merge_attempts": 0,
        },
        "result": None,
        "error": None,
        "last_response_chars": 0,
    }

    async def progress_callback(progress: dict):
        # Сохраняем максимальное значение last_response_chars (не перезаписываем на 0)
        current_last = _structure_tasks[task_id].get("last_response_chars", 0)
        new_last = progress.get("last_response_chars", 0)
        if new_last > 0:
            _structure_tasks[task_id]["last_response_chars"] = new_last
        else:
            # Если пришёл 0, сохраняем предыдущее значение
            progress["last_response_chars"] = current_last

        _structure_tasks[task_id]["progress"] = progress
        logger.info(
            "Progress callback for task %s: phase=%s, last_response_chars=%d, chars_sent=%d",
            task_id,
            progress.get("phase"),
            progress.get("last_response_chars", 0),
            progress.get("chars_sent", 0),
        )

    async def run_structure():
        try:
            requirements = await structure_requirements(
                raw_text=req.raw_text,
                progress_callback=progress_callback,
            )
            _structure_tasks[task_id]["result"] = requirements
        except Exception as e:
            logger.error("Structure task %s failed: %s", task_id, str(e))
            _structure_tasks[task_id]["error"] = str(e)

    # Запускаем в фоне
    asyncio.create_task(run_structure())

    return StructureStartResponse(task_id=task_id)


@router.get("/structure/{task_id}/progress", response_model=StructureProgressResponse)
async def structure_progress(task_id: str):
    """
    Получение прогресса структурирования (long polling).
    """
    task = _structure_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    progress = task["progress"]
    error = task.get("error")
    result = task.get("result")

    return StructureProgressResponse(
        phase=progress.get("phase", "unknown"),
        current_chunk=progress.get("current_chunk", 0),
        total_chunks=progress.get("total_chunks", 0),
        chars_sent=progress.get("chars_sent", 0),
        last_response_chars=task.get("last_response_chars", progress.get("last_response_chars", 0)),
        chunk_size=progress.get("chunk_size", 0),
        attempts=progress.get("attempts", 0),
        merge_input_chars=progress.get("merge_input_chars", 0),
        merge_attempts=progress.get("merge_attempts", 0),
        done=result is not None or error is not None,
        error=error,
    )


@router.get("/structure/{task_id}/result", response_model=StructureResponse)
async def structure_result(task_id: str):
    """
    Получение результата структурирования.
    """
    task = _structure_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    error = task.get("error")
    if error:
        raise HTTPException(status_code=400, detail=error)

    result = task.get("result")
    if result is None:
        raise HTTPException(status_code=425, detail="Задача ещё не завершена")


    return StructureResponse(
        requirements=[
            StructuredRequirement(
                id=r.get("id", ""),
                type=r.get("type", ""),
                title=r.get("title", ""),
                description=r.get("description", ""),
            )
            for r in result
        ]
    )
