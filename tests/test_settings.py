import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from stream_keeper import LiveStreamClient
from stream_keeper.cloud import CloudArchiveConfig
from stream_keeper.settings import CLOUD_ARCHIVE_ROOT, ENV_PREFIX, Settings, load_environment_file
from stream_keeper.web import server
from stream_keeper.web.events import _retention_from_env


def make_settings(root: Path, **overrides) -> Settings:
    values = {
        "data_dir": root,
        "recordings_dir": root / "recordings",
        "database_path": root / "tasks.db",
        "validate_binaries": False,
    }
    values.update(overrides)
    return Settings(**values)


class SettingsTests(TestCase):
    def test_environment_file_is_loaded_without_overriding_process_environment(self) -> None:
        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "STREAM_KEEPER_WEB_PORT=9000\n"
                'STREAM_KEEPER_DOUYIN_COOKIE="session=from-file; token=a#b"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"STREAM_KEEPER_WEB_PORT": "9100"}, clear=True):
                loaded = load_environment_file(env_file)
                settings = Settings.from_env()

        self.assertTrue(loaded)
        self.assertEqual(settings.web_port, 9100)
        self.assertEqual(settings.douyin_cookies, "session=from-file; token=a#b")

    def test_missing_environment_file_preserves_defaults(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            loaded = load_environment_file(Path(tmp) / ".env")
            settings = Settings.from_env()

        self.assertFalse(loaded)
        self.assertEqual(settings.web_port, 8000)

    def test_default_configuration_and_data_paths_do_not_depend_on_working_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            application_directory = root / ".streamkeeper"
            application_directory.mkdir()
            (application_directory / ".env").write_text("STREAM_KEEPER_WEB_PORT=9000\n", encoding="utf-8")
            first_working_directory = root / "first"
            second_working_directory = root / "second"
            first_working_directory.mkdir()
            second_working_directory.mkdir()
            original_working_directory = Path.cwd()
            try:
                settings_by_working_directory = []
                for working_directory in (first_working_directory, second_working_directory):
                    os.chdir(working_directory)
                    with (
                        patch("stream_keeper.settings._application_directory", return_value=application_directory),
                        patch.dict(os.environ, {}, clear=True),
                    ):
                        self.assertTrue(load_environment_file())
                        settings_by_working_directory.append(Settings.from_env())
            finally:
                os.chdir(original_working_directory)

        for settings in settings_by_working_directory:
            self.assertEqual(settings.web_port, 9000)
            self.assertEqual(settings.data_dir, application_directory / "data")
            self.assertEqual(settings.recordings_dir, application_directory / "data" / "recordings")
            self.assertEqual(settings.database_path, application_directory / "data" / "tasks.db")

    def test_python_entrypoint_loads_environment_file_before_reading_settings(self) -> None:
        calls: list[str] = []
        settings = Settings(validate_binaries=False)
        with (
            patch.object(server, "load_environment_file", side_effect=lambda: calls.append("load")),
            patch.object(Settings, "from_env", side_effect=lambda: (calls.append("settings"), settings)[1]),
            patch.object(server, "create_app", return_value=object()),
            patch.object(server.uvicorn, "run"),
        ):
            self.assertEqual(server.main(), 0)

        self.assertEqual(calls, ["load", "settings"])

    def test_session_defaults_to_seven_days(self) -> None:
        settings = Settings()
        self.assertEqual(settings.session_ttl_hours, 24 * 7)
        self.assertEqual(settings.login_max_attempts, 3)
        self.assertEqual(settings.login_window_seconds, 3600)
        self.assertEqual(settings.quark_upload_path, CLOUD_ARCHIVE_ROOT)
        self.assertEqual(settings.wopan_upload_path, CLOUD_ARCHIVE_ROOT)
        self.assertEqual(settings.upload_mode, "scheduled")
        self.assertEqual(settings.max_concurrent_recordings, 3)

    def test_legacy_archive_config_defaults_to_scheduled_mode(self) -> None:
        config = CloudArchiveConfig.from_dict({"providers": {}})

        self.assertEqual(config.upload_mode, "scheduled")

    def test_prepare_creates_server_directories(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp) / "data")
            settings.prepare()
            self.assertTrue(settings.recordings_dir.is_dir())
            self.assertTrue(settings.database_path.parent.is_dir())

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
                {"max_concurrent_recordings": 0},
                {"max_concurrent_recordings": 101},
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
            "STREAM_KEEPER_WEB_WORKERS": "1",
            "STREAM_KEEPER_UPLOAD_MODE": "recording_completed",
            "STREAM_KEEPER_MAX_CONCURRENT_RECORDINGS": "8",
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
        self.assertEqual(settings.upload_mode, "recording_completed")
        self.assertEqual(settings.max_concurrent_recordings, 8)
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
        self.assertNotIn("STREAM_KEEPER_WEB_USERNAME", env_keys)
        self.assertNotIn("STREAM_KEEPER_WEB_PASSWORD", env_keys)

        compose = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
        readme = (project_root / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("STREAM_KEEPER_WEB_USERNAME", compose)
        self.assertNotIn("STREAM_KEEPER_WEB_PASSWORD", compose)
        self.assertIn("STREAM_KEEPER_DOUYIN_COOKIE", compose)
        self.assertIn("STREAM_KEEPER_DATA_DIR=/data", dockerfile)
        self.assertIn("releases/latest/download/streamkeeper-source.tar.gz", readme)
        self.assertIn("首次访问", readme)
        self.assertIn("管理员用户名和密码", readme)
        self.assertIn("STREAM_KEEPER_*_COOKIE", readme)
        self.assertNotIn("nianzhibai/DouYinStreamKeeper.git", readme)
        self.assertNotIn("\nDOUYIN_WEB_USERNAME=", readme)
        for obsolete in ("DOUYIN_WEB_USERNAME", "DOUYIN_WEB_PASSWORD", "WEB_CONCURRENCY"):
            self.assertNotIn(obsolete, compose)
            self.assertNotIn(obsolete, dockerfile)
