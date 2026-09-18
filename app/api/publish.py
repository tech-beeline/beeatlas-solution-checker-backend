# Copyright (c) 2024 PJSC VimpelCom
"""
REST API этапа Publish — экспорт и публикация результатов.
Все эндпоинты stateless — не используют хранилище сессий.
"""

import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import tempfile
from typing import Optional

from app.core.publish import generate_markdown, publish_to_confluence

logger = logging.getLogger("hld-agent")

router = APIRouter()


class StructuredRequirementItem(BaseModel):
    id: str = ""
    type: str = ""
    title: str = ""
    description: str = ""


class ImpactTCSystem(BaseModel):
    code: str = ""
    name: str = ""
    endpoints: list[str] = []


class ImpactBcItem(BaseModel):
    """Родительская BC технической возможности (код + название)."""
    code: str = ""
    name: str = ""


class ImpactTCItem(BaseModel):
    """Полная TC с системой — единый источник данных для экспорта."""
    code: str = ""
    name: str = ""
    description: str = ""
    action: str = "reuse"
    source: str = ""
    fr_ids: list[str] = []
    system: Optional[ImpactTCSystem] = None
    # Опционально: родительская BC (эксперимент feature/exp-bc-search). Старые клиенты
    # поле не шлют → None, поведение не меняется.
    parent_bc: Optional[ImpactBcItem] = None


class ExportRequest(BaseModel):
    title: str
    source: str
    source_url: Optional[str] = None
    structured_requirements: list[StructuredRequirementItem] = []
    impact_tcs: list[ImpactTCItem] = []
    task_description: str = ""
    impact_level: str = ""
    impact_level_label: str = ""


class ConfluencePublishRequest(BaseModel):
    title: str
    source: str
    source_url: Optional[str] = None
    page_title: Optional[str] = None
    parent_page_url: Optional[str] = None
    pat: Optional[str] = None
    structured_requirements: list[StructuredRequirementItem] = []
    impact_tcs: list[ImpactTCItem] = []
    task_description: str = ""
    impact_level: str = ""
    impact_level_label: str = ""


class ConfluencePublishResponse(BaseModel):
    confluence_url: str = ""


@router.post("/export")
async def export_hld(req: ExportRequest):
    """Экспорт всех данных сессии в hld-analyst.md."""
    logger.info("POST /api/publish/export | title=%s", req.title)

    # Debug: проверяем impact_tcs
    impact_tcs_data = [t.model_dump() for t in req.impact_tcs]
    for tc in impact_tcs_data:
        sys = tc.get("system")
        logger.info(
            "  TC: code=%s name=%s action=%s system=%s",
            tc.get("code"),
            tc.get("name"),
            tc.get("action"),
            f"{sys.get('code','')} — {sys.get('name','')}" if sys and isinstance(sys, dict) else "None",
        )

    try:
        markdown = generate_markdown(
            title=req.title,
            source=req.source,
            source_url=req.source_url,
            structured_requirements=[r.model_dump() for r in req.structured_requirements],
            impact_tcs=impact_tcs_data,
            task_description=req.task_description or None,
            impact_level=req.impact_level or None,
            impact_level_label=req.impact_level_label or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Сохраняем во временный файл
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="hld-report-",
        delete=False,
        encoding="utf-8",
    )
    tmp.write(markdown)
    tmp_path = tmp.name
    tmp.close()

    # Используем заголовок сессии для имени файла (только ASCII)
    safe_title = "".join(c if c.isascii() and (c.isalnum() or c in " _-") else "_" for c in req.title)[:40]
    filename = f"hld-report-{safe_title}.md"

    return FileResponse(
        path=tmp_path,
        media_type="text/markdown",
        filename=filename,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/confluence", response_model=ConfluencePublishResponse)
async def publish_to_confluence_api(req: ConfluencePublishRequest):
    """Публикация в Confluence (Storage Format)."""
    logger.info("POST /api/publish/confluence | title=%s", req.title)

    try:
        page_url = await publish_to_confluence(
            title=req.title,
            source=req.source,
            source_url=req.source_url,
            page_title=req.page_title,
            parent_page_url=req.parent_page_url,
            pat=req.pat,
            structured_requirements=[r.model_dump() for r in req.structured_requirements],
            impact_tcs=[t.model_dump() for t in req.impact_tcs],
            impact_level=req.impact_level or None,
            impact_level_label=req.impact_level_label or None,
        )

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Confluence publish failed: %s", str(e))
        raise HTTPException(status_code=502, detail=f"Confluence API error: {str(e)}")

    return ConfluencePublishResponse(
        confluence_url=page_url,
    )
