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

    async def test_rejects_non_allowlisted_update_column(self) -> None:
        created = await self.store.create(task_config())
        with self.assertRaises(ValueError):
            await self.store.update_runtime(created.id, url="https://example.com")
