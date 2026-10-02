from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from media_cache import resolve_public_targets
from web import server


class StandaloneLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_legacy_database_blocks_empty_destination_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "plugin-data"
            legacy_dir = root / "legacy"
            legacy_dir.mkdir()
            (legacy_dir / "chat_history.db").write_bytes(b"legacy-history")
            state = SimpleNamespace(schema_ready=False)

            with (
                mock.patch.object(server, "get_data_dir", return_value=data_dir),
                mock.patch.object(
                    server,
                    "get_legacy_data_dir",
                    return_value=legacy_dir,
                ),
                mock.patch.object(
                    server,
                    "DB_PATH",
                    str(data_dir / "chat_history.db"),
                ),
                mock.patch.object(server, "init_db") as init_db,
            ):
                with self.assertRaisesRegex(RuntimeError, "still needs migration"):
                    async with server.lifespan(SimpleNamespace(state=state)):
                        pass

            init_db.assert_not_called()
            self.assertFalse((data_dir / "chat_history.db").exists())

    async def test_custom_database_path_does_not_wait_for_default_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "plugin-data"
            legacy_dir = root / "legacy"
            legacy_dir.mkdir()
            (legacy_dir / "chat_history.db").write_bytes(b"legacy-history")
            state = SimpleNamespace(schema_ready=False)

            with (
                mock.patch.object(server, "get_data_dir", return_value=data_dir),
                mock.patch.object(
                    server,
                    "get_legacy_data_dir",
                    return_value=legacy_dir,
                ),
                mock.patch.object(server, "DB_PATH", str(root / "custom.db")),
                mock.patch.object(server, "init_db") as init_db,
            ):
                async with server.lifespan(SimpleNamespace(state=state)):
                    pass

            init_db.assert_called_once_with()


class _FakeStreamContent:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    async def read(self, size: int) -> bytes:
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    async def iter_chunked(self, size: int):
        while self.offset < len(self.data):
            yield await self.read(size)


class _FakeUpstreamResponse:
    def __init__(self, *, status: int, headers: dict[str, str], data: bytes):
        self.status = status
        self.headers = headers
        self.content = _FakeStreamContent(data)

    def release(self):
        return None

    async def wait_for_close(self):
        return None


