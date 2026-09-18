# Copyright (c) 2024 PJSC VimpelCom
"""
Настройки приложения из переменных окружения.
Источник истины для всех env-ключей.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Общие ---
    AGENT_NAME: str = "HLD Agent"
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # --- LLM ---
    LLM_API_URL: str = "https://api.deepseek.com"
    LLM_MODEL: str = "deepseek-chat"
    LLM_API_KEY: str = ""
    LLM_TIMEOUT_SECONDS: int = 30
    LLM_MAX_TOKENS: int = 16000
    LLM_TEMPERATURE: float = 0.2
    LLM_RETRY_COUNT: int = 3
    # Reasoning ("thinking") у моделей вроде Qwen3. Токены рассуждений расходуются из
    # того же бюджета max_tokens, что и ответ, поэтому на больших входах (merge,
    # identify_bc) весь лимит уходит в reasoning, а content приходит ПУСТОЙ строкой
    # при status=ok — потребитель молча получает 0 требований (инцидент 2026-09-11).
    # Глобальный выключатель: True — reasoning включён во всех вызовах.
    LLM_ENABLE_THINKING: bool = False
    # Исключения из глобального выключения: промпты (prompt_name), для которых reasoning
    # остаётся включённым. Дедупликация — задача на сопоставление и склейку дубликатов
    # (merge_dedup в intake, merge_tc в TC), где рассуждения повышают качество слияния.
    LLM_THINKING_PROMPTS: str = "" #merge_dedup,merge_tc"
    # Бюджет вывода (max_tokens) для промптов с reasoning. Рассуждения делят этот бюджет
    # с ответом, поэтому при общем LLM_MAX_TOKENS=16384 весь лимит может уйти в thinking
    # и content придёт пустым — здесь бюджет задаётся с запасом. 0 — не переопределять
    # (использовать LLM_MAX_TOKENS). Значение всё равно клампится под контекст модели.
    LLM_THINKING_MAX_TOKENS: int = 32768

    # --- Confluence ---
    CONFLUENCE_TIMEOUT_SECONDS: int = 30

    # --- Выявление TC ---
    # Если FR больше этого порога, выявление TC идёт по чанкам: последовательные
    # запросы по чанкам + финальный запрос дедупликации (промпт merge_tc).
    TC_IDENTIFY_CHUNK_SIZE: int = 30

    # --- BeeAtlas ---
    BEEATLAS_API_URL: str = ""
    BEEATLAS_API_KEY: str = ""
    BEEATLAS_API_SECRET: str = ""
    BEEATLAS_TIMEOUT_SECONDS: int = 30

    # --- FDM Search (поиск TC через fdm-search за BeeAtlas Gateway) ---
    FDM_SEARCH_TOP_K: int = 10
    # Список систем-исключений для поиска TC: alias или name через запятую.
    # Пустая строка — без исключений (по умолчанию исключения не применяются).
    FDM_SEARCH_EXCLUDE_SYSTEMS: str = ""

    # --- FDM Search: анализ по каталогу (live-catalog BC, этап TC) ---
    # Сколько BC-кандидатов ищем на каждый выделенный TC и сколько TC в каждом BC.
    # Поиск по каталогу идёт по TOP-10 BC на кандидата.
    FDM_BC_TOP_K: int = 10
    FDM_TC_PER_BC_TOP_K: int = 5

    # --- Асинхронные задачи (in-memory хранилища long polling) ---
    # TTL хранения задачи в секундах. Задачи старше TTL удаляются перед
    # запуском новых (см. app/api/task_store.py).
    TASK_TTL_SECONDS: int = 86400  # 24 часа

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
