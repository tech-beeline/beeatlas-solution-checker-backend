# Copyright (c) 2024 PJSC VimpelCom
"""
Точка входа FastAPI-приложения HLD Agent.
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import logging
import time

from app.config import settings
from app.api import health, intake, tc, publish, bc, landscape

# Настройка логгирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Отключаем логгирование HTTP-запросов httpx (библиотечные логи, не наши)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("hld-agent")

app = FastAPI(
    title=settings.AGENT_NAME,
    version="0.1.0",
    description="Агент для анализа требований и оценки влияния изменений на ландшафт",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Логгирование всех HTTP-запросов, кроме /api/health."""
    path = request.url.path

    # Пропускаем healthcheck без логгирования
    if path == "/api/health":
        return await call_next(request)

    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)

    logger.info(
        "%s %s %s %dms",
        request.method,
        path,
        response.status_code,
        duration_ms,
    )
    return response


# Регистрация роутов
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(intake.router, prefix="/api/intake", tags=["intake"])
app.include_router(bc.router, prefix="/api/bc", tags=["bc"])
app.include_router(tc.router, prefix="/api/tc", tags=["tc"])
app.include_router(publish.router, prefix="/api/publish", tags=["publish"])
app.include_router(landscape.router, prefix="/api/landscape", tags=["landscape"])
