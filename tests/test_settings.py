from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from douyin_recorder.settings import Settings


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
                (("quark", "/recordings"), ("wopan", "/recordings")),
            )

    def test_incomplete_native_upload_configuration_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid_values = (
                {"quark_upload_path": "/recordings"},
                {"quark_cookie": "cookie=value"},
                {"quark_cookie": "cookie=value", "quark_upload_path": "/../escape"},
                {"wopan_refresh_token": "refresh-token"},
                {"wopan_upload_path": "/recordings"},
                {"wopan_access_token": "short", "wopan_upload_path": "/recordings"},
            )
            for overrides in invalid_values:
                with self.subTest(overrides=overrides), self.assertRaises(RuntimeError):
                    make_settings(root, **overrides).prepare()

    def test_wopan_can_start_with_refresh_token_only(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(
                Path(tmp),
                wopan_refresh_token="refresh-token",
                wopan_upload_path="/DouYinStreamKeeper",
            )
            settings.prepare()
            self.assertEqual(settings.upload_targets, (("wopan", "/DouYinStreamKeeper"),))

    def test_cookie_file_takes_precedence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookie_file = root / "cookie.txt"
            cookie_file.write_text("from=file\n", encoding="utf-8")
            settings = make_settings(root, cookies="from=env", cookie_file=cookie_file)
            self.assertEqual(settings.load_cookies(), "from=file")
