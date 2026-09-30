"""Synthetic cache, authenticated Web and startup regressions; no live messages."""

import asyncio
import base64
import importlib
import logging
import os
import socket
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("archive-tests")
sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
sys.modules.setdefault("astrbot.api", api)


class WebMediaTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.tmp.name)
        cls.env = patch.dict(
            os.environ,
            {
                "ARCHIVE_DATA_DIR": str(cls.data),
                "ARCHIVE_DB_PATH": str(cls.data / "nested" / "synthetic.db"),
                "ARCHIVE_CONFIG_PATH": str(cls.data / "absent-config.json"),
                "ARCHIVE_API_KEY": "synthetic-test-only",
            },
        )
        cls.env.start()
        cls.db = importlib.import_module("db_config")
        cls.db_path_patch = patch.object(
            cls.db, "DB_PATH", str(cls.data / "nested" / "synthetic.db")
        )
        cls.pool_patch = patch.object(cls.db, "_POOL", None)
        cls.db_path_patch.start()
        cls.pool_patch.start()
        cls.server = importlib.import_module("web.server")
        cls.media = importlib.import_module("media_cache")
        cls.db.init_db()
        cls.png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII="
        )
        cls.server._custom_apis_loaded = True

    @classmethod
    def tearDownClass(cls):
        cls.db.get_connection_pool().close_all()
        cls.pool_patch.stop()
        cls.db_path_patch.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    async def test_cache_to_history_and_cookie_protected_static_media(self):
        cache = self.media.ArchiveMediaCache(
            config={}, cache_dir=self.data / "web_cache"
        )
        requests = []

        def fixture(request):
            requests.append(str(request.url))
            return httpx.Response(
                200, content=self.png, headers={"content-type": "image/png"}
            )

        client_type = httpx.AsyncClient
        with (
            patch.object(
                cache, "hostname_resolves_to_public_ips", AsyncMock(return_value=True)
            ),
            patch.object(
                self.media.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: client_type(
                    transport=httpx.MockTransport(fixture), **kwargs
                ),
            ),
        ):
            text = await cache.process_and_cache_media_in_string(
                "[合并转发]\n1. synthetic: [CQ:image,url=https://gchat.qpic.cn/synthetic?a=1&amp;b=2]\n[合并转发结束]"
            )
        self.assertEqual(requests, ["https://gchat.qpic.cn/synthetic?a=1&b=2"])
        self.assertIn("url=/static/cache/", text)
        self.assertIn("width=1,height=1", text)
        with self.db.get_db_connection() as db:
            db.execute(
                "INSERT INTO chat_history (user_id,message,timestamp,session_id,msg_id) VALUES (?,?,?,?,?)",
                (
                    "fixture",
                    text,
                    int(time.time()),
                    "synthetic:media",
                    "synthetic-media",
                ),
            )
            db.commit()
        url = text.split("url=", 1)[1].split(",", 1)[0]
        async with client_type(
            transport=httpx.ASGITransport(app=self.server.app),
            base_url="http://synthetic",
        ) as client:
            self.assertEqual((await client.get(url)).status_code, 401)
            login = await client.post(
                "/api/auth/verify", json={"api_key": "synthetic-test-only"}
            )
            self.assertEqual(login.status_code, 200)
            image = await client.get(url)
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.content, self.png)
            self.assertEqual(image.headers["content-type"], "image/png")
            history = await client.get(
                "/api/history", params={"session_id": "synthetic:media"}
            )
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json()["data"][0]["message"], text)
            await client.post("/api/auth/logout")
            self.assertEqual((await client.get(url)).status_code, 401)

    async def test_static_video_range_and_missing_media(self):
        cache = self.data / "web_cache"
        cache.mkdir(exist_ok=True)
        content = b"synthetic-video-bytes-for-http-range"
        (cache / "synthetic.mp4").write_bytes(content)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app),
            base_url="http://synthetic",
            headers={"X-API-Key": "synthetic-test-only"},
        ) as client:
            video = await client.get(
                "/static/cache/synthetic.mp4", headers={"Range": "bytes=0-7"}
            )
            self.assertEqual(video.status_code, 206)
            self.assertEqual(video.content, content[:8])
            self.assertEqual(
                (await client.get("/static/cache/missing.png")).status_code, 404
            )

    async def test_session_history_counts_and_rank_do_not_mix_platforms(self):
        with self.db.get_db_connection() as db:
            for session, user, amount in (
                ("qq:group:42", "qq-user", 2),
                ("telegram:group:42", "tg-user", 3),
            ):
                for index in range(amount):
                    db.execute(
                        "INSERT INTO chat_history (user_id,sender_name,message,timestamp,session_id,msg_id) VALUES (?,?,?,?,?,?)",
                        (
                            user,
                            user,
                            "synthetic",
                            int(time.time()),
                            session,
                            session + str(index),
                        ),
                    )
            db.commit()
        for session, count, user in (
            ("qq:group:42", 2, "qq-user"),
            ("telegram:group:42", 3, "tg-user"),
        ):
            self.assertEqual(
                self.db.DatabaseManager.get_message_count(session_id=session), count
            )
            self.assertEqual(
                {
                    x["user_id"]
                    for x in self.db.DatabaseManager.get_member_rank(session)
                },
                {user},
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.server.app),
                base_url="http://synthetic",
                headers={"X-API-Key": "synthetic-test-only"},
            ) as client:
                response = await client.get(
                    "/api/history", params={"session_id": session}
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(user, response.text)
                self.assertNotIn(
                    "tg-user" if user == "qq-user" else "qq-user", response.text
                )

    async def test_web_root_renders_after_dependency_upgrade(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app),
            base_url="http://synthetic",
        ) as client:
            response = await client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("main.js", response.text)

    async def test_management_auth_origin_preview_delete_restore(self):
        with self.db.get_db_connection() as db:
            db.execute(
                "INSERT INTO chat_history (session_id,message,timestamp) VALUES ('synthetic:manage','synthetic only',100)"
            )
            db.commit()
        body = {"session_id": "synthetic:manage"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app),
            base_url="http://synthetic",
        ) as client:
            self.assertEqual(
                (await client.post("/api/manage/preview", json=body)).status_code, 401
            )
            await client.post(
                "/api/auth/verify", json={"api_key": "synthetic-test-only"}
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/manage/preview",
                        json=body,
                        headers={"Origin": "https://evil.example"},
                    )
                ).status_code,
                403,
            )
            preview = (await client.post("/api/manage/preview", json=body)).json()
            self.server._DASHBOARD_CACHE["fixture"] = (0, {})
            result = await client.post(
                "/api/manage/delete",
                json={
                    "preview_token": preview["preview_token"],
                    "confirm_session_id": body["session_id"],
                },
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.server._DASHBOARD_CACHE, {})
            self.assertEqual(
                (await client.get("/api/history", params=body)).json()["data"], []
            )
            restored = await client.post(
                "/api/manage/restore",
                json={"operation_id": result.json()["operation_id"], **body},
            )
            self.assertEqual(restored.status_code, 200)
            self.assertEqual(
                len((await client.get("/api/history", params=body)).json()["data"]), 1
            )

    async def test_management_all_routes_require_auth_and_preview_is_cookie_bound(self):
        body = {"session_id": "synthetic:cookie-bound"}
        with self.db.get_db_connection() as db:
            db.execute(
                "INSERT INTO chat_history (session_id,message,timestamp) VALUES (?, 'synthetic only',100)",
                [body["session_id"]],
            )
            db.commit()
        transport = httpx.ASGITransport(app=self.server.app)
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://synthetic"
            ) as first,
            httpx.AsyncClient(
                transport=transport, base_url="http://synthetic"
            ) as second,
        ):
            for path in ("preview", "delete", "restore", "export"):
                self.assertEqual(
                    (await first.post("/api/manage/" + path, json={})).status_code, 401
                )
            for path in ("trash", "storage"):
                self.assertEqual(
                    (await first.get("/api/manage/" + path, params=body)).status_code,
                    401,
                )
            await first.post(
                "/api/auth/verify", json={"api_key": "synthetic-test-only"}
            )
            await second.post(
                "/api/auth/verify", json={"api_key": "synthetic-test-only"}
            )
            headers = {"X-API-Key": "wrong-synthetic-key"}
            preview = (
                await first.post("/api/manage/preview", json=body, headers=headers)
            ).json()
            payload = {
                "preview_token": preview["preview_token"],
                "confirm_session_id": body["session_id"],
            }
            self.assertEqual(
                (
                    await second.post(
                        "/api/manage/delete", json=payload, headers=headers
                    )
                ).status_code,
                409,
            )
            exported = await first.post(
                "/api/manage/export",
                json={"preview_token": preview["preview_token"]},
                headers=headers,
            )
            self.assertEqual(exported.status_code, 200)
            self.assertIn("attachment;", exported.headers["content-disposition"])
            self.assertEqual(
                exported.json()["messages"][0]["session_id"], body["session_id"]
            )
            self.assertEqual(
                (
                    await first.post(
                        "/api/manage/delete",
                        json={**payload, "delete_mode": "permanent"},
                    )
                ).status_code,
                400,
            )
            self.assertEqual(
                (
                    await first.post(
                        "/api/manage/delete",
                        json={**payload, "confirm_session_id": "synthetic:other"},
                    )
                ).status_code,
                400,
            )
            await first.post("/api/auth/logout")
            self.assertEqual(
                (await first.post("/api/manage/delete", json=payload)).status_code, 401
            )
            await first.post(
                "/api/auth/verify", json={"api_key": "synthetic-test-only"}
            )
            self.assertEqual(
                (await first.post("/api/manage/delete", json=payload)).status_code, 409
            )

    async def test_real_loopback_start_stop_restart(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        for _ in range(2):
            server = self.server.AdminServer(None, port=port)
            try:
                server.run_in_thread()
                deadline = time.monotonic() + 5
                while (
                    not server.server.started
                    and server.thread.is_alive()
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.02)
                self.assertTrue(server.server.started)
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.get(
                        f"http://127.0.0.1:{port}/api/history",
                        headers={"X-API-Key": "synthetic-test-only"},
                    )
                self.assertEqual(response.status_code, 200)
            finally:
                await server.stop()
            self.assertFalse(server.thread.is_alive())

    async def test_port_conflict_and_startup_failure_are_reported(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            server = self.server.AdminServer(None, port=sock.getsockname()[1])
            with self.assertLogs(api.logger.name, level="ERROR") as captured:
                server.run_in_thread()
                await asyncio.to_thread(server.thread.join, 5)
            self.assertFalse(server.server.started)
            self.assertFalse(server.thread.is_alive())
            self.assertIn("WebUI failed", " ".join(captured.output))
        server = self.server.AdminServer(None, port=0)
        with (
            patch.object(
                self.server,
                "init_db",
                side_effect=PermissionError("synthetic read-only directory"),
            ),
            self.assertLogs(api.logger.name, level="ERROR") as captured,
        ):
            server.run_in_thread()
            await asyncio.to_thread(server.thread.join, 5)
        self.assertFalse(server.server.started)
        self.assertRegex(" ".join(captured.output), "failed|did not start")
