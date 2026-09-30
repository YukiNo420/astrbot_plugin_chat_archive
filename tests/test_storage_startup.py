import asyncio
import importlib
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


# Exercise plugin helpers without loading a real AstrBot service or its data.
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("archive-tests")
star = types.ModuleType("astrbot.api.star")
star.StarTools = Mock()
sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
sys.modules["astrbot.api"] = api
sys.modules["astrbot.api.star"] = star
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plugin = self.root / "data" / "plugins" / config.PLUGIN_NAME
        self.plugin.mkdir(parents=True)
        self.target = self.root / "data" / "plugin_data" / config.PLUGIN_NAME
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.location = patch.object(config, "PLUGIN_DIR", self.plugin)
        self.location.start()
        star.StarTools.reset_mock()
        star.StarTools.get_data_dir.side_effect = None
        star.StarTools.get_data_dir.return_value = self.target

    def tearDown(self):
        self.location.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_explicit_plugin_name(self):
        self.assertEqual(config.get_data_dir(), self.target)
        star.StarTools.get_data_dir.assert_called_once_with(config.PLUGIN_NAME)

    def test_old_api_uses_persistent_sibling(self):
        star.StarTools.get_data_dir.side_effect = TypeError("old signature")
        self.assertEqual(config.get_data_dir(), self.target)

    def test_permission_failure_does_not_fall_back(self):
        star.StarTools.get_data_dir.side_effect = RuntimeError("permission denied")
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            config.get_data_dir()

    def test_legacy_data_blocks_empty_database_and_preserves_bytes(self):
        legacy = self.plugin / "data"
        legacy.mkdir()
        db = legacy / "chat_history.db"
        db.write_bytes(b"existing archive")
        with self.assertRaisesRegex(RuntimeError, "Legacy archive data"):
            config.load_db_path()
        self.assertEqual(db.read_bytes(), b"existing archive")
        self.assertFalse((self.target / "chat_history.db").exists())

    def test_environment_override_preserves_legacy_access(self):
        legacy = self.plugin / "data"
        legacy.mkdir()
        (legacy / "chat_history.db").write_bytes(b"existing archive")
        os.environ["ARCHIVE_DATA_DIR"] = str(legacy)
        self.assertEqual(config.load_db_path(), str(legacy / "chat_history.db"))

    def test_relative_database_and_config_paths(self):
        os.environ["ARCHIVE_DB_PATH"] = "custom/history.db"
        self.assertEqual(config.load_db_path(), str(self.target / "custom" / "history.db"))
        self.assertEqual(config.get_config_path(), self.root / "data" / "config" / (config.PLUGIN_NAME + "_config.json"))


class WebStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.environment = patch.dict(os.environ, {
            "ARCHIVE_DATA_DIR": cls.temp.name,
            "ARCHIVE_API_KEY": "test-key",
        })
        cls.environment.start()
        cls.web = importlib.import_module("web.server")

    @classmethod
    def tearDownClass(cls):
        import db_config
        db_config.get_connection_pool().close_all()
        cls.environment.stop()
        cls.temp.cleanup()

    def test_bind_failure_is_logged(self):
        server = self.web.AdminServer(None, port=0, api_key="test-key")
        async def fail():
            raise SystemExit(1)
        server.server.serve = fail
        with self.assertLogs(api.logger, level="ERROR") as logs:
            with patch.object(self.web, "_load_custom_apis"):
                server.run_in_thread()
                server.thread.join(3)
        self.assertTrue(any("WebUI failed" in line for line in logs.output))
        self.assertFalse(server.thread.is_alive())

    def test_real_server_starts_stops_and_restarts(self):
        import time
        import httpx
        for _ in range(2):
            server = self.web.AdminServer(None, port=0, api_key="test-key")
            # Reserve an ephemeral loopback socket owned only by this test.
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            server.config.port = port
            with patch.object(self.web, "_load_custom_apis"):
                server.run_in_thread()
            try:
                deadline = time.monotonic() + 5
                while not server.server.started and server.thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.server.started)
                response = httpx.get(f"http://127.0.0.1:{port}/", trust_env=False)
                self.assertEqual(response.status_code, 200)
            finally:
                asyncio.run(server.stop())
            self.assertFalse(server.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
