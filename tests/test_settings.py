import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from stream_keeper import LiveStreamClient
from stream_keeper.cloud import CloudArchiveConfig
from stream_keeper.settings import CLOUD_ARCHIVE_ROOT, Settings


def make_settings(root: Path, **overrides) -> Settings:
    values = {
        "data_dir": root,
        "recordings_dir": root / "recordings",
        "database_path": root / "tasks.db",
        "web_password": "test-password",
        "validate_binaries": False,
    }
    values.update(overrides)
    return Settings(**values)


class SettingsTests(TestCase):
    def test_session_defaults_to_seven_days(self) -> None:
        settings = Settings()
        self.assertEqual(settings.session_ttl_hours, 24 * 7)
        self.assertEqual(settings.login_max_attempts, 3)
        self.assertEqual(settings.login_window_seconds, 3600)
        self.assertEqual(settings.quark_upload_path, CLOUD_ARCHIVE_ROOT)
        self.assertEqual(settings.wopan_upload_path, CLOUD_ARCHIVE_ROOT)
        self.assertEqual(settings.upload_mode, "scheduled")

    def test_legacy_archive_config_defaults_to_scheduled_mode(self) -> None:
        config = CloudArchiveConfig.from_dict({"providers": {}})

        self.assertEqual(config.upload_mode, "scheduled")

    def test_prepare_creates_server_directories(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp) / "data")
            settings.prepare()
            self.assertTrue(settings.recordings_dir.is_dir())
            self.assertTrue(settings.database_path.parent.is_dir())

    def test_password_is_required(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), web_password="")
            with self.assertRaisesRegex(RuntimeError, "DOUYIN_WEB_PASSWORD"):
                settings.prepare()

    def test_short_password_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), web_password="too-short")
            with self.assertRaisesRegex(RuntimeError, "至少需要 10"):
                settings.prepare()

    def test_multiple_web_workers_are_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), web_workers=2)
            with self.assertRaisesRegex(RuntimeError, "单 Web worker"):
                settings.prepare()

    def test_session_and_login_limits_are_validated(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid_values = (
                {"session_ttl_hours": 0},
                {"login_max_attempts": 0},
                {"login_window_seconds": 9},
            )
            for overrides in invalid_values:
                with self.subTest(overrides=overrides), self.assertRaises(RuntimeError):
                    make_settings(root, **overrides).prepare()

    def test_native_cloud_upload_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(
                Path(tmp),
                quark_cookie="__uid=123; __puus=abc",
                quark_upload_path="/recordings",
                wopan_access_token="1234567890abcdef-access",
                wopan_refresh_token="refresh-token",
                wopan_upload_path="/recordings",
            )
            settings.prepare()

            self.assertTrue(settings.upload_enabled)
            self.assertEqual(
                settings.upload_targets,
                (("quark", CLOUD_ARCHIVE_ROOT), ("wopan", CLOUD_ARCHIVE_ROOT)),
            )

    def test_incomplete_native_upload_configuration_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid_values = (
                {"quark_root_id": "folder-without-cookie"},
                {"wopan_family_id": "family-without-token"},
                {"wopan_access_token": "short"},
                {"baidu_access_token": "access", "baidu_refresh_token": "refresh"},
            )
            for overrides in invalid_values:
                with self.subTest(overrides=overrides), self.assertRaises(RuntimeError):
                    make_settings(root, **overrides).prepare()

    def test_wopan_can_start_with_refresh_token_only(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(
                Path(tmp),
                wopan_refresh_token="refresh-token",
            )
            settings.prepare()
            self.assertEqual(settings.upload_targets, (("wopan", CLOUD_ARCHIVE_ROOT),))

    def test_cookie_file_takes_precedence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookie_file = root / "cookie.txt"
            cookie_file.write_text("from=file\n", encoding="utf-8")
            settings = make_settings(
                root,
                douyin_cookies="from=env",
                douyin_cookie_file=cookie_file,
            )
            self.assertEqual(settings.load_douyin_cookies(), "from=file")

    def test_platform_cookies_are_loaded_into_unified_client(self) -> None:
        settings = Settings(
            douyin_cookies="douyin=1",
            bilibili_cookies="bilibili=1",
            kuaishou_cookies="kuaishou=1",
        )

        client = settings.create_client()

        self.assertIsInstance(client, LiveStreamClient)
        self.assertEqual(
            client._cookies,
            {"douyin": "douyin=1", "bilibili": "bilibili=1", "kuaishou": "kuaishou=1"},
        )

    def test_upload_mode_environment_variable(self) -> None:
        with patch.dict(os.environ, {"DOUYIN_UPLOAD_MODE": "recording_completed"}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.upload_mode, "recording_completed")

    def test_invalid_upload_mode_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), upload_mode="immediately")
            with self.assertRaisesRegex(RuntimeError, "DOUYIN_UPLOAD_MODE"):
                settings.prepare()

    def test_platform_cookie_environment_variables(self) -> None:
        with patch.dict(os.environ, {"BILIBILI_COOKIE": "bili=1", "KUAISHOU_COOKIE": "kwai=1"}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.bilibili_cookies, "bili=1")
        self.assertEqual(settings.kuaishou_cookies, "kwai=1")

    def test_platform_cookies_reject_newlines(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), kuaishou_cookies="did=valid\nInjected: header")
            with self.assertRaisesRegex(RuntimeError, "KUAISHOU_COOKIE"):
                settings.prepare()
