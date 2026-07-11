import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from douyin_recorder.web.schemas import TaskConfig, TaskStatus
from douyin_recorder.web.store import TaskStore


def task_config(**overrides) -> TaskConfig:
    values = {
        "url": "https://live.douyin.com/123456789",
        "label": "测试任务",
        "quality": "OD",
        "output_format": "ts",
        "source": "auto",
        "segment_seconds": 1800,
        "segment_count": 4,
        "monitor": True,
        "interval_seconds": 60,
    }
    values.update(overrides)
    return TaskConfig(**values)


class StoreTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.store = TaskStore(Path(self.temp_dir.name) / "tasks.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_create_update_list_and_delete(self) -> None:
        created = await self.store.create(task_config())
        self.assertFalse(created.enabled)
        self.assertEqual(created.status, TaskStatus.STOPPED)
        self.assertEqual(created.segment_count, 4)

        updated = await self.store.update_runtime(
            created.id,
            enabled=True,
            status=TaskStatus.RECORDING,
            status_message="正在录制",
            is_live=True,
        )
        self.assertTrue(updated.enabled)
        self.assertTrue(updated.is_live)
        self.assertEqual(updated.status, TaskStatus.RECORDING)

        renamed = await self.store.update_config(created.id, {"label": "新备注", "quality": "HD"})
        self.assertEqual(renamed.label, "新备注")
        self.assertEqual(renamed.quality, "HD")
        self.assertEqual([item.id for item in await self.store.list()], [created.id])
        self.assertTrue(await self.store.delete(created.id))
        self.assertIsNone(await self.store.get(created.id))

    async def test_recover_interrupted_enabled_task(self) -> None:
        created = await self.store.create(task_config())
        await self.store.update_runtime(
            created.id,
            enabled=True,
            status=TaskStatus.RECORDING,
            is_live=True,
        )

        await self.store.recover_interrupted()

        recovered = await self.store.get(created.id)
        self.assertTrue(recovered.enabled)
        self.assertFalse(recovered.is_live)
        self.assertEqual(recovered.status, TaskStatus.WAITING)
        self.assertIn("等待恢复", recovered.status_message)

    async def test_initialize_migrates_legacy_tasks_with_unlimited_segments(self) -> None:
        database_path = Path(self.temp_dir.name) / "legacy.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE recording_tasks (
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
            connection.execute(
                """
                INSERT INTO recording_tasks (
                    id, url, quality, output_format, source, segment_seconds,
                    monitor, interval_seconds, enabled, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-task",
                    "https://live.douyin.com/123456789",
                    "OD",
                    "ts",
                    "auto",
                    1800,
                    1,
                    60,
                    0,
                    "stopped",
                    "2026-07-12T00:00:00+00:00",
                    "2026-07-12T00:00:00+00:00",
                ),
            )
        connection.close()

        legacy_store = TaskStore(database_path)
        await legacy_store.initialize()
        migrated = await legacy_store.get("legacy-task")

        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.segment_count, 0)

    async def test_rejects_non_allowlisted_update_column(self) -> None:
        created = await self.store.create(task_config())
        with self.assertRaises(ValueError):
            await self.store.update_runtime(created.id, url="https://example.com")

    async def test_session_create_validate_expire_and_delete(self) -> None:
        token, created = await self.store.create_session("admin", 3600)
        self.assertTrue(token)
        self.assertNotIn(token.encode(), self.store.database_path.read_bytes())
        self.assertEqual(created.username, "admin")
        self.assertTrue(created.csrf_token)

        restored = await self.store.get_session(token)
        self.assertEqual(restored, created)
        self.assertTrue(await self.store.delete_session(token))
        self.assertIsNone(await self.store.get_session(token))

        expired_token, _ = await self.store.create_session("admin", -1)
        self.assertIsNone(await self.store.get_session(expired_token))

        restart_token, _ = await self.store.create_session("admin", 3600)
        await self.store.initialize()
        self.assertIsNone(await self.store.get_session(restart_token))
