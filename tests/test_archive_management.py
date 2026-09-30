"""Exercise recoverable deletion on production schema and synthetic SQLite only."""

import importlib
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import sys
import tempfile
import types
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("archive-tests")
sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
sys.modules.setdefault("astrbot.api", api)
from archive_management import ArchiveManager, ManagementConflict


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = importlib.import_module("db_config")
        self.path_patch = patch.object(
            self.db, "DB_PATH", str(self.root / "fixture.db")
        )
        self.pool_patch = patch.object(self.db, "_POOL", None)
        self.path_patch.start()
        self.pool_patch.start()
        self.db.init_db()
        self.manager = ArchiveManager(
            self.db.get_db_connection, self.root / "web_cache"
        )
        self.manager.cache_dir.mkdir()
        (self.manager.cache_dir / "shared.png").write_bytes(
            b"synthetic shared attachment"
        )
        with self.db.get_db_connection() as db:
            for sid, text, ts in [
                ("qq:group:42", "旧消息[CQ:image,url=/static/cache/shared.png]", 100),
                ("qq:group:42", "new synthetic", 200),
                ("tg:group:42", "[CQ:image,url=/static/cache/shared.png]", 100),
                (None, "legacy synthetic", 50),
            ]:
                db.execute(
                    "INSERT INTO chat_history (session_id,message,timestamp,user_id,msg_id) VALUES (?,?,?,?,?)",
                    [sid, text, ts, "synthetic", str(ts) + str(sid)],
                )
            db.commit()

    def tearDown(self):
        self.db.get_connection_pool().close_all()
        self.pool_patch.stop()
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_single_message_delete_restore_stats_and_shared_cache(self):
        preview = self.manager.preview(
            "synthetic-principal", "qq:group:42", message_id=1
        )
        self.assertEqual(preview["shared_attachment_count"], 1)
        self.assertEqual(
            preview["message_utf8_bytes"],
            len("旧消息[CQ:image,url=/static/cache/shared.png]".encode()),
        )
        result = self.manager.delete(
            "synthetic-principal", preview["preview_token"], "qq:group:42"
        )
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="qq:group:42"), 1
        )
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="tg:group:42"), 1
        )
        self.assertEqual(
            (self.manager.cache_dir / "shared.png").read_bytes(),
            b"synthetic shared attachment",
        )
        self.assertEqual(self.manager.storage("qq:group:42")["trash"]["count"], 1)
        self.manager.restore(result["operation_id"], "qq:group:42")
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="qq:group:42"), 2
        )
        with self.db.get_db_connection() as db:
            summary = db.execute(
                "SELECT * FROM session_stats WHERE session_id = ?", ["qq:group:42"]
            ).fetchone()
        self.assertEqual((summary["message_count"], summary["last_message_id"]), (2, 2))

    def test_session_delete_keeps_new_messages_and_refreshes_latest(self):
        preview = self.manager.preview("p", "qq:group:42")
        with self.db.get_db_connection() as db:
            db.execute(
                "INSERT INTO chat_history (session_id,message,timestamp) VALUES (?,?,?)",
                ["qq:group:42", "arrived after preview", 300],
            )
            db.commit()
        result = self.manager.delete("p", preview["preview_token"], "qq:group:42")
        self.assertEqual(result["count"], 2)
        with self.db.get_db_connection() as db:
            latest = db.execute(
                "SELECT * FROM session_stats WHERE session_id = ?", ["qq:group:42"]
            ).fetchone()
        self.assertEqual(
            (latest["message_count"], latest["last_msg"]), (1, "arrived after preview")
        )
        self.manager.restore(result["operation_id"], "qq:group:42")
        with self.db.get_db_connection() as db:
            latest = db.execute(
                "SELECT * FROM session_stats WHERE session_id = ?", ["qq:group:42"]
            ).fetchone()
        self.assertEqual(
            (latest["message_count"], latest["last_msg"]), (3, "arrived after preview")
        )

    def test_changed_rows_principal_expiry_and_replay_rejected(self):
        preview = self.manager.preview("p", "qq:group:42")
        with self.assertRaises(ManagementConflict):
            self.manager.delete("other", preview["preview_token"], "qq:group:42")
        with self.db.get_db_connection() as db:
            db.execute("UPDATE chat_history SET message = 'synthetic edit' WHERE id=1")
            db.commit()
        with self.assertRaises(ManagementConflict):
            self.manager.delete("p", preview["preview_token"], "qq:group:42")
        preview = self.manager.preview("p", "qq:group:42")
        with patch("archive_management.time.time", return_value=10**12):
            with self.assertRaises(ManagementConflict):
                self.manager.delete("p", preview["preview_token"], "qq:group:42")
        preview = self.manager.preview("p", "qq:group:42")
        self.manager.delete("p", preview["preview_token"], "qq:group:42")
        with self.assertRaises(ManagementConflict):
            self.manager.delete("p", preview["preview_token"], "qq:group:42")

    def test_failed_delete_is_atomic_and_restore_never_overwrites(self):
        with self.db.get_db_connection() as db:
            db.execute(
                "CREATE TRIGGER fixture_fail BEFORE DELETE ON chat_history WHEN OLD.id=2 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
            )
            db.commit()
        preview = self.manager.preview("p", "qq:group:42")
        with self.assertRaises(Exception):
            self.manager.delete("p", preview["preview_token"], "qq:group:42")
        self.assertEqual(self.manager.storage("qq:group:42")["trash"]["count"], 0)
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="qq:group:42"), 2
        )
        with self.db.get_db_connection() as db:
            db.execute("DROP TRIGGER fixture_fail")
            db.commit()
        preview = self.manager.preview("p", "qq:group:42")
        result = self.manager.delete("p", preview["preview_token"], "qq:group:42")
        with self.db.get_db_connection() as db:
            db.execute(
                "INSERT INTO chat_history (id,session_id,message) VALUES (1,'qq:group:42','keep collision')"
            )
            db.commit()
        with self.assertRaises(ManagementConflict):
            self.manager.restore(result["operation_id"], "qq:group:42")
        self.assertEqual(self.manager.storage("qq:group:42")["trash"]["count"], 2)
        self.assertEqual(
            self.db.DatabaseManager.get_history(session_id="qq:group:42")[0]["message"],
            "keep collision",
        )

    def test_retention_defaults_scope_boundary_and_legacy_restore(self):
        self.assertEqual(
            self.manager.apply_retention(0, ["qq:group:42"], now=86550), []
        )
        self.assertEqual(self.manager.apply_retention(1, [], now=86550), [])
        self.assertEqual(
            self.manager.apply_retention(1000, ["qq:group:42"], now=86550), []
        )
        result = self.manager.apply_retention(1, ["qq:group:42"], now=86550)
        self.assertEqual(result[0]["count"], 1)
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="tg:group:42"), 1
        )
        preview = self.manager.preview("p", "legacy:archive")
        operation = self.manager.delete("p", preview["preview_token"], "legacy:archive")
        self.manager.restore(operation["operation_id"], "legacy:archive")
        with self.db.get_db_connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) AS n FROM chat_history WHERE session_id IS NULL"
                ).fetchone()["n"],
                1,
            )

    def test_legacy_scope_queries_and_large_preview_are_bounded(self):
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="legacy:archive"), 1
        )
        self.assertEqual(
            len(self.db.DatabaseManager.get_history(session_id="legacy:archive")), 1
        )
        self.assertEqual(
            self.db.DatabaseManager.get_user_summary("synthetic", "legacy:archive")[
                "total_messages"
            ],
            1,
        )
        self.assertEqual(
            self.db.DatabaseManager.get_member_rank("legacy:archive")[0]["count"], 1
        )
        with self.db.get_db_connection() as db:
            db.executemany(
                "INSERT INTO chat_history (session_id,message,timestamp) VALUES ('synthetic:large',?,1)",
                [("synthetic",)] * 501,
            )
            db.commit()
        preview = self.manager.preview("p", "synthetic:large")
        self.assertEqual((preview["count"], preview["matched_count"]), (500, 501))
        result = self.manager.delete("p", preview["preview_token"], "synthetic:large")
        self.assertEqual(result["count"], 500)
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="synthetic:large"), 1
        )

    def test_export_permanent_confirmation_and_global_retention_denylist(self):
        preview = self.manager.preview("p", "qq:group:42", message_id=2)
        backup = self.manager.export("p", preview["preview_token"])
        self.assertEqual(backup["messages"][0]["message"], "new synthetic")
        with self.assertRaises(ValueError):
            self.manager.delete(
                "p", preview["preview_token"], "qq:group:42", "permanent"
            )
        result = self.manager.delete(
            "p", preview["preview_token"], "qq:group:42", "permanent", "永久删除"
        )
        self.assertFalse(result["recoverable"])
        self.assertEqual(self.manager.storage("qq:group:42")["trash"]["count"], 0)
        self.manager.apply_retention(
            1, [], now=86550, global_scope=True, excluded_session_ids=["tg:group:42"]
        )
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="tg:group:42"), 1
        )
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="qq:group:42"), 0
        )
        self.assertEqual(
            (self.manager.cache_dir / "shared.png").read_bytes(),
            b"synthetic shared attachment",
        )

    def test_concurrent_confirmations_only_commit_once(self):
        def confirm(token, barrier):
            barrier.wait(timeout=3)
            try:
                return self.manager.delete("p", token, "qq:group:42")
            except ManagementConflict:
                return None

        for shared_token in (True, False):
            tokens = [
                self.manager.preview("p", "qq:group:42")["preview_token"]
                for _ in range(2)
            ]
            if shared_token:
                tokens[1] = tokens[0]
            barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(confirm, token, barrier) for token in tokens]
                results = [future.result(timeout=5) for future in futures]
            successes = [result for result in results if result]
            self.assertEqual(len(successes), 1)
            self.assertEqual(self.manager.storage("qq:group:42")["trash"]["count"], 2)
            self.assertEqual(
                self.db.DatabaseManager.get_message_count(session_id="qq:group:42"), 0
            )
            self.assertEqual(
                self.db.DatabaseManager.get_message_count(session_id="tg:group:42"), 1
            )
            self.manager.restore(successes[0]["operation_id"], "qq:group:42")

    def test_restore_partial_failure_rolls_back_and_trash_references_are_shared(self):
        preview = self.manager.preview("p", "tg:group:42")
        self.manager.delete("p", preview["preview_token"], "tg:group:42")
        preview = self.manager.preview("p", "qq:group:42")
        self.assertEqual(preview["shared_attachment_count"], 1)
        result = self.manager.delete("p", preview["preview_token"], "qq:group:42")
        with self.db.get_db_connection() as db:
            db.execute(
                "INSERT INTO chat_history (id,session_id,message) VALUES (2,'qq:group:42','keep collision')"
            )
            db.commit()
        with self.assertRaises(ManagementConflict):
            self.manager.restore(result["operation_id"], "qq:group:42")
        with self.db.get_db_connection() as db:
            self.assertIsNone(
                db.execute("SELECT id FROM chat_history WHERE id=1").fetchone()
            )
            self.assertEqual(
                db.execute("SELECT message FROM chat_history WHERE id=2").fetchone()[
                    "message"
                ],
                "keep collision",
            )
        self.assertEqual(self.manager.storage("qq:group:42")["trash"]["count"], 2)

    def test_sql_injection_paths_and_quoted_schema_fields(self):
        sid = "synthetic'); DELETE FROM chat_history; --"
        outside = self.root / "outside-synthetic.txt"
        outside.write_text("synthetic only")
        (self.manager.cache_dir / "link.png").symlink_to(outside)
        with self.db.get_db_connection() as db:
            db.execute(
                'ALTER TABLE chat_history ADD COLUMN "synthetic odd column" TEXT'
            )
            db.execute(
                "INSERT INTO chat_history (session_id,message) VALUES (?,?)",
                [
                    sid,
                    "[CQ:image,url=/static/cache/../outside-synthetic.txt][CQ:image,url=/static/cache/%2e%2e%2fx][CQ:image,url=/static/cache/link.png] '); DROP TABLE chat_history; --",
                ],
            )
            db.commit()
        preview = self.manager.preview("p", sid)
        self.assertEqual(preview["cached_attachment_count"], 1)
        self.assertEqual(preview["cached_attachment_bytes"], 0)
        operation = self.manager.delete("p", preview["preview_token"], sid)
        self.manager.restore(operation["operation_id"], sid)
        self.assertEqual(
            self.db.DatabaseManager.get_message_count(session_id="qq:group:42"), 2
        )
        self.assertEqual(self.db.DatabaseManager.get_message_count(session_id=sid), 1)
        self.assertEqual(outside.read_text(), "synthetic only")
        with self.assertRaises(ValueError):
            self.manager.preview("p", "")
        with self.assertRaises(ValueError):
            self.manager.preview("p", "x" * 257)
        self.assertEqual(
            self.manager.apply_retention(
                1, [], now=86550, global_scope=True, should_stop=lambda: True
            ),
            [],
        )
