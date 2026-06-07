from __future__ import annotations

import threading
import time
import uuid
from typing import Any


_DEFAULT_TTL_SECONDS = 3600


class TaskStore:
    """In-process registry of image tasks.

    The backend owns the full task lifecycle in a worker thread (submit ->
    call upstream / poll -> store result). The frontend keeps polling
    ``GET /api/status?task_id=...``; this store is what that endpoint reads.

    Suitable for a single-process local tool. Tasks are evicted after a TTL
    so memory stays bounded; they do not survive a process restart.
    """

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS):
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._ttl = ttl_seconds

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
        return task_id

    def update(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.update(fields)
                # Stamp terminal time so TTL only reaps finished tasks.
                if fields.get("status") in {"completed", "failed"} and not task.get("completed_at"):
                    task["completed_at"] = time.time()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task is not None else None

    def _evict_expired_locked(self) -> None:
        # Only evict tasks that have finished and whose TTL has elapsed; never
        # reap a still-running task (queued/processing have no completed_at).
        cutoff = time.time() - self._ttl
        stale = [
            tid
            for tid, task in self._tasks.items()
            if task.get("completed_at") is not None and task["completed_at"] < cutoff
        ]
        for tid in stale:
            self._tasks.pop(tid, None)


# Module-level singleton shared across requests and worker threads.
task_store = TaskStore()
