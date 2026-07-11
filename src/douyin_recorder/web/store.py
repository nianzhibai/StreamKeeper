from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .schemas import TaskConfig, TaskRecord, TaskStatus

CONFIG_COLUMNS = {
    "url",
    "label",
    "quality",
    "output_format",
    "source",
    "segment_seconds",
    "monitor",
    "interval_seconds",
}
RUNTIME_COLUMNS = {
    "enabled",
    "status",
    "status_message",
    "anchor_name",
    "live_title",
    "is_live",
    "output_path",
    "last_checked_at",
    "started_at",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recording_tasks (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    label TEXT,
                    quality TEXT NOT NULL,
                    output_format TEXT NOT NULL,
                    source TEXT NOT NULL,
                    segment_seconds INTEGER NOT NULL,
                    monitor INTEGER NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    status_message TEXT,
                    anchor_name TEXT,
                    live_title TEXT,
                    is_live INTEGER NOT NULL DEFAULT 0,
                    output_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    started_at TEXT
                )
                """
            )

    @staticmethod
    def _serialize(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, bool):
            return int(value)
        return value

    @staticmethod
    def _to_record(row: sqlite3.Row | None) -> TaskRecord | None:
        if row is None:
            return None
        return TaskRecord.model_validate(dict(row))

    async def create(self, config: TaskConfig) -> TaskRecord:
        return await asyncio.to_thread(self._create_sync, config)

    def _create_sync(self, config: TaskConfig) -> TaskRecord:
        task_id = uuid.uuid4().hex
        now = utc_now().isoformat()
        data = config.model_dump()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recording_tasks (
                    id, url, label, quality, output_format, source, segment_seconds,
                    monitor, interval_seconds, enabled, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    task_id,
                    data["url"],
                    data["label"],
                    data["quality"],
                    data["output_format"],
                    data["source"],
                    data["segment_seconds"],
                    int(data["monitor"]),
                    data["interval_seconds"],
                    TaskStatus.STOPPED.value,
                    now,
                    now,
                ),
            )
        record = self._get_sync(task_id)
        assert record is not None
        return record

    async def get(self, task_id: str) -> TaskRecord | None:
        return await asyncio.to_thread(self._get_sync, task_id)

    def _get_sync(self, task_id: str) -> TaskRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM recording_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._to_record(row)

    async def list(self) -> list[TaskRecord]:
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self) -> list[TaskRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM recording_tasks ORDER BY created_at DESC").fetchall()
        return [record for row in rows if (record := self._to_record(row)) is not None]

    async def list_enabled(self) -> list[TaskRecord]:
        records = await self.list()
        return [record for record in records if record.enabled]

    async def update_config(self, task_id: str, changes: dict[str, Any]) -> TaskRecord | None:
        invalid = set(changes) - CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Invalid config fields: {', '.join(sorted(invalid))}")
        return await asyncio.to_thread(self._update_sync, task_id, changes)

    async def update_runtime(self, task_id: str, **changes: Any) -> TaskRecord | None:
        invalid = set(changes) - RUNTIME_COLUMNS
        if invalid:
            raise ValueError(f"Invalid runtime fields: {', '.join(sorted(invalid))}")
        return await asyncio.to_thread(self._update_sync, task_id, changes)

    def _update_sync(self, task_id: str, changes: dict[str, Any]) -> TaskRecord | None:
        if not changes:
            return self._get_sync(task_id)
        values = {key: self._serialize(value) for key, value in changes.items()}
        values["updated_at"] = utc_now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE recording_tasks SET {assignments} WHERE id = ?",  # Columns are allow-listed above.
                (*values.values(), task_id),
            )
        return self._get_sync(task_id)

    async def delete(self, task_id: str) -> bool:
        return await asyncio.to_thread(self._delete_sync, task_id)

    def _delete_sync(self, task_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM recording_tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0

    async def recover_interrupted(self) -> None:
        await asyncio.to_thread(self._recover_interrupted_sync)

    def _recover_interrupted_sync(self) -> None:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE recording_tasks
                SET status = CASE WHEN enabled = 1 THEN ? ELSE ? END,
                    status_message = CASE WHEN enabled = 1 THEN ? ELSE status_message END,
                    is_live = 0,
                    updated_at = ?
                WHERE status IN (?, ?, ?)
                """,
                (
                    TaskStatus.WAITING.value,
                    TaskStatus.STOPPED.value,
                    "服务重启，任务等待恢复",
                    now,
                    TaskStatus.CHECKING.value,
                    TaskStatus.QUEUED.value,
                    TaskStatus.RECORDING.value,
                ),
            )
