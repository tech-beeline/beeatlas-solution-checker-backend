# Copyright (c) 2024 PJSC VimpelCom
"""
Health-check endpoint.
GET /api/health — проверка доступности всех внешних зависимостей.
"""

import logging
from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime, timezone
import httpx

from app.config import settings
from app.integrations.llm_client import disable_thinking_params

logger = logging.getLogger("hld-agent")

router = APIRouter()


class IntegrationStatus(BaseModel):
    backend: str = "ok"
    llm: str = "unknown"
    beeatlas: str = "unknown"
    fdm_search: str = "unknown"


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str
    integrations: IntegrationStatus


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Проверка работоспособности backend и внешних зависимостей."""
    integrations = IntegrationStatus()

    # Проверка LLM
    if settings.LLM_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=5, verify=False) as client:
                response = await client.post(
                    f"{settings.LLM_API_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.LLM_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings.LLM_MODEL,
                        "messages": [
                            {"role": "user", "content": "Say 'ok' if you are working."}
                        ],
                        "max_tokens": 10,
                        # Reasoning выключаем и здесь: иначе все 10 токенов уйдут в
                        # thinking и ответ придёт пустым (проба смотрит только на статус,
                        # но пустой content маскирует реальную деградацию модели).
                        **disable_thinking_params("health"),
                    }
                )

                if response.status_code == 200:
                    integrations.llm = "ok"
                else:
                    integrations.llm = "error"
                    logger.error("LLM health check failed: status=%d", response.status_code)
        except Exception as e:
            integrations.llm = "error"
            logger.error("LLM health check failed: %s", str(e))
    else:
        integrations.llm = "unknown"

    # Проверка BeeAtlas
    if settings.BEEATLAS_API_URL and settings.BEEATLAS_API_KEY:
        try:
            from app.integrations.beeatlas import _beeatlas_request

            # Используем лёгкий GET-запрос к product API для проверки доступности
            result = await _beeatlas_request("GET", "/api-gateway/product/v1/product/fdmshowcaseapp/patterns")
            integrations.beeatlas = "ok" if result is not None else "error"
            if result is None:
                logger.error("BeeAtlas health check failed")
        except Exception as e:
            integrations.beeatlas = "error"
            logger.error("BeeAtlas health check failed: %s", str(e))
    else:
        integrations.beeatlas = "unknown"

    # Проверка FDM Search (fdm-search за BeeAtlas Gateway)
    if settings.BEEATLAS_API_URL and settings.BEEATLAS_API_KEY:
        try:
            from app.integrations.beeatlas import _beeatlas_request_with_path

            # Лёгкий поисковый запрос для проверки доступности fdm-search
            result = await _beeatlas_request_with_path(
                "GET",
                "/search/api/v1/search",
                "/search/api/v1/search?query=test&limit=1",
            )
            integrations.fdm_search = "ok" if result is not None else "error"
            if result is None:
                logger.error("FDM search health check failed")
        except Exception as e:
            integrations.fdm_search = "error"
            logger.error("FDM search health check failed: %s", str(e))
    else:
        integrations.fdm_search = "not_configured"

    overall = "ok" if all(
        v == "ok" or v == "unknown" or v == "disabled" or v == "not_configured"
        for v in [
            integrations.backend,
            integrations.llm,
            integrations.beeatlas,
            integrations.fdm_search,
        ]
    ) else "degraded"

    return HealthResponse(
        status=overall,
        timestamp=datetime.now(timezone.utc).isoformat(),
        version="0.1.0",
        integrations=integrations,
    )
