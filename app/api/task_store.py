# Copyright (c) 2024 PJSC VimpelCom
"""
Хелперы для in-memory хранилищ асинхронных задач (long polling).

Задачи хранятся в модульных dict: task_id -> {...}. Чтобы не допустить
неограниченного роста памяти, перед запуском новой задачи вызывается
cleanup_expired(), удаляющая задачи старше TTL (TASK_TTL_SECONDS).
"""

import logging
import time

from app.config import settings

logger = logging.getLogger("hld-agent")


def cleanup_expired(store: dict[str, dict]) -> int:
    """Удаляет из store задачи старше TTL. Возвращает число удалённых задач."""
    ttl = settings.TASK_TTL_SECONDS
    now = time.time()

    # Задачи без created_at (созданные до введения TTL) не трогаем
    expired_ids = [
        task_id
        for task_id, task in store.items()
        if task.get("created_at") and now - task["created_at"] > ttl
    ]

    for task_id in expired_ids:
        store.pop(task_id, None)

    if expired_ids:
        logger.info(
            "Task store cleanup: removed %d expired tasks (ttl=%ds)",
            len(expired_ids),
            ttl,
        )

    return len(expired_ids)
