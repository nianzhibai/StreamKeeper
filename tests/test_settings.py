import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from stream_keeper import LiveStreamClient
from stream_keeper.cloud import CloudArchiveConfig
from stream_keeper.settings import CLOUD_ARCHIVE_ROOT, ENV_PREFIX, Settings
from stream_keeper.web.events import _retention_from_env


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
            with self.assertRaisesRegex(RuntimeError, "STREAM_KEEPER_WEB_PASSWORD"):
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

    def test_stream_keeper_environment_namespace(self) -> None:
        environment = {
            "STREAM_KEEPER_DATA_DIR": "/tmp/stream-keeper-data",
            "STREAM_KEEPER_BIND_ADDRESS": "127.0.0.1",
            "STREAM_KEEPER_WEB_PORT": "55554",
            "STREAM_KEEPER_WEB_USERNAME": "operator",
            "STREAM_KEEPER_WEB_PASSWORD": "long-test-password",
            "STREAM_KEEPER_WEB_WORKERS": "1",
            "STREAM_KEEPER_UPLOAD_MODE": "recording_completed",
            "STREAM_KEEPER_PROXY": "http://127.0.0.1:7890",
            "STREAM_KEEPER_DOUYIN_COOKIE": "douyin=1",
            "STREAM_KEEPER_BILIBILI_COOKIE": "bili=1",
            "STREAM_KEEPER_KUAISHOU_COOKIE": "kwai=1",
            "STREAM_KEEPER_FFMPEG": "/usr/local/bin/ffmpeg",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(ENV_PREFIX, "STREAM_KEEPER_")
        self.assertEqual(settings.data_dir, Path("/tmp/stream-keeper-data"))
        self.assertEqual(settings.web_host, "127.0.0.1")
        self.assertEqual(settings.web_port, 55554)
        self.assertEqual(settings.web_username, "operator")
        self.assertEqual(settings.web_password, "long-test-password")
        self.assertEqual(settings.upload_mode, "recording_completed")
        self.assertEqual(settings.proxy, "http://127.0.0.1:7890")
        self.assertEqual(settings.douyin_cookies, "douyin=1")
        self.assertEqual(settings.bilibili_cookies, "bili=1")
        self.assertEqual(settings.kuaishou_cookies, "kwai=1")
        self.assertEqual(settings.ffmpeg, "/usr/local/bin/ffmpeg")

    def test_legacy_environment_names_are_not_used(self) -> None:
        legacy_environment = {
            "DOUYIN_WEB_USERNAME": "legacy-operator",
            "DOUYIN_WEB_PASSWORD": "legacy-password",
            "DOUYIN_UPLOAD_MODE": "recording_completed",
            "DOUYIN_COOKIE": "legacy-douyin=1",
            "BILIBILI_COOKIE": "legacy-bili=1",
            "KUAISHOU_COOKIE": "legacy-kwai=1",
            "WEB_CONCURRENCY": "2",
            "FFMPEG": "/legacy/ffmpeg",
        }
        with patch.dict(os.environ, legacy_environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.web_username, "admin")
        self.assertEqual(settings.web_password, "")
        self.assertEqual(settings.upload_mode, "scheduled")
        self.assertIsNone(settings.douyin_cookies)
        self.assertIsNone(settings.bilibili_cookies)
        self.assertIsNone(settings.kuaishou_cookies)
        self.assertEqual(settings.web_workers, 1)
        self.assertEqual(settings.ffmpeg, "ffmpeg")

    def test_event_retention_uses_stream_keeper_namespace(self) -> None:
        with patch.dict(os.environ, {"STREAM_KEEPER_EVENT_RETENTION": "4321"}, clear=True):
            self.assertEqual(_retention_from_env(), 4321)
        with patch.dict(os.environ, {"DOUYIN_EVENT_RETENTION": "9999"}, clear=True):
            self.assertEqual(_retention_from_env(), 5000)

    def test_invalid_upload_mode_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), upload_mode="immediately")
            with self.assertRaisesRegex(RuntimeError, "STREAM_KEEPER_UPLOAD_MODE"):
                settings.prepare()

    def test_platform_cookie_environment_variables(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STREAM_KEEPER_BILIBILI_COOKIE": "bili=1",
                "STREAM_KEEPER_KUAISHOU_COOKIE": "kwai=1",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.bilibili_cookies, "bili=1")
        self.assertEqual(settings.kuaishou_cookies, "kwai=1")

    def test_platform_cookies_reject_newlines(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), kuaishou_cookies="did=valid\nInjected: header")
            with self.assertRaisesRegex(RuntimeError, "STREAM_KEEPER_KUAISHOU_COOKIE"):
                settings.prepare()

    def test_deployment_templates_use_the_stream_keeper_namespace(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        env_lines = (project_root / ".env.example").read_text(encoding="utf-8").splitlines()
        env_keys = {
            line.split("=", 1)[0]
            for line in env_lines
            if line and not line.startswith("#") and "=" in line
        }
        self.assertTrue(env_keys)
        self.assertTrue(all(key == "TZ" or key.startswith(ENV_PREFIX) for key in env_keys))

        compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
        readme = (project_root / "README.md").read_text(encoding="utf-8")
        self.assertIn("STREAM_KEEPER_WEB_PASSWORD", compose)
        self.assertIn("STREAM_KEEPER_DOUYIN_COOKIE", compose)
        self.assertIn("STREAM_KEEPER_DATA_DIR=/data", dockerfile)
        self.assertIn("nianzhibai/StreamKeeper.git", readme)
        self.assertIn("STREAM_KEEPER_WEB_USERNAME=admin", readme)
        self.assertIn("STREAM_KEEPER_DOUYIN_COOKIE=", readme)
        self.assertNotIn("nianzhibai/DouYinStreamKeeper.git", readme)
        self.assertNotIn("\nDOUYIN_WEB_USERNAME=", readme)
        for obsolete in ("DOUYIN_WEB_USERNAME", "DOUYIN_WEB_PASSWORD", "WEB_CONCURRENCY"):
            self.assertNotIn(obsolete, compose)
            self.assertNotIn(obsolete, dockerfile)
