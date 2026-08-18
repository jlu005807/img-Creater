from __future__ import annotations

import json
import logging
import atexit
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


_DEFAULT_TTL_SECONDS = 3600
logger = logging.getLogger(__name__)


class TaskStore:
    """Registry of image tasks with optional SQLite persistence.

    The backend owns the full task lifecycle in a worker thread (submit ->
    call upstream / poll -> store result). The frontend keeps polling
    ``GET /api/status?task_id=...``; this store is what that endpoint reads.

    When ``db_path`` is provided, task state is persisted to a SQLite
    database so it survives process restarts. When ``db_path`` is None
    (default), tasks are kept in-memory only and do not survive restarts.

    Tasks are evicted after a TTL so storage stays bounded.
    """

    def __init__(
        self,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        db_path: str | None = None,
    ):
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._ttl = ttl_seconds
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        if db_path is not None:
            self._init_db()
            self._load_from_db()
            self._register_cleanup()

    # ------------------------------------------------------------------ db

    def _init_db(self) -> None:
        assert self._db_path is not None
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._db = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "    task_id TEXT PRIMARY KEY,"
            "    data TEXT NOT NULL,"
            "    created_at REAL NOT NULL,"
            "    completed_at REAL,"
            "    status TEXT NOT NULL"
            ")"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
        )

    def _load_from_db(self) -> None:
        if self._db is None:
            return
        rows = self._db.execute("SELECT task_id, data FROM tasks").fetchall()
        for task_id, data_json in rows:
            try:
                self._tasks[task_id] = json.loads(data_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("[task_store] skipping corrupt task row task_id=%s", task_id)

    def _persist_task_locked(self, task_id: str) -> None:
        if self._db is None:
            return
        task = self._tasks.get(task_id)
        if task is None:
            return
        data_json = json.dumps(task, ensure_ascii=False, default=str)
        self._db.execute(
            "INSERT OR REPLACE INTO tasks (task_id, data, created_at, completed_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                data_json,
                task.get("created_at") or time.time(),
                task.get("completed_at"),
                task.get("status", "queued"),
            ),
        )

    def _delete_task_locked(self, task_id: str) -> None:
        if self._db is None:
            return
        self._db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

    # ------------------------------------------------------------------ api

    def create(self, operation: str) -> str:
        task_id = uuid.uuid4().hex
        with self._lock:
            self._evict_expired_locked()
            self._tasks[task_id] = {
                "task_id": task_id,
                "operation": operation,
                "status": "queued",
                "api_id": None,
                "api_name": None,
                "urls": [],
                "attempts": [],
                "error": None,
                "expires_at": None,
                "created_at": time.time(),
                "completed_at": None,
            }
            self._persist_task_locked(task_id)
        return task_id

    def update(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                if task.get("status") == "cancelled" and fields.get("status") != "cancelled":
                    return
                task.update(fields)
                if fields.get("status") in {"completed", "failed", "cancelled"} and not task.get("completed_at"):
                    task["completed_at"] = time.time()
                self._persist_task_locked(task_id)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task is not None else None

    def cancel(self, task_id: str, error: str = "任务已手动停止") -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.get("status") in {"completed", "failed", "cancelled"}:
                return False
            task.update(
                {
                    "status": "cancelled",
                    "error": error,
                    "completed_at": time.time(),
                }
            )
            self._persist_task_locked(task_id)
            return True

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            return bool(task and task.get("status") == "cancelled")

    def list_active(self) -> list[dict[str, Any]]:
        """Return all tasks that are not in a terminal state."""
        with self._lock:
            return [
                dict(task)
                for task in self._tasks.values()
                if task.get("status") in ("queued", "processing")
            ]

    def _evict_expired_locked(self) -> None:
        cutoff = time.time() - self._ttl
        stale = [
            tid
            for tid, task in self._tasks.items()
            if task.get("completed_at") is not None and task["completed_at"] < cutoff
        ]
        for tid in stale:
            self._tasks.pop(tid, None)
            self._delete_task_locked(tid)


    def close(self) -> None:
        """Close the database connection if open."""
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _register_cleanup(self) -> None:
        """Register atexit cleanup for the SQLite connection.

        Unlike __del__, atexit runs before interpreter teardown so sqlite3
        connections close cleanly without risking 'Interpreter already
        finalised' exceptions.
        """
        atexit.register(self.close)
