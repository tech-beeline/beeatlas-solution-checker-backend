# Copyright (c) 2024 PJSC VimpelCom
"""
Тесты для app/api/task_store.py — TTL-очистка in-memory хранилищ задач.
"""

import time

from app.api.task_store import cleanup_expired
from app.config import settings


def _task(created_at):
    return {"created_at": created_at, "progress": {}, "result": None, "error": None}


def test_removes_expired_tasks(monkeypatch):
    monkeypatch.setattr(settings, "TASK_TTL_SECONDS", 10)
    store = {
        "old": _task(time.time() - 60),   # истекла (старше TTL)
        "fresh": _task(time.time()),        # в пределах TTL
    }
    removed = cleanup_expired(store)
    assert removed == 1
    assert "old" not in store
    assert "fresh" in store


def test_keeps_tasks_without_created_at(monkeypatch):
    """Задачи без created_at (созданные до введения TTL) не трогаем."""
    monkeypatch.setattr(settings, "TASK_TTL_SECONDS", 10)
    store = {"legacy": {"progress": {}, "result": None, "error": None}}
    removed = cleanup_expired(store)
    assert removed == 0
    assert "legacy" in store


def test_empty_store(monkeypatch):
    monkeypatch.setattr(settings, "TASK_TTL_SECONDS", 10)
    assert cleanup_expired({}) == 0
