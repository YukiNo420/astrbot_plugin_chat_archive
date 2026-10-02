from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("astrbot.api.event", reason="Requires an installed AstrBot runtime")

import config
import db_config
import main
from telegram_channel_capture import TelegramChannelCapture


class ConfigMigrationTests(unittest.TestCase):
    def test_default_media_allowlist_covers_rendered_discord_assets(self):
        self.assertIn("cdn.discordapp.com", config.DEFAULT_ALLOWED_MEDIA_DOMAINS)

    def test_explicit_archive_data_dir_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"ARCHIVE_DATA_DIR": temporary}):
                self.assertEqual(config.get_data_dir(), Path(temporary).resolve())

    def test_known_legacy_state_moves_without_touching_unknown_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy"
            destination = root / "plugin_data"
            (legacy / "web_cache").mkdir(parents=True)
            (legacy / "web_cache" / "image.jpg").write_bytes(b"image")
            (legacy / "chat_history.db").write_bytes(b"database")
            (legacy / "chat_history.db-wal").write_bytes(b"wal")
            (legacy / "chat_history.db-shm").write_bytes(b"shm")
            (legacy / "chat_archive_failed_writes.jsonl").write_text(
                "{}\n",
                encoding="utf-8",
            )
            (legacy / "unknown.keep").write_text("keep", encoding="utf-8")
            (legacy / "web_cache").chmod(0o755)
            for private_file in (
                legacy / "web_cache" / "image.jpg",
                legacy / "chat_history.db",
                legacy / "chat_history.db-wal",
                legacy / "chat_history.db-shm",
                legacy / "chat_archive_failed_writes.jsonl",
            ):
                private_file.chmod(0o644)

            result = config.migrate_legacy_storage(destination, legacy)

            self.assertEqual(result, {"cache_files": 1, "state_files": 4})
            self.assertEqual(
                (destination / "web_cache" / "image.jpg").read_bytes(),
                b"image",
            )
            self.assertEqual(
                (destination / "chat_history.db").read_bytes(),
                b"database",
            )
            self.assertEqual(
                (legacy / "unknown.keep").read_text(encoding="utf-8"),
                "keep",
            )
            self.assertEqual(
                (destination / "web_cache").stat().st_mode & 0o777,
                0o700,
            )
            for private_file in (
                destination / "web_cache" / "image.jpg",
                destination / "chat_history.db",
                destination / "chat_history.db-wal",
                destination / "chat_history.db-shm",
                destination / "chat_archive_failed_writes.jsonl",
            ):
                self.assertEqual(private_file.stat().st_mode & 0o777, 0o600)

    def test_legacy_data_symlink_is_never_migrated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            external = root / "external-storage"
            destination = root / "plugin-data"
            (external / "web_cache").mkdir(parents=True)
            external_file = external / "web_cache" / "history.jpg"
            external_file.write_bytes(b"external")
            legacy_link = root / "legacy-data"
            legacy_link.symlink_to(external, target_is_directory=True)

            result = config.migrate_legacy_storage(
                destination,
                legacy_link,
            )

            self.assertEqual(result, {"cache_files": 0, "state_files": 0})
            self.assertTrue(legacy_link.is_symlink())
            self.assertEqual(external_file.read_bytes(), b"external")
            self.assertFalse(destination.exists())

    def test_existing_destination_database_stops_legacy_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy"
            destination = root / "plugin-data"
            legacy.mkdir()
            destination.mkdir()
            legacy_db = legacy / "chat_history.db"
            destination_db = destination / "chat_history.db"
            legacy_db.write_bytes(b"legacy-history")
            destination_db.write_bytes(b"destination-history")

            with self.assertRaisesRegex(RuntimeError, "migration conflict"):
                config.migrate_legacy_storage(destination, legacy)

            self.assertEqual(legacy_db.read_bytes(), b"legacy-history")
            self.assertEqual(destination_db.read_bytes(), b"destination-history")

    def test_custom_database_path_leaves_legacy_default_database_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy"
            destination = root / "plugin-data"
            legacy.mkdir()
            legacy_db = legacy / "chat_history.db"
            legacy_db.write_bytes(b"legacy-history")

            result = config.migrate_legacy_storage(
                destination,
                legacy,
                database_path=root / "custom" / "archive.db",
            )

            self.assertEqual(result, {"cache_files": 0, "state_files": 0})
            self.assertEqual(legacy_db.read_bytes(), b"legacy-history")
            self.assertFalse((destination / "chat_history.db").exists())


class InitializationMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialization_migrates_before_opening_database_and_stops_on_conflict(
        self,
    ):
        for conflict in (False, True):
            with (
                self.subTest(conflict=conflict),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                legacy = root / "legacy"
                destination = root / "plugin-data"
                legacy.mkdir()
                legacy_db = legacy / "chat_history.db"
                target_db = destination / "chat_history.db"
                with sqlite3.connect(legacy_db) as db:
                    db.execute("CREATE TABLE preserved(message TEXT)")
                    db.execute("INSERT INTO preserved VALUES ('old history')")
                before = legacy_db.read_bytes()
                if conflict:
                    destination.mkdir()
                    target_db.write_bytes(b"existing destination")

                plugin = object.__new__(main.ChatArchivePlugin)
                plugin._lifecycle_lock = asyncio.Lock()
                plugin._media_accept_lock = asyncio.Lock()
                plugin._initialized = False
                plugin._media_generation = 0
                plugin._writer = SimpleNamespace(start=AsyncMock())
                plugin._telegram_channel_capture = SimpleNamespace(
                    ensure_registered=AsyncMock()
                )
                plugin.web_server = None
                plugin._media_cache_worker = AsyncMock()
                plugin._periodic_clean_loop = AsyncMock()
                plugin._pending_recall_reconcile_loop = AsyncMock()
                plugin._start_telegram_channel_capture_task = Mock()
                plugin._rollback_failed_initialize = AsyncMock()

                def open_migrated_database():
                    self.assertTrue(target_db.is_file())
                    with sqlite3.connect(target_db) as db:
                        self.assertEqual(
                            db.execute("SELECT message FROM preserved").fetchone()[0],
                            "old history",
                        )

                with (
                    patch.object(config, "get_legacy_data_dir", return_value=legacy),
                    patch.object(main, "DATA_DIR", destination),
                    patch.object(main, "DB_PATH", str(target_db)),
                    patch.object(
                        main, "init_db", side_effect=open_migrated_database
                    ) as init_db,
                ):
                    if conflict:
                        with self.assertRaisesRegex(RuntimeError, "migration conflict"):
                            await plugin.initialize()
                        init_db.assert_not_called()
                        plugin._writer.start.assert_not_awaited()
                        plugin._rollback_failed_initialize.assert_awaited_once()
                        self.assertEqual(legacy_db.read_bytes(), before)
                        self.assertEqual(
                            target_db.read_bytes(), b"existing destination"
                        )
                    else:
                        await plugin.initialize()
                        await asyncio.gather(
                            plugin._media_worker_task,
                            plugin._clean_task,
                            plugin._recall_reconcile_task,
                        )
                        init_db.assert_called_once_with()
                        plugin._writer.start.assert_awaited_once()
                        self.assertTrue(plugin._initialized)
                        self.assertFalse(legacy_db.exists())


class _FakeWriter:
    def __init__(self):
        self.records: list[tuple] = []
        self.failed: list[tuple[list, str]] = []

    async def start(self):
        return None

    async def enqueue(self, record):
        self.records.append(record)

    def write_failed_batch(self, batch, reason):
        self.failed.append((batch, reason))


class RuntimeHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_records_share_one_ordered_media_queue(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin._shutting_down = False
        plugin._initialized = True
        plugin._media_generation = 1
        plugin._media_accept_lock = asyncio.Lock()
        plugin._media_queue = asyncio.Queue(maxsize=10)
        plugin._writer = _FakeWriter()
        plugin._telegram_channel_capture = SimpleNamespace(
            enrich_record_media=AsyncMock()
        )

        async def cache_media(text):
            await asyncio.sleep(0.02)
            return f"cached:{text}"

        plugin._process_and_cache_media_in_string = cache_media
        worker = asyncio.create_task(plugin._media_cache_worker())
        try:
            await plugin._enqueue_record_dict(
                {"msg_id": "first", "message": "[CQ:image]"},
                cache_media=True,
            )
            await plugin._enqueue_record_dict(
                {"msg_id": "second", "message": "plain"},
                cache_media=False,
            )
            await asyncio.wait_for(plugin._media_queue.join(), timeout=1)
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        self.assertEqual(
            [record[7] for record in plugin._writer.records],
            ["first", "second"],
        )

    async def test_full_media_ingress_queue_rejects_immediately(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin._shutting_down = False
        plugin._initialized = True
        plugin._media_generation = 1
        plugin._media_accept_lock = asyncio.Lock()
        plugin._media_queue = asyncio.Queue(maxsize=1)
        plugin._media_queue.put_nowait(({"msg_id": "existing"}, False))

        accepted = await asyncio.wait_for(
            plugin._enqueue_record_dict(
                {"msg_id": "late"},
                cache_media=False,
            ),
            timeout=0.1,
        )

        existing = plugin._media_queue.get_nowait()
        plugin._media_queue.task_done()
        self.assertFalse(accepted)
        self.assertEqual(existing[0]["msg_id"], "existing")
        self.assertTrue(plugin._media_queue.empty())

    async def test_worker_cancellation_does_not_duplicate_accepted_enqueue(self):
        accepted = asyncio.Event()
        release = asyncio.Event()

        class RaceWriter(_FakeWriter):
            async def enqueue(self, record):
                self.records.append(record)
                accepted.set()
                await release.wait()
                return True

        plugin = object.__new__(main.ChatArchivePlugin)
        plugin._media_queue = asyncio.Queue()
        plugin._writer = RaceWriter()
        plugin._telegram_channel_capture = SimpleNamespace(
            enrich_record_media=AsyncMock()
        )
        plugin._process_and_cache_media_in_string = AsyncMock()
        await plugin._media_queue.put(({"msg_id": "one", "message": "plain"}, False))

        worker = asyncio.create_task(plugin._media_cache_worker())
        await accepted.wait()
        worker.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await worker

        self.assertEqual(
            [record[7] for record in plugin._writer.records],
            ["one"],
        )

    async def test_bad_record_does_not_kill_ordered_worker(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin._media_queue = asyncio.Queue()
        plugin._writer = _FakeWriter()
        plugin._telegram_channel_capture = SimpleNamespace(
            enrich_record_media=AsyncMock()
        )
        plugin._process_and_cache_media_in_string = AsyncMock()
        await plugin._media_queue.put(({"msg_id": "bad", "timestamp": object()}, False))
        await plugin._media_queue.put(({"msg_id": "good", "timestamp": 1}, False))

        worker = asyncio.create_task(plugin._media_cache_worker())
        try:
            await asyncio.wait_for(plugin._media_queue.join(), timeout=1)
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        self.assertEqual(
            [record[7] for record in plugin._writer.records],
            ["good"],
        )
        self.assertEqual(len(plugin._writer.failed), 1)

    async def test_media_timeout_falls_back_to_original_record(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin._media_queue = asyncio.Queue()
        plugin._writer = _FakeWriter()
        plugin._MEDIA_PROCESS_TIMEOUT = 0.01
        plugin._telegram_channel_capture = SimpleNamespace(
            enrich_record_media=AsyncMock()
        )

        async def never_finishes(_text):
            await asyncio.Future()

        plugin._process_and_cache_media_in_string = never_finishes
        await plugin._media_queue.put(
            ({"msg_id": "one", "message": "[CQ:image]"}, True)
        )
        worker = asyncio.create_task(plugin._media_cache_worker())
        try:
            await asyncio.wait_for(plugin._media_queue.join(), timeout=1)
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        self.assertEqual(plugin._writer.records[0][2], "[CQ:image]")

    async def test_periodic_maintenance_enforces_quota_without_downloads(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin.conf = {"basic": {"enable_clean": False}}
        plugin._media_cache = SimpleNamespace(
            ensure_cache_capacity=AsyncMock(return_value=True)
        )

        await plugin._maintain_media_cache_once()

        plugin._media_cache.ensure_cache_capacity.assert_awaited_once_with(
            0,
            force_rescan=True,
        )

    async def test_pending_recall_survives_until_delayed_insert(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "archive.db"
            old_db_path = db_config.DB_PATH
            old_pool = db_config._POOL
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "CREATE TABLE chat_history ("
                    "msg_id TEXT, session_id TEXT, user_id TEXT, "
                    "is_recalled INTEGER DEFAULT 0)"
                )

            plugin = object.__new__(main.ChatArchivePlugin)
            plugin._pending_recalls = {}
            plugin._pending_recall_lock = asyncio.Lock()
            plugin._pending_recall_event = asyncio.Event()

            db_config.DB_PATH = str(db_path)
            db_config._POOL = None
            try:
                await plugin._remember_pending_recall("session", "message")
                self.assertEqual(
                    await plugin._reconcile_pending_recalls_once(),
                    0,
                )
                with sqlite3.connect(db_path) as connection:
                    connection.execute(
                        "INSERT INTO chat_history "
                        "(msg_id, session_id, user_id) VALUES (?, ?, ?)",
                        ("message", "session", "user"),
                    )
                    connection.commit()
                self.assertEqual(
                    await plugin._reconcile_pending_recalls_once(),
                    1,
                )
            finally:
                if db_config._POOL is not None:
                    db_config._POOL.close_all()
                db_config._POOL = old_pool
                db_config.DB_PATH = old_db_path

            with sqlite3.connect(db_path) as connection:
                recalled = connection.execute(
                    "SELECT is_recalled FROM chat_history"
                ).fetchone()[0]
            self.assertEqual(recalled, 1)
            self.assertFalse(plugin._pending_recalls)

    def test_group_admin_only_defaults_to_enabled(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin.conf = {}

        self.assertTrue(plugin._group_admin_only_enabled())

    async def test_private_chat_is_not_blocked_by_group_admin_only(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin.conf = {"basic": {"group_admin_only": True}}
        plugin._initialized = True
        plugin._shutting_down = False
        event = SimpleNamespace(
            unified_msg_origin="qq:FriendMessage:123",
            get_sender_id=lambda: "123",
            get_group_id=lambda: "",
            is_admin=lambda: False,
        )
        context = SimpleNamespace(event=event)

        allowed, scoped = await plugin._prepare_archive_tool_query(
            context,
            {},
        )

        self.assertTrue(allowed)
        self.assertEqual(scoped["session_id"], "qq:FriendMessage:123")

    def test_admin_check_uses_current_event_role_only(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        event = SimpleNamespace(
            unified_msg_origin="qq:GroupMessage:1",
            get_sender_id=lambda: "previous-admin",
            is_admin=lambda: False,
        )
        self.assertFalse(plugin._is_admin_tool_context(SimpleNamespace(event=event)))

    async def test_archive_tool_rejects_queries_until_init_finishes(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin._initialized = False
        plugin._shutting_down = True
        allowed, result = await plugin._prepare_archive_tool_query(
            SimpleNamespace(),
            {},
        )
        self.assertFalse(allowed)
        self.assertFalse(result["scope"]["ready"])

    async def test_after_send_uses_preserved_voice_snapshot(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin._initialized = True
        plugin._shutting_down = False
        plugin._archive_bot_reply = AsyncMock()
        event = SimpleNamespace(
            get_result=lambda: SimpleNamespace(chain=[]),
            get_extra=lambda key, default=None: (
                "[CQ:record,url=/voice.ogg]"
                if key == "_chat_archive_reply_snapshot"
                else default
            ),
        )

        await plugin.handle_bot_reply(event)

        plugin._archive_bot_reply.assert_awaited_once_with(
            event,
            "[CQ:record,url=/voice.ogg]",
        )

    def test_notice_and_request_wrappers_are_not_chat_messages(self):
        self.assertTrue(
            main.ChatArchivePlugin._is_non_message_platform_event(
                {"post_type": "notice", "notice_type": "group_increase"}
            )
        )
        self.assertTrue(
            main.ChatArchivePlugin._is_non_message_platform_event(
                {"request_type": "friend"}
            )
        )
        self.assertFalse(
            main.ChatArchivePlugin._is_non_message_platform_event(
                {"post_type": "message"}
            )
        )

    async def test_forward_expansion_preserves_surrounding_and_all_forwards(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin._fetch_forward_archive_text = AsyncMock(
            side_effect=["expanded-one", "expanded-two"]
        )
        forward_type = type("Forward", (), {})
        first = forward_type()
        first.id = "1"
        first.res_id = ""
        second = forward_type()
        second.id = "2"
        second.res_id = ""
        event = SimpleNamespace(get_messages=lambda: [first, second])
        record = {"message": ("before[合并转发,id=1]middle[合并转发,id=2]after")}

        await plugin._expand_forward_message_if_needed(event, record, {})

        self.assertEqual(
            record["message"],
            "beforeexpanded-onemiddleexpanded-twoafter",
        )

    async def test_forward_expansion_timeout_preserves_original_message(self):
        plugin = object.__new__(main.ChatArchivePlugin)
        plugin._FORWARD_EXPAND_TIMEOUT = 0.01

        async def fetch_slow_forward(*_args):
            await asyncio.sleep(60)

        plugin._fetch_forward_archive_text = AsyncMock(side_effect=fetch_slow_forward)
        forward_type = type("Forward", (), {})
        forward = forward_type()
        forward.id = "slow"
        forward.res_id = ""
        event = SimpleNamespace(get_messages=lambda: [forward])
        record = {"message": "before[合并转发,id=slow]after"}

        await asyncio.wait_for(
            plugin._expand_forward_message_if_needed(event, record, {}),
            timeout=0.2,
        )

        self.assertEqual(record["message"], "before[合并转发,id=slow]after")


class TelegramHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        media_cache = SimpleNamespace(
            cache_dir=Path(self.temporary.name),
            get_max_media_bytes=lambda: 1024 * 1024,
        )
        self.plugin = SimpleNamespace(
            _media_cache=media_cache,
            _shutting_down=False,
            _ignored_users=set(),
            conf={"basic": {"enable_archive": True, "cache_media": False}},
        )
        self.capture = TelegramChannelCapture(self.plugin)

    async def asyncTearDown(self):
        await self.capture.stop()
        self.temporary.cleanup()

    async def test_cache_media_false_never_resolves_telegram_file(self):
        calls = 0

        class Media:
            async def get_file(self):
                nonlocal calls
                calls += 1
                raise AssertionError("file download must stay disabled")

        message = SimpleNamespace(
            text=None,
            caption="caption",
            photo=[Media()],
            video=None,
            animation=None,
            voice=None,
            audio=None,
            document=None,
            sticker=None,
        )

        archived = await self.capture._message_to_archive_text(
            message,
            cache_media=False,
        )

        self.assertEqual(archived, "caption[CQ:image]")
        self.assertEqual(calls, 0)

    async def test_same_cache_key_downloads_once(self):
        calls = 0

        class File:
            file_path = "photo.jpg"
            file_size = 5

            async def download_to_drive(self, custom_path):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.02)
                Path(custom_path).write_bytes(b"photo")

        first, second = await asyncio.gather(
            self.capture._cache_telegram_file(File(), "same-key"),
            self.capture._cache_telegram_file(File(), "same-key"),
        )

        self.assertEqual(first, second)
        self.assertEqual(calls, 1)
        self.assertFalse(self.capture._file_locks)

    async def test_cancelled_download_removes_tmp_file(self):
        started = asyncio.Event()

        class File:
            file_path = "photo.jpg"
            file_size = 5

            async def download_to_drive(self, custom_path):
                Path(custom_path).write_bytes(b"partial")
                started.set()
                await asyncio.Future()

        task = asyncio.create_task(
            self.capture._cache_telegram_file(File(), "cancel-key")
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        digest = hashlib.sha256(b"cancel-key").hexdigest()[:32]
        tmp_path = Path(self.temporary.name) / f"telegram_avatar_{digest}.jpg.tmp"
        self.assertFalse(tmp_path.exists())

    async def test_total_quota_is_checked_before_and_after_download(self):
        capacity_checks = []
        responses = iter((True, False))
        downloads = 0

        async def ensure_capacity(required_bytes):
            capacity_checks.append(required_bytes)
            return next(responses)

        self.plugin._media_cache.ensure_cache_capacity = ensure_capacity

        class File:
            file_path = "photo.jpg"
            file_size = 5

            async def download_to_drive(self, custom_path):
                nonlocal downloads
                downloads += 1
                Path(custom_path).write_bytes(b"actual")

        result = await self.capture._cache_telegram_file(
            File(),
            "quota-key",
        )

        self.assertEqual(result, "")
        self.assertEqual(downloads, 1)
        self.assertEqual(capacity_checks, [5, 0])
        self.assertFalse(any(Path(self.temporary.name).iterdir()))

    async def test_expired_avatar_file_is_refreshed_atomically(self):
        downloads = 0
        cache_key = "avatar-key"
        digest = hashlib.sha256(cache_key.encode()).hexdigest()[:32]
        destination = Path(self.temporary.name) / f"telegram_avatar_{digest}.jpg"
        destination.write_bytes(b"old")
        expired = time.time() - self.capture._AVATAR_HIT_TTL - 1
        os.utime(destination, (expired, expired))

        class File:
            file_path = "photo.jpg"
            file_size = 3

            async def download_to_drive(self, custom_path):
                nonlocal downloads
                downloads += 1
                Path(custom_path).write_bytes(b"new")

        result = await self.capture._cache_telegram_file(File(), cache_key)

        self.assertEqual(
            result,
            f"/static/cache/{destination.name}",
        )
        self.assertEqual(downloads, 1)
        self.assertEqual(destination.read_bytes(), b"new")

    async def test_application_replacement_rebinds_handler_immediately(self):
        class Application:
            def __init__(self):
                self.added = []
                self.removed = []

            def add_handler(self, handler, group):
                self.added.append((handler, group))

            def remove_handler(self, handler, group):
                self.removed.append((handler, group))

        class Adapter:
            config = {}

            def __init__(self, application):
                self.application = application

            @staticmethod
            def meta():
                return SimpleNamespace(id="tg", name="telegram")

        first = Application()
        second = Application()
        adapter = Adapter(first)
        manager = SimpleNamespace(get_insts=lambda: [adapter])
        self.plugin.context = SimpleNamespace(platform_manager=manager)

        self.assertEqual(await self.capture.ensure_registered(), 1)
        adapter.application = second
        self.assertEqual(await self.capture.ensure_registered(), 1)

        self.assertEqual(len(first.removed), 1)
        self.assertEqual(len(second.added), 1)

    def test_avatar_cache_has_ttl_and_bound(self):
        self.capture._AVATAR_CACHE_MAX = 2
        self.capture._remember_avatar("one", "/one")
        self.capture._remember_avatar("two", "/two")
        self.capture._remember_avatar("three", "/three")
        self.assertEqual(list(self.capture._avatar_cache), ["two", "three"])

        self.capture._avatar_cache["three"] = (
            "/three",
            time.time() - 1,
        )
        self.assertIsNone(self.capture._get_cached_avatar("three"))


if __name__ == "__main__":
    unittest.main()
