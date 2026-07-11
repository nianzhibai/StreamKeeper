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

    def test_cookie_file_takes_precedence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookie_file = root / "cookie.txt"
            cookie_file.write_text("from=file\n", encoding="utf-8")
            settings = make_settings(root, cookies="from=env", cookie_file=cookie_file)
            self.assertEqual(settings.load_cookies(), "from=file")
