import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from stream_keeper.web.schemas import RecordingDefaults, TaskConfig, TaskStatus
from stream_keeper.web.store import TaskStore


def task_config(**overrides) -> TaskConfig:
    values = {
        "url": "https://live.douyin.com/123456789",
        "label": "测试任务",
        "quality": "OD",
        "output_format": "ts",
        "source": "auto",
        "segment_seconds": 1800,
        "segment_count": 0,
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

    async def test_recording_defaults_are_initialized_and_persisted(self) -> None:
        initial = await self.store.get_recording_defaults()
        self.assertEqual(initial, RecordingDefaults())

        configured = RecordingDefaults(output_format="mkv", segment_seconds=600, segment_count=4)
        await self.store.save_recording_defaults(configured)
        await self.store.initialize()

        self.assertEqual(await self.store.get_recording_defaults(), configured)

    async def test_create_update_list_and_delete(self) -> None:
        created = await self.store.create(task_config())
        self.assertFalse(created.enabled)
        self.assertEqual(created.status, TaskStatus.STOPPED)
        self.assertEqual(created.segment_count, 0)
        self.assertTrue(created.monitor)

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

    async def test_supported_platform_urls_share_the_existing_task_schema(self) -> None:
        for url in (
            "https://live.bilibili.com/123456",
            "https://live.kuaishou.com/u/example",
        ):
            with self.subTest(url=url):
                created = await self.store.create(task_config(url=url))
                self.assertEqual(created.url, url)

    async def test_finite_segment_limit_disables_monitoring(self) -> None:
        created = await self.store.create(task_config(segment_count=4, monitor=True))

        self.assertEqual(created.segment_count, 4)
        self.assertFalse(created.monitor)

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

    async def test_initialize_normalizes_legacy_limited_monitoring_tasks(self) -> None:
        created = await self.store.create(task_config())
        with sqlite3.connect(self.store.database_path) as connection:
            connection.execute(
                "UPDATE recording_tasks SET segment_count = 4, monitor = 1 WHERE id = ?",
                (created.id,),
            )

        await self.store.initialize()
        normalized = await self.store.get(created.id)
        with sqlite3.connect(self.store.database_path) as connection:
            stored_monitor = connection.execute(
                "SELECT monitor FROM recording_tasks WHERE id = ?",
                (created.id,),
            ).fetchone()[0]

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized.segment_count, 4)
        self.assertFalse(normalized.monitor)
        self.assertEqual(stored_monitor, 0)

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
        self.assertIsNotNone(await self.store.get_session(restart_token))

    async def test_session_is_renewed_only_inside_sliding_window(self) -> None:
        token, created = await self.store.create_session("admin", 3600)

        unchanged, renewed = await self.store.renew_session_if_needed(
            token,
            ttl_seconds=7200,
            renew_before_seconds=60,
        )
        self.assertFalse(renewed)
        self.assertEqual(unchanged, created)

        extended, renewed = await self.store.renew_session_if_needed(
            token,
            ttl_seconds=7200,
            renew_before_seconds=3600,
        )
        self.assertTrue(renewed)
        self.assertGreater(extended.expires_at, created.expires_at)

        unchanged_again, renewed = await self.store.renew_session_if_needed(
            token,
            ttl_seconds=7200,
            renew_before_seconds=3600,
        )
        self.assertFalse(renewed)
        self.assertEqual(unchanged_again, extended)

    async def test_credential_change_revokes_all_sessions(self) -> None:
        legacy_token, _ = await self.store.create_session("admin", 3600)
        self.assertTrue(await self.store.sync_web_credentials("admin", "first-long-password"))
        self.assertIsNone(await self.store.get_session(legacy_token))

        first_token, _ = await self.store.create_session("admin", 3600)
        second_token, _ = await self.store.create_session("admin", 3600)

        self.assertFalse(await self.store.sync_web_credentials("admin", "first-long-password"))
        self.assertIsNotNone(await self.store.get_session(first_token))
        self.assertIsNotNone(await self.store.get_session(second_token))

        self.assertTrue(await self.store.sync_web_credentials("admin", "second-long-password"))
        self.assertIsNone(await self.store.get_session(first_token))
        self.assertIsNone(await self.store.get_session(second_token))
        self.assertNotIn(b"second-long-password", self.store.database_path.read_bytes())

        replacement_token, _ = await self.store.create_session("admin", 3600)
        await self.store.initialize()
        self.assertFalse(await self.store.sync_web_credentials("admin", "second-long-password"))
        self.assertIsNotNone(await self.store.get_session(replacement_token))

    async def test_refreshed_cloud_credentials_survive_until_environment_changes(self) -> None:
        defaults = {"access_token": "old-access", "refresh_token": "old-refresh"}
        self.assertEqual(
            await self.store.resolve_cloud_credentials("wopan", "fingerprint-1", defaults),
            defaults,
        )

        refreshed = {"access_token": "new-access", "refresh_token": "new-refresh"}
        await self.store.save_cloud_credentials("wopan", "fingerprint-1", refreshed)
        await self.store.initialize()
        self.assertEqual(
            await self.store.resolve_cloud_credentials("wopan", "fingerprint-1", defaults),
            refreshed,
        )

        replacement = {"access_token": "replacement", "refresh_token": "replacement-refresh"}
        self.assertEqual(
            await self.store.resolve_cloud_credentials("wopan", "fingerprint-2", replacement),
            replacement,
        )

    async def test_web_cloud_config_overrides_later_environment_changes(self) -> None:
        environment = {"quark_enabled": False, "upload_hour": 1}
        self.assertEqual(
            await self.store.sync_cloud_upload_config("environment-1", environment),
            environment,
        )

        changed_environment = {"quark_enabled": True, "upload_hour": 2}
        self.assertEqual(
            await self.store.sync_cloud_upload_config("environment-2", changed_environment),
            changed_environment,
        )

        web_config = {"quark_enabled": True, "upload_hour": 3}
        await self.store.save_cloud_upload_config(web_config)
        await self.store.initialize()
        self.assertEqual(
            await self.store.sync_cloud_upload_config("environment-3", environment),
            web_config,
        )

    async def test_login_failures_create_persistent_blacklist(self) -> None:
        client_key = "203.0.113.10"
        self.assertFalse(await self.store.is_login_blacklisted(client_key))
        self.assertFalse(await self.store.register_login_failure(client_key, 3, 3600))
        self.assertFalse(await self.store.register_login_failure(client_key, 3, 3600))
        self.assertTrue(await self.store.register_login_failure(client_key, 3, 3600))

        await self.store.initialize()
        self.assertTrue(await self.store.is_login_blacklisted(client_key))
        self.assertFalse(await self.store.accept_login_success(client_key))
        self.assertEqual(await self.store.list_login_blacklist(), [client_key])

        self.assertTrue(await self.store.unblock_login_client(client_key))
        self.assertFalse(await self.store.is_login_blacklisted(client_key))
        self.assertTrue(await self.store.accept_login_success(client_key))

    async def test_successful_login_clears_failures_before_blacklist(self) -> None:
        client_key = "198.51.100.20"
        self.assertFalse(await self.store.register_login_failure(client_key, 3, 3600))
        self.assertFalse(await self.store.register_login_failure(client_key, 3, 3600))
        self.assertTrue(await self.store.accept_login_success(client_key))

        self.assertFalse(await self.store.register_login_failure(client_key, 3, 3600))
        self.assertFalse(await self.store.is_login_blacklisted(client_key))
