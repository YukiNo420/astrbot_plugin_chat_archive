from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db_config


class DatabasePerformancePathTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db_config.DB_PATH
        self.old_pool = db_config._POOL
        self.old_fts_ready = db_config._FTS_READY
        db_config.DB_PATH = str(Path(self.temp_dir.name) / "archive.db")
        db_config._POOL = None
        db_config._FTS_READY = False
        db_config.init_db()

    def tearDown(self):
        if db_config._POOL is not None:
            db_config._POOL.close_all()
        db_config._POOL = self.old_pool
        db_config.DB_PATH = self.old_db_path
        db_config._FTS_READY = self.old_fts_ready
        self.temp_dir.cleanup()

    @staticmethod
    def _record(
        user_id: str,
        message: str,
        msg_id: str,
        *,
        recalled: int = 0,
        timestamp: int = 1_700_000_000,
        session_id: str = "session-1",
        session_name: str = "Test Session",
        sender_name: str | None = None,
    ) -> tuple:
        return (
            user_id,
            sender_name if sender_name is not None else f"name-{user_id}",
            message,
            timestamp,
            session_id,
            "GroupMessage",
            session_name,
            msg_id,
            recalled,
            0,
            0,
            "text",
            "platform-1",
            "qq",
            f"/avatar/{user_id}",
            "",
        )

    def _insert(self, *records: tuple) -> None:
        with db_config.get_db_connection() as db:
            db.executemany(
                """
                INSERT INTO chat_history (
                    user_id, sender_name, message, timestamp, session_id,
                    message_type, session_name, msg_id, is_recalled,
                    has_image, has_video, msg_kind, platform_id,
                    platform_name, avatar_url, guild_avatar_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
            db.commit()

    def test_trigram_search_finds_text_after_embedded_nul(self):
        self._insert(self._record("u1", "prefix\x00三字词suffix", "m1"))
        conditions: list[str] = []
        params: list = []
        with db_config.get_db_connection() as db:
            used_fts = db_config.add_message_search_condition(
                db, conditions, params, "三字词"
            )
            rows = db.execute(
                "SELECT msg_id FROM chat_history WHERE " + " AND ".join(conditions),
                params,
            ).fetchall()
            text_meta = db.execute(
                "SELECT archive_text_length(message) as full_length, "
                "archive_text_prefix(message, 8) as prefix "
                "FROM chat_history WHERE msg_id = 'm1'"
            ).fetchone()
        self.assertTrue(used_fts)
        self.assertEqual([row["msg_id"] for row in rows], ["m1"])
        self.assertEqual(text_meta["full_length"], len("prefix\x00三字词suffix"))
        self.assertEqual(text_meta["prefix"], "prefix\x00三")

    def test_short_search_fallback_also_scans_after_nul(self):
        self._insert(self._record("u1", "prefix\x00图片suffix", "m1"))
        conditions: list[str] = []
        params: list = []
        with db_config.get_db_connection() as db:
            used_fts = db_config.add_message_search_condition(
                db, conditions, params, "图片"
            )
            rows = db.execute(
                "SELECT msg_id FROM chat_history WHERE " + " AND ".join(conditions),
                params,
            ).fetchall()
        self.assertFalse(used_fts)
        self.assertEqual([row["msg_id"] for row in rows], ["m1"])

    def test_keyword_with_embedded_nul_bypasses_fts_safely(self):
        keyword = "foo\x00bar"
        self._insert(self._record("u1", f"prefix-{keyword}-suffix", "m1"))

        rows = db_config.DatabaseManager.get_history(keyword=keyword)

        self.assertEqual([row["msg_id"] for row in rows], ["m1"])

    def test_user_stats_track_insert_and_recall(self):
        self._insert(
            self._record("u1", "one", "m1", sender_name="Stable User"),
            self._record("u1", "two", "m2", sender_name=""),
            self._record("u2", "three", "m3", recalled=1),
        )
        self.assertEqual(
            db_config.DatabaseManager.get_message_count(
                session_id="session-1", exclude_recalled=True
            ),
            2,
        )
        rank = db_config.DatabaseManager.get_member_rank("session-1")
        self.assertEqual([(row["user_id"], row["count"]) for row in rank], [("u1", 2)])
        self.assertEqual(rank[0]["avatar_url"], "/avatar/u1")
        self.assertEqual(rank[0]["platform_name"], "qq")
        window_rank = db_config.DatabaseManager.get_member_rank(
            "session-1", since_ts=1_699_999_999
        )
        self.assertEqual(window_rank[0]["avatar_url"], "/avatar/u1")
        self.assertEqual(window_rank[0]["platform_name"], "qq")
        before_rebuild = db_config.DatabaseManager.get_user_summary("u1")
        with db_config.get_db_connection() as db:
            db_config.rebuild_user_stats(db)
            db.commit()
        after_rebuild = db_config.DatabaseManager.get_user_summary("u1")
        self.assertEqual(before_rebuild["last_nickname"], "Stable User")
        self.assertEqual(before_rebuild, after_rebuild)

        with db_config.get_db_connection() as db:
            db.execute("UPDATE chat_history SET is_recalled = 1 WHERE msg_id = 'm1'")
            db.commit()
        self.assertEqual(
            db_config.DatabaseManager.get_message_count(
                session_id="session-1", exclude_recalled=True
            ),
            1,
        )

    def test_schema_upgrade_repairs_equal_count_fts_content_drift(self):
        self._insert(self._record("u1", "original phrase", "m1"))
        with db_config.get_db_connection() as db:
            row = db.execute(
                "SELECT id, message FROM chat_history WHERE msg_id = 'm1'"
            ).fetchone()
            db.execute(
                "INSERT INTO chat_history_fts(chat_history_fts, rowid, message) "
                "VALUES('delete', ?, ?)",
                [row["id"], row["message"]],
            )
            db.execute(
                "INSERT INTO chat_history_fts(rowid, message) VALUES(?, ?)",
                [row["id"], "different phrase"],
            )
            db.execute("PRAGMA user_version = 11")
            db.commit()

        db_config.init_db()
        with db_config.get_db_connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()["user_version"]
            found = db.execute(
                "SELECT rowid FROM chat_history_fts WHERE chat_history_fts MATCH ?",
                ['"original phrase"'],
            ).fetchall()
        self.assertEqual(version, db_config.SCHEMA_VERSION)
        self.assertEqual(len(found), 1)

    def test_current_schema_restart_skips_expensive_fts_content_check(self):
        with mock.patch.object(
            db_config,
            "ensure_fts_search",
            wraps=db_config.ensure_fts_search,
        ) as ensure_fts:
            db_config.init_db()

        self.assertEqual(ensure_fts.call_count, 1)
        self.assertFalse(ensure_fts.call_args.kwargs["verify_external_content"])

    def test_stale_trigger_is_replaced_and_latest_uses_timestamp_then_id(self):
        self._insert(
            self._record(
                "u1",
                "chronologically-new",
                "new",
                timestamp=200,
                session_name="Stable Name",
            ),
            self._record(
                "u1",
                "inserted-later-but-old",
                "old",
                timestamp=100,
                session_name="",
            ),
            self._record(
                "u1",
                "same-time-higher-id",
                "same-time",
                timestamp=200,
                session_name="",
            ),
        )
        with db_config.get_db_connection() as db:
            before = db.execute(
                "SELECT last_msg, last_time, session_name, message_count "
                "FROM session_stats WHERE session_id = 'session-1'"
            ).fetchone()
            db_config.rebuild_session_stats(db)
            after_rebuild = db.execute(
                "SELECT last_msg, last_time, session_name, message_count "
                "FROM session_stats WHERE session_id = 'session-1'"
            ).fetchone()
            db.execute("DROP TRIGGER trg_chat_history_session_stats_insert")
            db.execute("""
                CREATE TRIGGER trg_chat_history_session_stats_insert
                AFTER INSERT ON chat_history
                BEGIN
                    UPDATE session_stats SET last_msg = 'stale-trigger';
                END;
            """)
            db.execute("DROP TRIGGER trg_chat_history_fts_insert")
            db.execute("""
                CREATE TRIGGER trg_chat_history_fts_insert
                AFTER INSERT ON chat_history
                BEGIN
                    SELECT 'stale-fts-trigger';
                END;
            """)
            db.commit()

        self.assertEqual(before, after_rebuild)
        self.assertEqual(before["last_msg"], "same-time-higher-id")
        self.assertEqual(before["last_time"], 200)
        self.assertEqual(before["session_name"], "Stable Name")
        self.assertEqual(before["message_count"], 3)

        db_config.init_db()
        with db_config.get_db_connection() as db:
            trigger_sql = db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND name = 'trg_chat_history_session_stats_insert'"
            ).fetchone()["sql"]
            fts_trigger_sql = db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND name = 'trg_chat_history_fts_insert'"
            ).fetchone()["sql"]
        self.assertNotIn("stale-trigger", trigger_sql)
        self.assertIn("excluded.last_time", trigger_sql)
        self.assertNotIn("stale-fts-trigger", fts_trigger_sql)
        self.assertIn("chat_history_fts", fts_trigger_sql)

    def test_telegram_migration_failure_rolls_back_and_propagates(self):
        legacy_session = "telegram:GroupMessage:-1001"
        record = list(
            self._record(
                "-1001",
                "legacy channel message",
                "tg-1",
                session_id=legacy_session,
            )
        )
        record[5] = "GroupMessage"
        record[13] = "telegram"
        self._insert(tuple(record))

        with mock.patch.object(
            db_config,
            "rebuild_session_stats",
            side_effect=RuntimeError("forced summary failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced summary failure"):
                db_config.init_db()

        with db_config.get_db_connection() as db:
            row = db.execute(
                "SELECT message_type, session_id FROM chat_history "
                "WHERE msg_id = 'tg-1'"
            ).fetchone()
        self.assertEqual(row["message_type"], "GroupMessage")
        self.assertEqual(row["session_id"], legacy_session)

    def test_sqlite_database_exposes_update_rowcount_for_recall(self):
        self._insert(self._record("u1", "message", "m1"))

        with db_config.get_db_connection() as db:
            cursor = db.execute(
                "UPDATE chat_history SET is_recalled = 1 "
                "WHERE msg_id = ? AND session_id = ?",
                ("m1", "session-1"),
            )
            self.assertEqual(cursor.rowcount, 1)
            db.commit()

        with db_config.get_db_connection() as db:
            recalled = db.execute(
                "SELECT is_recalled FROM chat_history WHERE msg_id = 'm1'"
            ).fetchone()["is_recalled"]
        self.assertEqual(recalled, 1)

    def test_commit_retries_busy_without_reexecuting_insert(self):
        raw_connection = sqlite3.connect(":memory:")
        raw_connection.execute("CREATE TABLE commit_probe(value INTEGER)")

        class BusyCommitProxy:
            def __init__(self, connection):
                self.connection = connection
                self.commit_calls = 0

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                self.commit_calls += 1
                if self.commit_calls <= 2:
                    raise sqlite3.OperationalError("database is busy")
                self.connection.commit()

        proxy = BusyCommitProxy(raw_connection)
        db = object.__new__(db_config.SQLiteDatabase)
        db.db_path = ":memory:"
        db._conn = proxy
        db._cursor = None
        db._pool = None
        try:
            db.execute("INSERT INTO commit_probe(value) VALUES (?)", (1,))
            with mock.patch.object(db_config.time, "sleep") as sleep:
                db.commit()

            self.assertEqual(proxy.commit_calls, 3)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                [0.5, 1.0],
            )
            count = raw_connection.execute(
                "SELECT COUNT(*) FROM commit_probe"
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            db.close()
            raw_connection.close()

    def test_executemany_retry_rolls_back_partial_attempt(self):
        with db_config.get_db_connection() as db:
            db.execute("CREATE TABLE retry_probe(value INTEGER)")
            raw_connection = db._get_connection()
            state = {"failed": False}

            def fail_once(value):
                if value == 2 and not state["failed"]:
                    state["failed"] = True
                    raise sqlite3.OperationalError("database is locked")
                return value

            raw_connection.create_function("fail_once", 1, fail_once)
            with (
                mock.patch.object(
                    db_config,
                    "_is_retryable_sqlite_error",
                    return_value=True,
                ),
                mock.patch.object(db_config.time, "sleep"),
            ):
                db.executemany(
                    "INSERT INTO retry_probe(value) VALUES(fail_once(?))",
                    [(1,), (2,), (3,)],
                )
            db.commit()
            values = [
                row["value"]
                for row in db.execute(
                    "SELECT value FROM retry_probe ORDER BY rowid"
                ).fetchall()
            ]
        self.assertEqual(values, [1, 2, 3])

    def test_history_limit_matches_documented_upper_bound(self):
        records = [
            self._record("u1", f"message-{index}", f"m-{index}")
            for index in range(1005)
        ]
        self._insert(*records)

        rows = db_config.DatabaseManager.get_history(limit=10_000)

        self.assertEqual(db_config.DatabaseManager._MAX_QUERY_LIMIT, 1000)
        self.assertEqual(len(rows), 1000)

    def test_schema_v13_drops_redundant_indexes_and_refreshes_planner_stats(self):
        self._insert(
            *(
                self._record(
                    "u1",
                    f"message-{index}",
                    f"m-{index}",
                    timestamp=1_700_000_000 + index,
                )
                for index in range(2000)
            )
        )
        obsolete_indexes = (
            "idx_session",
            "idx_user_timestamp",
            "idx_session_recall_id_desc",
            "idx_session_user_recall_ts",
            "idx_has_image",
            "idx_has_video",
            "idx_has_face",
            "idx_has_voice",
        )
        with db_config.get_db_connection() as db:
            db.execute("CREATE INDEX idx_session ON chat_history(session_id)")
            db.execute(
                "CREATE INDEX idx_user_timestamp ON chat_history(user_id, timestamp)"
            )
            db.execute(
                "CREATE INDEX idx_session_recall_id_desc "
                "ON chat_history(session_id, is_recalled, id DESC)"
            )
            db.execute(
                "CREATE INDEX idx_session_user_recall_ts "
                "ON chat_history(session_id, user_id, is_recalled, timestamp DESC)"
            )
            for index_name, column in (
                ("idx_has_image", "has_image"),
                ("idx_has_video", "has_video"),
                ("idx_has_face", "has_image"),
                ("idx_has_voice", "has_video"),
            ):
                db.execute(f"CREATE INDEX {index_name} ON chat_history({column})")
            db.execute("PRAGMA user_version = 12")
            db.commit()

        db_config.init_db()

        with db_config.get_db_connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()["user_version"]
            remaining = {
                row["name"]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            stat_rows = db.execute(
                "SELECT COUNT(*) as cnt FROM sqlite_stat1 WHERE tbl = 'chat_history'"
            ).fetchone()["cnt"]
            plan = " ".join(
                row["detail"]
                for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM chat_history "
                    "WHERE msg_id = ? AND session_id = ? AND user_id != '0' "
                    "AND COALESCE(is_recalled, 0) = 0",
                    ("m-1500", "session-1"),
                ).fetchall()
            )

        self.assertEqual(version, db_config.SCHEMA_VERSION)
        self.assertTrue(set(obsolete_indexes).isdisjoint(remaining))
        self.assertIn("idx_user", remaining)
        self.assertGreater(stat_rows, 0)
        self.assertIn("idx_msg_id", plan)


if __name__ == "__main__":
    unittest.main()