class _FakeUpstreamSession:
    def __init__(self, response: _FakeUpstreamResponse):
        self.response = response
        self.request_headers = {}

    async def get(self, _url, *, headers, allow_redirects):
        self.request_headers = headers
        self.allow_redirects = allow_redirects
        return self.response

    async def close(self):
        return None


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.primary = tempfile.TemporaryDirectory()
        self.legacy = tempfile.TemporaryDirectory()
        self.old_cache_static_dir = server.cache_static_dir
        self.old_dirs = server._CACHE_STATIC_DIRS
        self.old_api_key = server.API_KEY
        self.old_plugin = getattr(server.app.state, "plugin", None)
        self.old_schema_ready = getattr(server.app.state, "schema_ready", False)
        with server._SESSION_TOKENS_LOCK:
            self.old_tokens = dict(server._SESSION_TOKENS)
            server._SESSION_TOKENS.clear()
        with server._AUTH_FAILURES_LOCK:
            self.old_failures = dict(server._AUTH_FAILURES)
            server._AUTH_FAILURES.clear()
        server._CACHE_STATIC_DIRS = (
            Path(self.primary.name),
            Path(self.legacy.name),
        )
        server._static_cache_exists_cached.cache_clear()
        server.API_KEY = "test-api-key"
        server.app.state.schema_ready = True
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()
        server.API_KEY = self.old_api_key
        server.cache_static_dir = self.old_cache_static_dir
        server._CACHE_STATIC_DIRS = self.old_dirs
        server.app.state.plugin = self.old_plugin
        server.app.state.schema_ready = self.old_schema_ready
        server._static_cache_exists_cached.cache_clear()
        with server._SESSION_TOKENS_LOCK:
            server._SESSION_TOKENS.clear()
            server._SESSION_TOKENS.update(self.old_tokens)
        with server._AUTH_FAILURES_LOCK:
            server._AUTH_FAILURES.clear()
            server._AUTH_FAILURES.update(self.old_failures)
        self.primary.cleanup()
        self.legacy.cleanup()

    def _headers(self):
        return {"X-API-Key": server.API_KEY}

    def test_non_ascii_api_key_is_compared_without_500(self):
        server.API_KEY = "密钥-安全"
        failed = self.client.post(
            "/api/auth/verify",
            json={"api_key": "错误-密钥"},
        )
        self.assertEqual(failed.status_code, 401)
        self.assertEqual(len(server._AUTH_FAILURES), 1)

        success = self.client.post(
            "/api/auth/verify",
            json={"api_key": server.API_KEY},
        )
        self.assertEqual(success.status_code, 200)
        self.assertTrue(success.json()["success"])

    def test_stats_ignores_time_bounds_that_cover_the_whole_archive(self):
        summary = {
            "total": 2,
            "today_total": 0,
            **{f"slot_{slot}": 0 for slot in range(12)},
        }
        db = mock.MagicMock()

        def execute(sql, _params=None):
            if "ORDER BY timestamp ASC" in sql:
                return SimpleNamespace(fetchone=lambda: {"timestamp": 10})
            if "ORDER BY timestamp DESC" in sql:
                return SimpleNamespace(fetchone=lambda: {"timestamp": 100})
            if "SELECT COUNT(*) as total" in sql:
                return SimpleNamespace(fetchone=lambda: summary)
            self.fail(f"unexpected SQL: {sql}")

        db.execute.side_effect = execute
        with (
            mock.patch.object(server, "get_db_connection", return_value=db),
            mock.patch.object(server, "_table_has_columns", return_value=True),
            mock.patch.object(
                server, "_fetch_user_counts_from_stats", return_value=[]
            ) as from_stats,
            mock.patch.object(server, "_fetch_user_counts") as from_history,
            mock.patch.object(server, "_STATS_CACHE_TTL", 0),
        ):
            response = self.client.get(
                "/api/stats?session_id=group:1&time_start=1&time_end=1000",
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        from_stats.assert_called_once()
        from_history.assert_not_called()
        db.close.assert_called_once()

    def test_api_key_rotation_revokes_old_sessions_and_applies_at_start_boundary(self):
        old_plugin = object()
        next_plugin = object()
        server.app.state.plugin = old_plugin
        server._set_api_key("old-key")
        old_token = server._create_session_token()
        server._record_auth_failure("rotating-client")

        candidate = server.AdminServer(
            plugin_instance=next_plugin,
            api_key="new-key",
            cache_dir=Path(self.primary.name),
        )

        self.assertEqual(server.API_KEY, "old-key")
        self.assertTrue(server._validate_session_token(old_token))
        self.assertIs(server.app.state.plugin, old_plugin)

        with mock.patch.dict(os.environ, {"ARCHIVE_API_KEY": ""}):
            candidate._apply_runtime_state()

        self.assertEqual(server.API_KEY, "new-key")
        self.assertFalse(server._validate_session_token(old_token))
        self.assertFalse(server._AUTH_FAILURES)
        self.assertIs(server.app.state.plugin, next_plugin)

    def test_same_key_preserves_session_and_environment_overrides_constructor(self):
        server._set_api_key("same-key")
        token = server._create_session_token()
        same = server.AdminServer(plugin_instance=object(), api_key="same-key")
        with mock.patch.dict(os.environ, {"ARCHIVE_API_KEY": ""}):
            same._apply_runtime_state()
        self.assertTrue(server._validate_session_token(token))

        override = server.AdminServer(
            plugin_instance=object(), api_key="constructor-key"
        )
        with mock.patch.dict(os.environ, {"ARCHIVE_API_KEY": "environment-key"}):
            override._apply_runtime_state()
        self.assertEqual(server.API_KEY, "environment-key")
        self.assertFalse(server._validate_session_token(token))

    def test_protected_header_failures_share_login_lockout(self):
        self.client.cookies.clear()
        with (
            mock.patch.object(server, "_AUTH_FAILURE_LIMIT", 2),
            mock.patch.object(server, "_AUTH_LOCKOUT_SECONDS", 60),
        ):
            first = self.client.get(
                "/api/stats",
                headers={"X-API-Key": "wrong-one"},
            )
            locked = self.client.get(
                "/api/stats",
                headers={"X-API-Key": "wrong-two"},
            )
            correct_while_locked = self.client.get(
                "/api/stats",
                headers={"X-API-Key": server.API_KEY},
            )
            login_while_locked = self.client.post(
                "/api/auth/verify",
                json={"api_key": server.API_KEY},
            )

        self.assertEqual(first.status_code, 401)
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(correct_while_locked.status_code, 429)
        self.assertEqual(login_while_locked.status_code, 429)
        self.assertGreaterEqual(int(locked.headers["retry-after"]), 1)

    def test_cors_preflight_bypasses_auth_and_is_validated_by_cors(self):
        allowed = self.client.options(
            "/api/history",
            headers={
                "Origin": "http://localhost:8090",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )
        denied = self.client.options(
            "/api/history",
            headers={
                "Origin": "https://not-allowed.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.headers.get("access-control-allow-origin"),
            "http://localhost:8090",
        )
        self.assertNotEqual(denied.status_code, 401)
        self.assertNotIn("access-control-allow-origin", denied.headers)

    def test_root_response_blocks_framing_and_base_injection(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        csp = response.headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("object-src 'none'", csp)

    def test_public_frontend_assets_require_revalidation(self):
        response = self.client.get("/static/css/main.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response.headers["cache-control"])
        self.assertIn("must-revalidate", response.headers["cache-control"])

    def test_cache_rejects_svg_disguised_as_jpeg(self):
        name = f"{'a' * 32}.jpg"
        Path(self.primary.name, name).write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            encoding="utf-8",
        )
        response = self.client.get(f"/static/cache/{name}", headers=self._headers())
        self.assertEqual(response.status_code, 404)

    def test_legacy_telegram_avatar_is_served_with_security_headers(self):
        name = f"telegram_avatar_{'b' * 32}.jpg"
        Path(self.legacy.name, name).write_bytes(b"\xff\xd8\xff" + b"x" * 32)
        response = self.client.get(f"/static/cache/{name}", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", response.headers["content-security-policy"])
        self.assertIn("no-store", response.headers["cache-control"])

    def test_telegram_document_is_forced_to_attachment(self):
        name = f"telegram_media_{'c' * 32}.pdf"
        Path(self.primary.name, name).write_bytes(b"%PDF-1.7\n" + b"x" * 32)
        response = self.client.get(f"/static/cache/{name}", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertTrue(
            response.headers["content-disposition"].lower().startswith("attachment;")
        )

    def test_cached_video_supports_range_requests(self):
        name = f"telegram_media_{'d' * 32}.mp4"
        payload = b"\x00\x00\x00\x18ftypisom" + b"x" * 64
        Path(self.primary.name, name).write_bytes(payload)
        response = self.client.get(
            f"/static/cache/{name}",
            headers={**self._headers(), "Range": "bytes=0-11"},
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, payload[:12])
        self.assertEqual(
            response.headers["content-range"], f"bytes 0-11/{len(payload)}"
        )

    def test_cache_filename_policy_blocks_uncontrolled_names(self):
        Path(self.primary.name, "payload.pdf").write_bytes(b"%PDF-1.7\n")
        response = self.client.get(
            "/static/cache/payload.pdf",
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 404)

    def test_cache_route_rejects_symlinks(self):
        target = Path(self.legacy.name) / "outside.jpg"
        target.write_bytes(b"\xff\xd8\xff" + b"x" * 32)
        name = f"{'e' * 32}.jpg"
        (Path(self.primary.name) / name).symlink_to(target)
        response = self.client.get(f"/static/cache/{name}", headers=self._headers())
        self.assertEqual(response.status_code, 404)

    def test_svg_is_not_an_allowed_proxy_type(self):
        self.assertNotIn("image/svg+xml", server._SAFE_MEDIA_TYPES)

    def test_extension_loader_rejects_group_writable_paths(self):
        directory = Path(self.primary.name) / "ext"
        directory.mkdir(mode=0o700)
        extension = directory / "example.py"
        extension.write_text("def register(*_args): pass\n", encoding="utf-8")
        extension.chmod(0o600)
        self.assertTrue(
            server._extension_path_is_trusted(
                directory,
                expect_directory=True,
            )
        )
        self.assertTrue(
            server._extension_path_is_trusted(
                extension,
                expect_directory=False,
            )
        )
        extension.chmod(0o660)
        self.assertFalse(
            server._extension_path_is_trusted(
                extension,
                expect_directory=False,
            )
        )

    def test_sessions_uses_materialized_profiles_without_history_scan(self):
        class FakeDb:
            def __init__(self):
                self.queries = []

            def execute(self, query, _params=()):
                self.queries.append(query)
                if "FROM session_stats" in query:
                    return self
                raise AssertionError(f"unexpected database scan: {query}")

            def fetchall(self):
                return [
                    {
                        "session_id": "qq:group:123",
                        "message_type": "GroupMessage",
                        "last_time": 1,
                        "last_msg": "hello",
                        "sender_name": "user",
                        "session_name": "group",
                        "count": 1,
                        "avatar_url": "",
                        "platform_name": "qq",
                        "guild_avatar_url": "",
                        "_from_stats": 1,
                    }
                ]

            def close(self):
                return None

        fake_db = FakeDb()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/sessions",
                "headers": [],
                "app": server.app,
            }
        )
        with (
            mock.patch.object(server, "get_db_connection", return_value=fake_db),
            mock.patch.object(server, "_table_has_columns", return_value=True),
            mock.patch.object(
                server,
                "_fetch_latest_session_profiles",
                side_effect=AssertionError("chat_history profile scan must not run"),
            ),
            mock.patch.object(
                server,
                "_fetch_materialized_sender_profiles",
                return_value={},
            ),
        ):
            response = server.get_sessions(request)
        payload = json.loads(response.body)
        self.assertTrue(payload["success"])
        self.assertFalse(any("chat_history" in query for query in fake_db.queries))

    def test_filtered_member_counts_use_materialized_profiles(self):
        class FakeResult:
            @staticmethod
            def fetchall():
                return [{"user_id": "u1", "cnt": 7}]

        class FakeDb:
            @staticmethod
            def execute(_query, _params=()):
                return FakeResult()

        profiles = {
            "u1": {
                "sender_name": "Current Name",
                "avatar_url": "/static/cache/avatar.jpg",
                "platform_name": "qq",
            }
        }
        with (
            mock.patch.object(
                server,
                "_fetch_latest_sender_profiles",
                side_effect=AssertionError("history profile scan must not run"),
            ),
            mock.patch.object(
                server,
                "_fetch_materialized_sender_profiles",
                return_value=profiles,
            ) as materialized,
        ):
            rows = server._fetch_user_counts(
                FakeDb(),
                ["timestamp <= ?"],
                [123],
                30,
                profile_session_id="group:1",
            )

        materialized.assert_called_once_with(
            mock.ANY,
            ["u1"],
            session_id="group:1",
        )
        self.assertEqual(rows[0]["sender_name"], "Current Name")

    def test_history_has_more_uses_an_extra_row(self):
        class FakeResult:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        class FakeDb:
            def __init__(self, row_count):
                self.row_count = row_count
                self.params = None

            def execute(self, _query, params=()):
                self.params = list(params)
                rows = [
                    {
                        "id": 1000 - index,
                        "user_id": "1",
                        "sender_name": "user",
                        "message": "hello",
                        "message_length": 5,
                        "message_truncated": 0,
                        "timestamp": 1,
                        "session_id": "group:1",
                        "message_type": "GroupMessage",
                        "session_name": "group",
                        "msg_id": str(index),
                        "is_recalled": 0,
                        "avatar_url": "",
                        "platform_id": "",
                        "platform_name": "qq",
                    }
                    for index in range(self.row_count)
                ]
                return FakeResult(rows)

            def close(self):
                return None

        for row_count, expected_has_more in ((50, False), (51, True)):
            with self.subTest(row_count=row_count):
                fake_db = FakeDb(row_count)
                with (
                    mock.patch.object(
                        server, "get_db_connection", return_value=fake_db
                    ),
                    mock.patch.object(server, "_table_has_columns", return_value=True),
                    mock.patch.object(
                        server,
                        "_fetch_materialized_sender_profiles",
                        return_value={},
                    ),
                    mock.patch.object(
                        server, "load_right_align_ids", return_value=frozenset()
                    ),
                ):
                    response = server.get_history(
                        keyword="",
                        user_id="",
                        session_id="",
                        record_id=0,
                        time_start=0,
                        time_end=0,
                        page=1,
                        limit=50,
                        cursor=0,
                        include_total=False,
                        full_message=False,
                    )
                payload = json.loads(response.body)
                self.assertEqual(len(payload["data"]), 50)
                self.assertEqual(payload["has_more"], expected_has_more)
                self.assertEqual(fake_db.params[-2:], [51, 0])

    def test_history_record_id_returns_only_the_requested_full_message(self):
        long_message = "全文" * 10_000

        class FakeResult:
            def fetchall(self):
                return [
                    {
                        "id": 42,
                        "user_id": "1",
                        "sender_name": "user",
                        "message": long_message,
                        "message_length": len(long_message),
                        "message_truncated": 0,
                        "timestamp": 1,
                        "session_id": "group:1",
                        "message_type": "GroupMessage",
                        "session_name": "group",
                        "msg_id": "platform-42",
                        "is_recalled": 0,
                        "avatar_url": "",
                        "platform_id": "",
                        "platform_name": "qq",
                    }
                ]

        class FakeDb:
            def __init__(self):
                self.query = ""
                self.params = []

            def execute(self, query, params=()):
                self.query = query
                self.params = list(params)
                return FakeResult()

            def close(self):
                return None

        fake_db = FakeDb()
        with (
            mock.patch.object(server, "get_db_connection", return_value=fake_db),
            mock.patch.object(server, "_table_has_columns", return_value=True),
            mock.patch.object(
                server,
                "_fetch_materialized_sender_profiles",
                return_value={},
            ),
            mock.patch.object(server, "load_right_align_ids", return_value=frozenset()),
        ):
            response = server.get_history(
                keyword="",
                user_id="",
                session_id="",
                record_id=42,
                time_start=0,
                time_end=0,
                page=1,
                limit=1,
                cursor=0,
                include_total=False,
                full_message=True,
            )

        payload = json.loads(response.body)
        self.assertIn("id = ?", fake_db.query)
        self.assertNotIn("archive_text_prefix", fake_db.query)
        self.assertEqual(fake_db.params, [42, 2, 0])
        self.assertFalse(payload["has_more"])
        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(payload["data"][0]["id"], 42)
        self.assertEqual(payload["data"][0]["message"], long_message)
        self.assertEqual(payload["data"][0]["message_length"], len(long_message))
        self.assertEqual(payload["data"][0]["message_truncated"], 0)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def _proxy_with_fake(
        self,
        response: _FakeUpstreamResponse,
        *,
        url: str,
        range_header: str = "",
    ):
        headers = [(b"range", range_header.encode("ascii"))] if range_header else []
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/proxy/image",
                "headers": headers,
            }
        )
        fake_session = _FakeUpstreamSession(response)
        with (
            mock.patch.object(server.aiohttp, "TCPConnector", return_value=object()),
            mock.patch.object(
                server.aiohttp, "ClientSession", return_value=fake_session
            ),
            mock.patch.object(
                server, "ALLOWED_MEDIA_DOMAINS", frozenset({"gchat.qpic.cn"})
            ),
        ):
            result = await server.proxy_image(request, url)
        return result, fake_session

    def test_content_range_requires_bounded_complete_size(self):
        self.assertEqual(
            server._parse_content_range(
                "bytes 0-3/10",
                max_total_bytes=10,
            ),
            (0, 3, 10),
        )
        with self.assertRaises(ValueError):
            server._parse_content_range("", max_total_bytes=10)
        with self.assertRaises(ValueError):
            server._parse_content_range("bytes 0-3/*", max_total_bytes=10)
        with self.assertRaises(OverflowError):
            server._parse_content_range(
                "bytes 0-3/11",
                max_total_bytes=10,
            )

    async def test_resolver_rejects_private_target(self):
        private_result = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]
        with mock.patch.object(socket, "getaddrinfo", return_value=private_result):
            with self.assertRaises(OSError):
                await resolve_public_targets("example.test", 443)

    async def test_resolver_returns_the_validated_numeric_address(self):
        public_result = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]
        with mock.patch.object(socket, "getaddrinfo", return_value=public_result):
            targets = await resolve_public_targets("example.test", 443)
        self.assertEqual(targets[0]["host"], "93.184.216.34")
        self.assertEqual(targets[0]["hostname"], "example.test")

    async def test_proxy_forwards_single_range_and_preserves_206(self):
        class FakeContent:
            async def iter_chunked(self, _size):
                yield b"data"

        class FakeResponse:
            status = 206
            headers = {
                "content-type": "video/mp4",
                "content-length": "4",
                "content-range": "bytes 0-3/10",
                "accept-ranges": "bytes",
            }
            content = FakeContent()

            def release(self):
                return None

            async def wait_for_close(self):
                return None

        class FakeSession:
            def __init__(self):
                self.request_headers = {}

            async def get(self, _url, *, headers, allow_redirects):
                self.request_headers = headers
                self.allow_redirects = allow_redirects
                return FakeResponse()

            async def close(self):
                return None

        fake_session = FakeSession()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/proxy/image",
                "headers": [(b"range", b"bytes=0-3")],
            }
        )
        with (
            mock.patch.object(server.aiohttp, "TCPConnector", return_value=object()),
            mock.patch.object(
                server.aiohttp, "ClientSession", return_value=fake_session
            ),
            mock.patch.object(
                server, "ALLOWED_MEDIA_DOMAINS", frozenset({"gchat.qpic.cn"})
            ),
        ):
            response = await server.proxy_image(
                request,
                "https://gchat.qpic.cn/video.mp4",
            )
        body = b"".join([chunk async for chunk in response.body_iterator])
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-3/10")
        self.assertNotIn("content-length", response.headers)
        self.assertEqual(fake_session.request_headers["Range"], "bytes=0-3")
        self.assertEqual(body, b"data")

    async def test_proxy_rejects_svg_even_from_allowed_domain(self):
        class FakeResponse:
            status = 200
            headers = {"content-type": "image/svg+xml", "content-length": "10"}

            def release(self):
                return None

            async def wait_for_close(self):
                return None

        class FakeSession:
            async def get(self, *_args, **_kwargs):
                return FakeResponse()

            async def close(self):
                return None

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/proxy/image",
                "headers": [],
            }
        )
        with (
            mock.patch.object(server.aiohttp, "TCPConnector", return_value=object()),
            mock.patch.object(
                server.aiohttp, "ClientSession", return_value=FakeSession()
            ),
            mock.patch.object(
                server, "ALLOWED_MEDIA_DOMAINS", frozenset({"gchat.qpic.cn"})
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await server.proxy_image(
                    request,
                    "https://gchat.qpic.cn/active.svg",
                )
        self.assertEqual(raised.exception.status_code, 415)

    async def test_proxy_sniffs_and_replays_generic_png(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"x" * 80
        upstream = _FakeUpstreamResponse(
            status=200,
            headers={
                "content-type": "application/octet-stream",
                "content-length": str(len(payload)),
            },
            data=payload,
        )
        response, _session = await self._proxy_with_fake(
            upstream,
            url="https://gchat.qpic.cn/generic-image",
        )
        body = b"".join([chunk async for chunk in response.body_iterator])
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(body, payload)

    async def test_proxy_rejects_active_or_unknown_generic_payloads(self):
        payloads = (
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            b"<!doctype html><script>alert(1)</script>",
            b"%PDF-1.7\n",
            b"PK\x03\x04archive",
            b"unknown bytes",
        )
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                upstream = _FakeUpstreamResponse(
                    status=200,
                    headers={"content-type": "application/octet-stream"},
                    data=payload,
                )
                with self.assertRaises(HTTPException) as raised:
                    await self._proxy_with_fake(
                        upstream,
                        url=f"https://gchat.qpic.cn/generic-rejected-{index}",
                    )
                self.assertEqual(raised.exception.status_code, 415)

    async def test_generic_nonzero_range_requires_prior_byte_zero_sniff(self):
        server._MEDIA_SNIFF_CACHE.clear()
        url = "https://gchat.qpic.cn/generic-video"
        nonzero = _FakeUpstreamResponse(
            status=206,
            headers={
                "content-type": "application/octet-stream",
                "content-length": "10",
                "content-range": "bytes 10-19/92",
            },
            data=b"x" * 10,
        )
        with self.assertRaises(HTTPException) as raised:
            await self._proxy_with_fake(
                nonzero,
                url=url,
                range_header="bytes=10-19",
            )
        self.assertEqual(raised.exception.status_code, 415)

        payload = b"\x00\x00\x00\x18ftypisom" + b"x" * 80
        byte_zero = _FakeUpstreamResponse(
            status=206,
            headers={
                "content-type": "application/octet-stream",
                "content-length": str(len(payload)),
                "content-range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
            },
            data=payload,
        )
        first_response, _session = await self._proxy_with_fake(
            byte_zero,
            url=url,
            range_header=f"bytes=0-{len(payload) - 1}",
        )
        first_body = b"".join([chunk async for chunk in first_response.body_iterator])
        self.assertEqual(first_response.media_type, "video/mp4")
        self.assertEqual(first_body, payload)

        resumed = _FakeUpstreamResponse(
            status=206,
            headers={
                "content-type": "application/octet-stream",
                "content-length": "10",
                "content-range": f"bytes 10-19/{len(payload)}",
            },
            data=b"y" * 10,
        )
        resumed_response, _session = await self._proxy_with_fake(
            resumed,
            url=url,
            range_header="bytes=10-19",
        )
        resumed_body = b"".join(
            [chunk async for chunk in resumed_response.body_iterator]
        )
        self.assertEqual(resumed_response.media_type, "video/mp4")
        self.assertEqual(resumed_body, b"y" * 10)


class WebLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.servers = []
        self.release_events = []
        self.old_env = {
            key: os.environ.get(key)
            for key in ("ARCHIVE_HOST", "ARCHIVE_PORT", "ARCHIVE_API_KEY")
        }
        for key in self.old_env:
            os.environ.pop(key, None)

        self.old_api_key = server.API_KEY
        self.old_cache_static_dir = server.cache_static_dir
        self.old_dirs = server._CACHE_STATIC_DIRS
        self.old_plugin = getattr(server.app.state, "plugin", None)
        self.old_schema_ready = getattr(server.app.state, "schema_ready", False)
        with server._SESSION_TOKENS_LOCK:
            self.old_tokens = dict(server._SESSION_TOKENS)
            server._SESSION_TOKENS.clear()
        with server._AUTH_FAILURES_LOCK:
            self.old_failures = dict(server._AUTH_FAILURES)
            server._AUTH_FAILURES.clear()

    async def asyncTearDown(self):
        for event in self.release_events:
            event.set()
        for admin_server in reversed(self.servers):
            try:
                await admin_server.stop()
            except Exception:
                pass

        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.API_KEY = self.old_api_key
        server.cache_static_dir = self.old_cache_static_dir
        server._CACHE_STATIC_DIRS = self.old_dirs
        server.app.state.plugin = self.old_plugin
        server.app.state.schema_ready = self.old_schema_ready
        server._static_cache_exists_cached.cache_clear()
        with server._SESSION_TOKENS_LOCK:
            server._SESSION_TOKENS.clear()
            server._SESSION_TOKENS.update(self.old_tokens)
        with server._AUTH_FAILURES_LOCK:
            server._AUTH_FAILURES.clear()
            server._AUTH_FAILURES.update(self.old_failures)
        self.temp_dir.cleanup()

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _new_server(self, port: int):
        admin_server = server.AdminServer(
            plugin_instance=object(),
            host="127.0.0.1",
            port=port,
            api_key="lifecycle-key",
            cache_dir=Path(self.temp_dir.name) / "cache",
        )
        admin_server.schema_ready = True
        self.servers.append(admin_server)
        return admin_server

    async def test_same_instance_can_start_stop_and_start_again(self):
        admin_server = self._new_server(self._free_port())

        await admin_server.start()
        first_thread = admin_server.thread
        self.assertTrue(admin_server.server.started)
        self.assertIsNotNone(first_thread)

        await admin_server.stop()
        self.assertIsNone(admin_server.thread)
        self.assertFalse(first_thread.is_alive())

        await admin_server.start()
        second_thread = admin_server.thread
        self.assertTrue(admin_server.server.started)
        self.assertIsNot(first_thread, second_thread)

        await admin_server.stop()
        self.assertIsNone(admin_server.thread)
        self.assertFalse(second_thread.is_alive())

    async def test_same_instance_recovers_after_port_conflict(self):
        port = self._free_port()
        admin_server = self._new_server(port)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", port))
            holder.listen(1)
            with self.assertRaisesRegex(RuntimeError, "failed to listen"):
                await admin_server.start()
            self.assertFalse(admin_server.thread and admin_server.thread.is_alive())

        await admin_server.start()
        self.assertTrue(admin_server.server.started)
        await admin_server.stop()
        self.assertIsNone(admin_server.thread)

    async def test_startup_timeout_keeps_live_thread_reclaimable(self):
        release = threading.Event()
        self.release_events.append(release)

        class StuckServer:
            def __init__(self, _config):
                self.started = False
                self.should_exit = False
                self.force_exit = False

            async def serve(self):
                await asyncio.to_thread(release.wait)

        with (
            mock.patch.object(server.uvicorn, "Server", StuckServer),
            mock.patch.object(server, "_WEB_STARTUP_TIMEOUT_SECONDS", 0.05),
            mock.patch.object(server, "_WEB_STARTUP_JOIN_SECONDS", 0.02),
            mock.patch.object(server, "_WEB_SHUTDOWN_JOIN_SECONDS", 0.1),
        ):
            admin_server = self._new_server(self._free_port())
            with self.assertRaisesRegex(RuntimeError, "still running"):
                await admin_server.start()

            stuck_thread = admin_server.thread
            self.assertIsNotNone(stuck_thread)
            self.assertTrue(stuck_thread.is_alive())
            self.assertIsInstance(admin_server.server, StuckServer)

            release.set()
            await admin_server.stop()
            self.assertIsNone(admin_server.thread)
            self.assertFalse(stuck_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
