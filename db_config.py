from __future__ import annotations

import datetime
import queue
import re
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path

from astrbot.api import logger

try:
    from .config import (
        load_db_path,
        load_sqlite_journal_mode,
        load_sqlite_pool_size,
    )
except ImportError:
    from config import (
        load_db_path,
        load_sqlite_journal_mode,
        load_sqlite_pool_size,
    )


SCHEMA_VERSION = 13
# Version 12 is the explicit marker for the one-time external-content FTS
# verification. Future unrelated schema bumps must not repeat that full scan.
FTS_EXTERNAL_CONTENT_CHECK_VERSION = 12


def _sqlite_text_length(value) -> int:
    return len(str(value or ""))


def _sqlite_text_prefix(value, max_chars) -> str:
    try:
        limit = max(0, int(max_chars))
    except (TypeError, ValueError):
        limit = 0
    return str(value or "")[:limit]


def _is_retryable_sqlite_error(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _session_predicate(session_id):
    return (
        "COALESCE(NULLIF(session_id, ''), 'legacy:archive') = ?"
        if session_id == "legacy:archive"
        else "session_id = ?"
    )


DEFAULT_DB_PATH = str(Path(load_db_path()).parent / "chat_history.db")

# logger is imported from astrbot.api to meet framework standards


class Database:
    def execute(self, sql, params=None):
        raise NotImplementedError

    def execute_quiet(self, sql, params=None):
        return self.execute(sql, params)

    def executemany(self, sql, seq_of_params):
        raise NotImplementedError

    def fetchone(self):
        raise NotImplementedError

    def fetchall(self):
        raise NotImplementedError

    @property
    def rowcount(self):
        raise NotImplementedError

    def commit(self):
        raise NotImplementedError

    def rollback(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class SQLiteConnectionPool:
    def __init__(self, db_path, max_connections=10):
        self.db_path = db_path
        self.max_connections = max_connections
        self.pool = queue.Queue(max_connections)
        self._lock = threading.Lock()
        self._allocated = 0

    def _dict_factory(self, cursor, row):
        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

    def _create_connection(self):
        # Allow cross-thread connection usage under safe queue-based reuse
        conn = sqlite3.connect(self.db_path, timeout=20, check_same_thread=False)
        conn.row_factory = self._dict_factory
        conn.create_function(
            "archive_text_length", 1, _sqlite_text_length, deterministic=True
        )
        conn.create_function(
            "archive_text_prefix", 2, _sqlite_text_prefix, deterministic=True
        )
        conn.create_function(
            "archive_search_fold",
            1,
            lambda value: str(value or "").casefold(),
            deterministic=True,
        )
        journal_mode = load_sqlite_journal_mode()
        conn.execute(f"PRAGMA journal_mode={journal_mode};")
        conn.execute("PRAGMA synchronous=NORMAL;")
        if journal_mode == "WAL":
            # Keep the reusable WAL high-water mark bounded after large one-off
            # migrations such as the initial FTS rebuild. SQLite still reuses
            # the file between checkpoints instead of truncating each commit.
            conn.execute("PRAGMA wal_autocheckpoint=1000;")
            conn.execute("PRAGMA journal_size_limit=67108864;")
        return conn

    def get_connection(self):
        try:
            return self.pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._allocated < self.max_connections:
                    self._allocated += 1
                    try:
                        return self._create_connection()
                    except Exception as e:
                        self._allocated -= 1
                        raise e
            # Block until a connection is released
            return self.pool.get(timeout=10)

    def release_connection(self, conn):
        if conn:
            try:
                if conn.in_transaction:
                    conn.rollback()
                    logger.warning(
                        "Rolled back pending SQLite transaction before returning connection to pool."
                    )
            except Exception as e:
                logger.error(
                    f"SQLite connection rollback before pool release failed: {e}"
                )
                try:
                    conn.close()
                finally:
                    with self._lock:
                        self._allocated = max(0, self._allocated - 1)
                return
            try:
                self.pool.put_nowait(conn)
            except queue.Full:
                logger.warning(
                    "SQLite connection pool is full; closing returned connection."
                )
                try:
                    conn.close()
                finally:
                    with self._lock:
                        self._allocated = max(0, self._allocated - 1)

    def close_all(self):
        closed = 0
        while not self.pool.empty():
            try:
                conn = self.pool.get_nowait()
                conn.close()
                closed += 1
            except queue.Empty:
                break
        if closed:
            with self._lock:
                self._allocated = max(0, self._allocated - closed)


DB_PATH = load_db_path()

_POOL = None
_POOL_LOCK = threading.Lock()
_INIT_DB_LOCK = threading.Lock()


def get_connection_pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = SQLiteConnectionPool(
                    DB_PATH, max_connections=load_sqlite_pool_size()
                )
    return _POOL


class SQLiteDatabase(Database):
    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = None
        self._cursor = None
        self._pool = get_connection_pool()

    def _get_connection(self):
        if self._conn is None:
            self._conn = self._pool.get_connection()
        return self._conn

    def execute(self, sql, params=None):
        try:
            conn = self._get_connection()
            self._cursor = conn.execute(sql, params or ())
            return self
        except Exception as e:
            logger.error(f"SQL execution failed: {sql} | Error: {e}")
            raise e

    def execute_quiet(self, sql, params=None):
        conn = self._get_connection()
        self._cursor = conn.execute(sql, params or ())
        return self

    def executemany(self, sql, seq_of_params):
        params = list(seq_of_params)
        conn = self._get_connection()
        retries = 3
        delays = [0.5, 1.0, 2.0]
        started_transaction = not conn.in_transaction
        if started_transaction:
            # An outer transaction keeps RELEASE SAVEPOINT from committing.
            # The caller still owns the final commit/rollback decision.
            conn.execute("BEGIN")

        for attempt in range(retries):
            savepoint = "archive_executemany_retry"
            savepoint_active = False
            try:
                conn.execute(f"SAVEPOINT {savepoint}")
                savepoint_active = True
                self._cursor = conn.executemany(sql, params)
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                savepoint_active = False
                return self
            except sqlite3.OperationalError as e:
                if savepoint_active:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                retryable = _is_retryable_sqlite_error(e)
                if retryable and attempt < retries - 1:
                    logger.warning(
                        "SQL executemany operational error: "
                        f"{e}. Retrying in {delays[attempt]}s..."
                    )
                    time.sleep(delays[attempt])
                else:
                    if started_transaction:
                        conn.rollback()
                    logger.error(
                        f"SQL executemany failed after retries: {sql} | Error: {e}"
                    )
                    raise
            except Exception as e:
                if savepoint_active:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if started_transaction:
                    conn.rollback()
                logger.error(f"SQL executemany failed: {sql} | Error: {e}")
                raise

    def fetchone(self):
        if self._cursor:
            return self._cursor.fetchone()
        return None

    def fetchall(self):
        if self._cursor:
            return self._cursor.fetchall()
        return []

    @property
    def rowcount(self) -> int:
        """Expose the current cursor's affected-row count."""
        return self._cursor.rowcount if self._cursor is not None else -1

    def commit(self):
        if not self._conn:
            return

        retries = 3
        delays = [0.5, 1.0, 2.0]
        for attempt in range(retries):
            try:
                self._conn.commit()
                return
            except sqlite3.OperationalError as e:
                if not _is_retryable_sqlite_error(e) or attempt >= retries - 1:
                    raise
                logger.warning(
                    f"SQLite commit busy, retrying in {delays[attempt]}s: {e}"
                )
                time.sleep(delays[attempt])

    def rollback(self):
        if self._conn:
            self._conn.rollback()

    def close(self):
        if self._conn:
            if self._pool:
                self._pool.release_connection(self._conn)
            self._conn = None
            self._cursor = None
            logger.debug("SQLiteDatabase connection closed and released to pool.")

    @property
    def row_factory(self):
        return self._get_connection().row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._get_connection().row_factory = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc_val, _exc_tb):
        if exc_type is not None:
            try:
                self.rollback()
            except Exception as e:
                logger.error(f"SQLiteDatabase rollback failed: {e}")
        self.close()


def get_db_connection() -> Database:
    return SQLiteDatabase(DB_PATH)


class DatabaseManager:
    """Manages all complex query operations for chat history to keep plugin class slim."""

    # DEVELOPER.md documents 1000 as the supported upper bound.
    _MAX_QUERY_LIMIT = 1000
    _MAX_QUERY_OFFSET = 100000

    @staticmethod
    def _clamp_int(value, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    @staticmethod
    def get_history(
        user_id: str = None,
        session_id: str = None,
        keyword: str = None,
        since_ts: int = None,
        until_ts: int = None,
        limit: int = 50,
        offset: int = 0,
        asc: bool = True,
        exclude_recalled: bool = True,
    ) -> list[dict]:
        try:
            limit = DatabaseManager._clamp_int(
                limit, 50, 1, DatabaseManager._MAX_QUERY_LIMIT
            )
            offset = DatabaseManager._clamp_int(
                offset, 0, 0, DatabaseManager._MAX_QUERY_OFFSET
            )
            with get_db_connection() as conn:
                optional_columns = []
                if column_exists(conn, "chat_history", "platform_id"):
                    optional_columns.append("platform_id")
                if column_exists(conn, "chat_history", "platform_name"):
                    optional_columns.append("platform_name")
                if column_exists(conn, "chat_history", "avatar_url"):
                    optional_columns.append("avatar_url")
                if column_exists(conn, "chat_history", "guild_avatar_url"):
                    optional_columns.append("guild_avatar_url")
                optional_select = (
                    ", " + ", ".join(optional_columns) if optional_columns else ""
                )
                query = (
                    "SELECT id, user_id, sender_name, message, timestamp, "
                    "session_id, message_type, session_name, msg_id, is_recalled"
                    f"{optional_select} "
                    "FROM chat_history WHERE 1=1"
                )
                params: list = []

                if exclude_recalled:
                    query += " AND (is_recalled IS NULL OR is_recalled = 0)"
                if user_id:
                    query += " AND user_id = ?"
                    params.append(str(user_id))
                if session_id:
                    query += " AND " + _session_predicate(session_id)
                    params.append(str(session_id))
                if keyword:
                    search_conditions: list[str] = []
                    search_params: list = []
                    add_message_search_condition(
                        conn,
                        search_conditions,
                        search_params,
                        str(keyword),
                    )
                    if search_conditions:
                        query += " AND " + " AND ".join(search_conditions)
                        params.extend(search_params)
                if since_ts is not None:
                    query += " AND timestamp >= ?"
                    params.append(int(since_ts))
                if until_ts is not None:
                    query += " AND timestamp <= ?"
                    params.append(int(until_ts))

                order = "ASC" if asc else "DESC"
                id_order = "ASC" if asc else "DESC"
                query += f" ORDER BY timestamp {order}, id {id_order} LIMIT ? OFFSET ?"
                params.extend([limit, offset])

                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Chat Archive API get_history error: {e}")
            return []

    @staticmethod
    def get_sessions() -> list[dict]:
        try:
            with get_db_connection() as conn:
                try:
                    cursor = conn.execute(
                        "SELECT session_id, message_type, message_count as count, "
                        "last_time FROM session_stats ORDER BY last_time DESC"
                    )
                except Exception:
                    # Forward-compatible fallback for databases that have not run init_db yet.
                    cursor = conn.execute(
                        "SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive') as session_id, "
                        "COALESCE(message_type, 'legacy') as message_type, COUNT(*) as count, "
                        "MAX(timestamp) as last_time "
                        "FROM chat_history GROUP BY COALESCE(NULLIF(session_id, ''), 'legacy:archive') "
                        "ORDER BY last_time DESC"
                    )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Chat Archive API get_sessions error: {e}")
            return []

    @staticmethod
    def get_member_rank(
        session_id: str,
        limit: int = 10,
        since_ts: int = None,
        until_ts: int = None,
    ) -> list[dict]:
        try:
            limit = DatabaseManager._clamp_int(
                limit, 10, 1, DatabaseManager._MAX_QUERY_LIMIT
            )
            with get_db_connection() as conn:
                if (
                    since_ts is None
                    and until_ts is None
                    and column_exists(
                        conn, "session_user_stats", "visible_message_count"
                    )
                ):
                    cursor = conn.execute(
                        "SELECT user_id, "
                        "COALESCE(NULLIF(sender_name, ''), user_id) as sender_name, "
                        "COALESCE(avatar_url, '') as avatar_url, "
                        "COALESCE(platform_name, '') as platform_name, "
                        "visible_message_count as count "
                        "FROM session_user_stats "
                        "WHERE session_id = ? AND user_id IS NOT NULL "
                        "AND user_id != '' AND user_id != '0' "
                        "AND visible_message_count > 0 "
                        "ORDER BY visible_message_count DESC, user_id ASC LIMIT ?",
                        [str(session_id), limit],
                    )
                    return [dict(row) for row in cursor.fetchall()]
                query = (
                    "SELECT user_id, sender_name, COUNT(*) as count "
                    f"FROM chat_history WHERE {_session_predicate(session_id)} "
                    "AND user_id IS NOT NULL AND user_id != '' AND user_id != '0' "
                    "AND (is_recalled IS NULL OR is_recalled = 0)"
                )
                params: list = [str(session_id)]

                if since_ts is not None:
                    query += " AND timestamp >= ?"
                    params.append(int(since_ts))
                if until_ts is not None:
                    query += " AND timestamp <= ?"
                    params.append(int(until_ts))

                query += " GROUP BY user_id ORDER BY count DESC LIMIT ?"
                params.append(limit)

                cursor = conn.execute(query, params)
                rows = [dict(row) for row in cursor.fetchall()]
                if not rows:
                    return []
                user_ids = [str(row["user_id"]) for row in rows]
                placeholders = ",".join("?" for _ in user_ids)
                profiles = conn.execute(
                    f"SELECT user_id, sender_name, avatar_url, platform_name "
                    f"FROM session_user_stats WHERE session_id = ? "
                    f"AND user_id IN ({placeholders})",
                    [str(session_id), *user_ids],
                ).fetchall()
                profiles_by_id = {str(row["user_id"]): dict(row) for row in profiles}
                for row in rows:
                    profile = profiles_by_id.get(str(row["user_id"]), {})
                    row["sender_name"] = (
                        str(profile.get("sender_name") or "").strip()
                        or row.get("sender_name")
                        or row["user_id"]
                    )
                    row["avatar_url"] = str(profile.get("avatar_url") or "")
                    row["platform_name"] = str(profile.get("platform_name") or "")
                return rows
        except Exception as e:
            logger.error(f"Chat Archive API get_member_rank error: {e}")
            return []

    @staticmethod
    def get_user_summary(user_id: str, session_id: str = None) -> dict:
        summary = {
            "user_id": str(user_id),
            "total_messages": 0,
            "first_seen": None,
            "last_seen": None,
            "last_nickname": None,
        }
        try:
            with get_db_connection() as conn:
                stats_table = "session_user_stats" if session_id else "user_stats"
                if column_exists(conn, stats_table, "message_count"):
                    if session_id:
                        stats_row = conn.execute(
                            "SELECT message_count, first_time, last_time, sender_name "
                            "FROM session_user_stats WHERE session_id = ? AND user_id = ?",
                            [str(session_id), str(user_id)],
                        ).fetchone()
                    else:
                        stats_row = conn.execute(
                            "SELECT message_count, first_time, last_time, sender_name "
                            "FROM user_stats WHERE user_id = ?",
                            [str(user_id)],
                        ).fetchone()
                    if stats_row:
                        summary["total_messages"] = int(stats_row["message_count"] or 0)
                        summary["first_seen"] = stats_row["first_time"]
                        summary["last_seen"] = stats_row["last_time"]
                        summary["last_nickname"] = stats_row["sender_name"]
                        return summary
                where = "WHERE user_id = ?"
                params: list = [str(user_id)]
                if session_id:
                    where += " AND " + _session_predicate(session_id)
                    params.append(str(session_id))

                cursor = conn.execute(
                    f"SELECT COUNT(*) as cnt, "
                    f"MIN(timestamp) as first_ts, "
                    f"MAX(timestamp) as last_ts "
                    f"FROM chat_history {where}",
                    params,
                )
                row = cursor.fetchone()
                if row and row["cnt"] > 0:
                    summary["total_messages"] = row["cnt"]
                    summary["first_seen"] = row["first_ts"]
                    summary["last_seen"] = row["last_ts"]

                    name_cursor = conn.execute(
                        f"SELECT sender_name FROM chat_history "
                        f"{where} ORDER BY timestamp DESC LIMIT 1",
                        params,
                    )
                    name_row = name_cursor.fetchone()
                    summary["last_nickname"] = (
                        name_row["sender_name"] if name_row else None
                    )
        except Exception as e:
            logger.error(f"Chat Archive API get_user_summary error: {e}")
        return summary

    @staticmethod
    def get_message_count(
        user_id: str = None,
        session_id: str = None,
        since_ts: int = None,
        until_ts: int = None,
        exclude_recalled: bool = True,
    ) -> int:
        try:
            with get_db_connection() as conn:
                if since_ts is None and until_ts is None:
                    count_column = (
                        "visible_message_count" if exclude_recalled else "message_count"
                    )
                    if (
                        user_id
                        and session_id
                        and column_exists(conn, "session_user_stats", count_column)
                    ):
                        row = conn.execute(
                            f"SELECT {count_column} as cnt FROM session_user_stats "
                            "WHERE session_id = ? AND user_id = ?",
                            [str(session_id), str(user_id)],
                        ).fetchone()
                        return int(row["cnt"] or 0) if row else 0
                    if (
                        user_id
                        and not session_id
                        and column_exists(conn, "user_stats", count_column)
                    ):
                        row = conn.execute(
                            f"SELECT {count_column} as cnt FROM user_stats WHERE user_id = ?",
                            [str(user_id)],
                        ).fetchone()
                        return int(row["cnt"] or 0) if row else 0
                    if (
                        session_id
                        and not user_id
                        and not column_exists(conn, "session_stats", "message_count")
                        and column_exists(conn, "session_user_stats", count_column)
                    ):
                        row = conn.execute(
                            f"SELECT COALESCE(SUM({count_column}), 0) as cnt "
                            "FROM session_user_stats WHERE session_id = ?",
                            [str(session_id)],
                        ).fetchone()
                        return int(row["cnt"] or 0) if row else 0
                query = "SELECT COUNT(*) as cnt FROM chat_history WHERE 1=1"
                params: list = []

                if exclude_recalled:
                    query += " AND (is_recalled IS NULL OR is_recalled = 0)"
                if user_id:
                    query += " AND user_id = ?"
                    params.append(str(user_id))
                if session_id:
                    query += " AND " + _session_predicate(session_id)
                    params.append(str(session_id))
                if since_ts is not None:
                    query += " AND timestamp >= ?"
                    params.append(int(since_ts))
                if until_ts is not None:
                    query += " AND timestamp <= ?"
                    params.append(int(until_ts))

                row = conn.execute(query, params).fetchone()
                return row["cnt"] if row else 0
        except Exception as e:
            logger.error(f"Chat Archive API get_message_count error: {e}")
            return 0

    @classmethod
    def get_context_messages(
        cls,
        session_id: str,
        user_id: str = None,
        limit: int = 50,
        exclude_recalled: bool = True,
    ) -> list[tuple[str, str, str]]:
        records = cls.get_history(
            session_id=session_id,
            user_id=user_id,
            limit=limit,
            asc=False,
            exclude_recalled=exclude_recalled,
        )
        records.reverse()
        result = []
        for msg in records:
            ts = msg.get("timestamp", 0)
            ts_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            sender = msg.get("sender_name", "")
            content = msg.get("message", "")
            result.append((ts_str, sender, content))
        return result


_ALLOWED_TABLES = frozenset(
    {
        "chat_history",
        "session_stats",
        "user_stats",
        "session_user_stats",
        "sqlite_master",
    }
)
_FTS_TABLE = "chat_history_fts"
_FTS_DOCSIZE_TABLE = f"{_FTS_TABLE}_docsize"
_FTS_TRIGGERS = (
    "trg_chat_history_fts_insert",
    "trg_chat_history_fts_delete",
    "trg_chat_history_fts_update",
)
_FTS_MIN_KEYWORD_CHARS = 3
_FTS_READY = False


def _fts_match_query(keyword: str) -> str:
    return '"' + str(keyword).replace('"', '""') + '"'


def _sqlite_master_name_exists(db, name: str, object_type: str) -> bool:
    if object_type not in {"table", "index", "trigger", "view"}:
        raise ValueError(
            f"_sqlite_master_name_exists: disallowed object type '{object_type}'"
        )
    row = db.execute(
        "SELECT 1 as found FROM sqlite_master WHERE type = ? AND name = ? LIMIT 1;",
        [object_type, str(name)],
    ).fetchone()
    return row is not None


def ensure_fts_search(db, *, verify_external_content: bool = False) -> bool:
    """Create and maintain optional FTS5 trigram search acceleration."""
    global _FTS_READY
    try:
        db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} "
            "USING fts5(message, content='chat_history', content_rowid='id', tokenize='trigram');"
        )
        for trigger_name in _FTS_TRIGGERS:
            db.execute(f"DROP TRIGGER IF EXISTS {trigger_name};")
        db.execute(f"""CREATE TRIGGER {_FTS_TRIGGERS[0]}
        AFTER INSERT ON chat_history
        BEGIN
            INSERT INTO {_FTS_TABLE}(rowid, message) VALUES (NEW.id, NEW.message);
        END;""")
        db.execute(f"""CREATE TRIGGER {_FTS_TRIGGERS[1]}
        AFTER DELETE ON chat_history
        BEGIN
            INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, message) VALUES('delete', OLD.id, OLD.message);
        END;""")
        db.execute(f"""CREATE TRIGGER {_FTS_TRIGGERS[2]}
        AFTER UPDATE OF message ON chat_history
        BEGIN
            INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, message) VALUES('delete', OLD.id, OLD.message);
            INSERT INTO {_FTS_TABLE}(rowid, message) VALUES (NEW.id, NEW.message);
        END;""")

        if not _sqlite_master_name_exists(db, _FTS_DOCSIZE_TABLE, "table"):
            raise RuntimeError("FTS5 docsize shadow table is unavailable")
        counts = db.execute(
            f"SELECT (SELECT COUNT(*) FROM chat_history) AS history_count, "
            f"(SELECT COUNT(*) FROM {_FTS_DOCSIZE_TABLE}) AS indexed_count;"
        ).fetchone()
        history_count = int(counts["history_count"] or 0) if counts else 0
        indexed_count = int(counts["indexed_count"] or 0) if counts else 0
        rebuilt = indexed_count != history_count
        if rebuilt:
            db.execute(f"INSERT INTO {_FTS_TABLE}({_FTS_TABLE}) VALUES('rebuild');")
        elif verify_external_content:
            try:
                # rank=1 also compares an external-content index with the
                # underlying chat_history rows. Run this only on a schema
                # migration; it is intentionally not a per-startup full scan.
                db.execute_quiet(
                    f"INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rank) "
                    "VALUES('integrity-check', 1);"
                )
            except sqlite3.DatabaseError as integrity_error:
                logger.warning(
                    f"Chat Archive: FTS5 内容索引不一致，正在重建: {integrity_error}"
                )
                db.execute(f"INSERT INTO {_FTS_TABLE}({_FTS_TABLE}) VALUES('rebuild');")
        _FTS_READY = True
        return True
    except Exception as e:
        _FTS_READY = False
        logger.warning(
            f"Chat Archive: FTS5 search unavailable, falling back to substring scan: {e}"
        )
        return False


def add_message_search_condition(
    db,
    conditions: list[str],
    params: list,
    keyword: str,
    *,
    search_mode: str = "literal",
) -> bool:
    """Append parameterized search filters while preserving legacy callers.

    Args:
        db: Archive database connection.
        conditions: SQL predicates to extend.
        params: Bound values to extend in predicate order.
        keyword: User-supplied text, never interpreted as SQL or FTS syntax.
        search_mode: ``literal`` keeps the original substring behavior;
            ``terms`` requires every whitespace-separated term, with double
            quotes grouping a contiguous phrase.

    Returns:
        Whether the predicates use the optional trigram index.
    """
    keyword = str(keyword or "")
    if search_mode == "terms":
        # Only standalone, balanced double quotes group phrases. Quotes in
        # JSON, URLs, apostrophes and unmatched quotes remain literal text.
        terms = list(
            dict.fromkeys(
                phrase or word
                for phrase, word in re.findall(
                    r'(?<!\S)"([^"]+)"(?=\s|$)|(\S+)', keyword
                )
            )
        ) or [keyword]
        indexed_terms = [
            term
            for term in terms
            if len(term) >= _FTS_MIN_KEYWORD_CHARS and "\x00" not in term
        ]
        use_fts = bool(
            indexed_terms
            and _FTS_READY
            and _sqlite_master_name_exists(db, _FTS_TABLE, "table")
        )
        if use_fts:
            conditions.append(
                f"id IN (SELECT rowid FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH ?)"
            )
            params.append(
                " AND ".join(_fts_match_query(term) for term in indexed_terms)
            )
        # SQLite LOWER is ASCII-only. Verify indexed candidates (or scan short
        # terms) with Unicode case folding without rewriting archived messages.
        for term in terms:
            column = (
                "message"
                if term.lower() == term.upper()
                else "archive_search_fold(message)"
            )
            conditions.append(f"INSTR({column}, ?) > 0")
            params.append(term.casefold())
        return use_fts

    if len(keyword) < _FTS_MIN_KEYWORD_CHARS or "\x00" in keyword:
        # LIKE and LENGTH stop at embedded NUL characters in SQLite. INSTR
        # keeps scanning the complete value, which matters for expanded
        # forward messages that may contain NUL separators.
        conditions.append("INSTR(LOWER(message), LOWER(?)) > 0")
        params.append(keyword)
        return False

    if _FTS_READY and _sqlite_master_name_exists(db, _FTS_TABLE, "table"):
        conditions.append(
            f"id IN (SELECT rowid FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH ?) "
            "AND INSTR(LOWER(message), LOWER(?)) > 0"
        )
        params.extend([_fts_match_query(keyword), keyword])
        return True

    conditions.append("INSTR(LOWER(message), LOWER(?)) > 0")
    params.append(keyword)
    return False


def column_exists(db, table, column):
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"column_exists: disallowed table name '{table}'")
    cursor = db.execute(f"PRAGMA table_info({table});")
    columns = [row["name"] for row in cursor.fetchall()]
    return column in columns


def migrate_v1(db):
    if not column_exists(db, "chat_history", "session_id"):
        db.execute("ALTER TABLE chat_history ADD COLUMN session_id TEXT;")
    if not column_exists(db, "chat_history", "message_type"):
        db.execute("ALTER TABLE chat_history ADD COLUMN message_type TEXT;")


def migrate_v2(db):
    if not column_exists(db, "chat_history", "session_name"):
        db.execute("ALTER TABLE chat_history ADD COLUMN session_name TEXT;")


def migrate_v3(db):
    if not column_exists(db, "chat_history", "msg_id"):
        db.execute("ALTER TABLE chat_history ADD COLUMN msg_id TEXT;")


def migrate_v4(db):
    if not column_exists(db, "chat_history", "is_recalled"):
        db.execute("ALTER TABLE chat_history ADD COLUMN is_recalled INTEGER DEFAULT 0;")


def migrate_v5(db):
    """Add additive media flags for fast dashboard aggregates.

    Defaults keep all existing INSERT statements compatible. Existing rows are
    backfilled from CQ codes once the columns exist.
    """
    added_any = False
    if not column_exists(db, "chat_history", "has_image"):
        db.execute("ALTER TABLE chat_history ADD COLUMN has_image INTEGER DEFAULT 0;")
        added_any = True
    if not column_exists(db, "chat_history", "has_video"):
        db.execute("ALTER TABLE chat_history ADD COLUMN has_video INTEGER DEFAULT 0;")
        added_any = True
    if not column_exists(db, "chat_history", "msg_kind"):
        db.execute("ALTER TABLE chat_history ADD COLUMN msg_kind TEXT DEFAULT 'text';")
        added_any = True

    if added_any:
        db.execute("""UPDATE chat_history
            SET has_image = CASE WHEN message LIKE '%[CQ:image%' THEN 1 ELSE COALESCE(has_image, 0) END,
                has_video = CASE WHEN message LIKE '%[CQ:video%' THEN 1 ELSE COALESCE(has_video, 0) END,
                msg_kind = CASE
                    WHEN message LIKE '%[CQ:image%' THEN 'image'
                    WHEN message LIKE '%[CQ:video%' THEN 'video'
                    WHEN message LIKE '%[CQ:record%' THEN 'record'
                    WHEN message LIKE '%[CQ:file%' THEN 'file'
                    WHEN message LIKE '%[CQ:%' THEN 'other'
                    ELSE COALESCE(NULLIF(msg_kind, ''), 'text')
                END
        """)


def migrate_v6(db):
    """Add platform provenance fields for cross-platform archive writes."""
    if not column_exists(db, "chat_history", "platform_id"):
        db.execute("ALTER TABLE chat_history ADD COLUMN platform_id TEXT;")
    if not column_exists(db, "chat_history", "platform_name"):
        db.execute("ALTER TABLE chat_history ADD COLUMN platform_name TEXT;")


def migrate_v7(db):
    """Add optional sender avatar URL for platforms that expose profile photos."""
    if not column_exists(db, "chat_history", "avatar_url"):
        db.execute("ALTER TABLE chat_history ADD COLUMN avatar_url TEXT;")


def migrate_v8(db):
    """Add optional guild/server avatar URL for platforms that support server structures."""
    if not column_exists(db, "chat_history", "guild_avatar_url"):
        db.execute("ALTER TABLE chat_history ADD COLUMN guild_avatar_url TEXT;")


def ensure_media_flags(db):
    """Create indexes/triggers for dashboard media flags if schema supports them."""
    if not all(
        column_exists(db, "chat_history", col)
        for col in ("has_image", "has_video", "msg_kind")
    ):
        return

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_history_has_image_true ON chat_history(has_image) WHERE has_image = 1;"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_history_has_video_true ON chat_history(has_video) WHERE has_video = 1;"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_history_msg_kind ON chat_history(msg_kind);"
    )

    # Keep rows inserted by old callers correct without changing their INSERT
    # column list. Plain text rows keep DEFAULT values and avoid this UPDATE.
    db.execute("DROP TRIGGER IF EXISTS trg_chat_history_media_flags_insert;")
    db.execute("""CREATE TRIGGER trg_chat_history_media_flags_insert
    AFTER INSERT ON chat_history
    WHEN NEW.message LIKE '%[CQ:%'
    BEGIN
        UPDATE chat_history
        SET has_image = CASE WHEN NEW.message LIKE '%[CQ:image%' THEN 1 ELSE COALESCE(NEW.has_image, 0) END,
            has_video = CASE WHEN NEW.message LIKE '%[CQ:video%' THEN 1 ELSE COALESCE(NEW.has_video, 0) END,
            msg_kind = CASE
                WHEN NEW.message LIKE '%[CQ:image%' THEN 'image'
                WHEN NEW.message LIKE '%[CQ:video%' THEN 'video'
                WHEN NEW.message LIKE '%[CQ:record%' THEN 'record'
                WHEN NEW.message LIKE '%[CQ:file%' THEN 'file'
                WHEN NEW.message LIKE '%[CQ:%' THEN 'other'
                ELSE COALESCE(NEW.msg_kind, 'text')
            END
        WHERE id = NEW.id;
    END;""")


def ensure_session_stats(db):
    """Create and backfill a session summary table without changing chat_history."""
    db.execute("""CREATE TABLE IF NOT EXISTS session_stats (
        session_id TEXT PRIMARY KEY,
        message_type TEXT,
        session_name TEXT,
        last_msg TEXT,
        sender_name TEXT,
        last_time INTEGER,
        last_message_id INTEGER,
        message_count INTEGER DEFAULT 0,
        avatar_url TEXT,
        platform_name TEXT,
        guild_avatar_url TEXT
    )""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_stats_last_time ON session_stats(last_time DESC);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_stats_message_count ON session_stats(message_count DESC);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_stats_type_count ON session_stats(message_type, message_count DESC);"
    )
    for col in ("avatar_url", "platform_name", "guild_avatar_url"):
        if not column_exists(db, "session_stats", col):
            db.execute(f"ALTER TABLE session_stats ADD COLUMN {col} TEXT;")

    # Recreate owned triggers so upgrades replace stale bodies left by earlier
    # plugin versions instead of silently preserving them forever.
    db.execute("DROP TRIGGER IF EXISTS trg_chat_history_session_stats_insert;")
    db.execute("""CREATE TRIGGER trg_chat_history_session_stats_insert
    AFTER INSERT ON chat_history
    BEGIN
        INSERT INTO session_stats (
            session_id, message_type, session_name, last_msg, sender_name,
            last_time, last_message_id, message_count,
            avatar_url, platform_name, guild_avatar_url
        ) VALUES (
            COALESCE(NULLIF(NEW.session_id, ''), 'legacy:archive'),
            COALESCE(NEW.message_type, 'legacy'),
            NEW.session_name,
            NEW.message,
            NEW.sender_name,
            NEW.timestamp,
            NEW.id,
            1,
            NEW.avatar_url,
            NEW.platform_name,
            NEW.guild_avatar_url
        )
        ON CONFLICT(session_id) DO UPDATE SET
            message_count = session_stats.message_count + 1,
            message_type = CASE
                WHEN COALESCE(excluded.last_time, 0) > COALESCE(session_stats.last_time, 0)
                  OR (
                    COALESCE(excluded.last_time, 0) = COALESCE(session_stats.last_time, 0)
                    AND excluded.last_message_id > session_stats.last_message_id
                  )
                THEN excluded.message_type
                ELSE session_stats.message_type
            END,
            session_name = CASE
                WHEN excluded.session_name IS NOT NULL AND excluded.session_name != ''
                THEN excluded.session_name
                ELSE session_stats.session_name
            END,
            last_msg = CASE
                WHEN COALESCE(excluded.last_time, 0) > COALESCE(session_stats.last_time, 0)
                  OR (
                    COALESCE(excluded.last_time, 0) = COALESCE(session_stats.last_time, 0)
                    AND excluded.last_message_id > session_stats.last_message_id
                  )
                THEN excluded.last_msg
                ELSE session_stats.last_msg
            END,
            sender_name = CASE
                WHEN COALESCE(excluded.last_time, 0) > COALESCE(session_stats.last_time, 0)
                  OR (
                    COALESCE(excluded.last_time, 0) = COALESCE(session_stats.last_time, 0)
                    AND excluded.last_message_id > session_stats.last_message_id
                  )
                THEN excluded.sender_name
                ELSE session_stats.sender_name
            END,
            last_time = CASE
                WHEN COALESCE(excluded.last_time, 0) > COALESCE(session_stats.last_time, 0)
                  OR (
                    COALESCE(excluded.last_time, 0) = COALESCE(session_stats.last_time, 0)
                    AND excluded.last_message_id > session_stats.last_message_id
                  )
                THEN excluded.last_time
                ELSE session_stats.last_time
            END,
            last_message_id = CASE
                WHEN COALESCE(excluded.last_time, 0) > COALESCE(session_stats.last_time, 0)
                  OR (
                    COALESCE(excluded.last_time, 0) = COALESCE(session_stats.last_time, 0)
                    AND excluded.last_message_id > session_stats.last_message_id
                  )
                THEN excluded.last_message_id
                ELSE session_stats.last_message_id
            END,
            avatar_url = CASE
                WHEN excluded.avatar_url IS NOT NULL AND excluded.avatar_url != '' THEN excluded.avatar_url
                ELSE session_stats.avatar_url
            END,
            platform_name = CASE
                WHEN excluded.platform_name IS NOT NULL AND excluded.platform_name != '' THEN excluded.platform_name
                ELSE session_stats.platform_name
            END,
            guild_avatar_url = CASE
                WHEN excluded.guild_avatar_url IS NOT NULL AND excluded.guild_avatar_url != '' THEN excluded.guild_avatar_url
                ELSE session_stats.guild_avatar_url
            END;
    END;""")

    history_row = db.execute("SELECT COUNT(*) as cnt FROM chat_history;").fetchone()
    session_row = db.execute("""
        SELECT COUNT(*) as cnt FROM (
            SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive')
            FROM chat_history
            GROUP BY COALESCE(NULLIF(session_id, ''), 'legacy:archive')
        );
    """).fetchone()
    stats_row = db.execute(
        "SELECT COUNT(*) as rows_cnt, COALESCE(SUM(message_count), 0) as msg_cnt FROM session_stats;"
    ).fetchone()
    latest_drift = db.execute("""
        SELECT 1 as found
        FROM session_stats stats
        WHERE stats.last_message_id IS NOT CASE
            WHEN stats.session_id = 'legacy:archive' THEN (
                SELECT history.id
                FROM chat_history history
                WHERE history.session_id IS NULL OR history.session_id = ''
                ORDER BY history.timestamp DESC, history.id DESC
                LIMIT 1
            )
            ELSE (
                SELECT history.id
                FROM chat_history history
                WHERE history.session_id = stats.session_id
                ORDER BY history.timestamp DESC, history.id DESC
                LIMIT 1
            )
        END
        LIMIT 1;
    """).fetchone()
    history_count = int(history_row["cnt"] or 0) if history_row else 0
    session_count = int(session_row["cnt"] or 0) if session_row else 0
    stats_rows = int(stats_row["rows_cnt"] or 0) if stats_row else 0
    stats_messages = int(stats_row["msg_cnt"] or 0) if stats_row else 0
    if (
        stats_rows != session_count
        or stats_messages != history_count
        or latest_drift is not None
    ):
        logger.warning(
            "Chat Archive: session_stats 与 chat_history 不一致，正在重建会话汇总表。"
        )
        rebuild_session_stats(db)


def rebuild_session_stats(db):
    """Rebuild session summary rows from chat_history when migrations drift."""
    db.execute("DELETE FROM session_stats;")
    db.execute("""WITH grouped AS (
            SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive') as session_id,
                   COUNT(*) as message_count,
                   MAX(CASE
                       WHEN session_name IS NOT NULL AND session_name != '' THEN id
                   END) as session_name_id,
                   MAX(CASE
                       WHEN avatar_url IS NOT NULL AND avatar_url != '' THEN id
                   END) as avatar_id,
                   MAX(CASE
                       WHEN platform_name IS NOT NULL AND platform_name != '' THEN id
                   END) as platform_id,
                   MAX(CASE
                       WHEN guild_avatar_url IS NOT NULL AND guild_avatar_url != '' THEN id
                   END) as guild_avatar_id
            FROM chat_history
            GROUP BY COALESCE(NULLIF(session_id, ''), 'legacy:archive')
        ),
        ranked AS (
            SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive') as session_id,
                   message_type, session_name, message, sender_name,
                   timestamp, id, avatar_url, platform_name, guild_avatar_url,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(NULLIF(session_id, ''), 'legacy:archive')
                       ORDER BY COALESCE(timestamp, 0) DESC, id DESC
                   ) as row_rank
            FROM chat_history
        )
        INSERT OR REPLACE INTO session_stats (
            session_id, message_type, session_name, last_msg, sender_name,
            last_time, last_message_id, message_count,
            avatar_url, platform_name, guild_avatar_url
        )
        SELECT grouped.session_id,
               COALESCE(latest.message_type, 'legacy') as message_type,
               COALESCE(named.session_name, latest.session_name),
               latest.message as last_msg,
               latest.sender_name,
               latest.timestamp as last_time,
               latest.id as last_message_id,
               grouped.message_count,
               COALESCE(avatar.avatar_url, latest.avatar_url),
               COALESCE(platform.platform_name, latest.platform_name),
               COALESCE(guild.guild_avatar_url, latest.guild_avatar_url)
        FROM grouped
        JOIN ranked latest
          ON latest.session_id = grouped.session_id AND latest.row_rank = 1
        LEFT JOIN chat_history named ON named.id = grouped.session_name_id
        LEFT JOIN chat_history avatar ON avatar.id = grouped.avatar_id
        LEFT JOIN chat_history platform ON platform.id = grouped.platform_id
        LEFT JOIN chat_history guild ON guild.id = grouped.guild_avatar_id;
    """)


def ensure_user_stats(db, *, validate: bool = True):
    """Maintain durable global and per-session user/profile summaries."""
    db.execute("""CREATE TABLE IF NOT EXISTS user_stats (
        user_id TEXT PRIMARY KEY,
        message_count INTEGER NOT NULL DEFAULT 0,
        visible_message_count INTEGER NOT NULL DEFAULT 0,
        sender_name TEXT,
        avatar_url TEXT,
        platform_name TEXT,
        first_time INTEGER,
        last_time INTEGER,
        last_message_id INTEGER
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS session_user_stats (
        session_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        message_count INTEGER NOT NULL DEFAULT 0,
        visible_message_count INTEGER NOT NULL DEFAULT 0,
        sender_name TEXT,
        avatar_url TEXT,
        platform_name TEXT,
        first_time INTEGER,
        last_time INTEGER,
        last_message_id INTEGER,
        PRIMARY KEY (session_id, user_id)
    )""")
    for table in ("user_stats", "session_user_stats"):
        if not column_exists(db, table, "visible_message_count"):
            db.execute(
                f"ALTER TABLE {table} ADD COLUMN "
                "visible_message_count INTEGER NOT NULL DEFAULT 0;"
            )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_stats_visible_count "
        "ON user_stats(visible_message_count DESC, user_id);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_user_stats_visible_count "
        "ON session_user_stats(session_id, visible_message_count DESC, user_id);"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_user_stats_user "
        "ON session_user_stats(user_id, last_message_id DESC);"
    )

    db.execute("DROP TRIGGER IF EXISTS trg_chat_history_user_stats_insert;")
    db.execute("""CREATE TRIGGER trg_chat_history_user_stats_insert
    AFTER INSERT ON chat_history
    WHEN NEW.user_id IS NOT NULL AND NEW.user_id != ''
    BEGIN
        INSERT INTO user_stats (
            user_id, message_count, visible_message_count,
            sender_name, avatar_url, platform_name,
            first_time, last_time, last_message_id
        ) VALUES (
            NEW.user_id, 1,
            CASE WHEN NEW.is_recalled IS NULL OR NEW.is_recalled = 0 THEN 1 ELSE 0 END,
            NEW.sender_name, NEW.avatar_url, NEW.platform_name,
            NEW.timestamp, NEW.timestamp, NEW.id
        )
        ON CONFLICT(user_id) DO UPDATE SET
            message_count = user_stats.message_count + 1,
            visible_message_count = user_stats.visible_message_count + excluded.visible_message_count,
            first_time = CASE
                WHEN user_stats.first_time IS NULL OR excluded.first_time < user_stats.first_time
                THEN excluded.first_time ELSE user_stats.first_time END,
            sender_name = CASE
                WHEN excluded.last_message_id >= user_stats.last_message_id
                     AND excluded.sender_name IS NOT NULL AND excluded.sender_name != ''
                THEN excluded.sender_name ELSE user_stats.sender_name END,
            avatar_url = CASE
                WHEN excluded.avatar_url IS NOT NULL AND excluded.avatar_url != ''
                THEN excluded.avatar_url ELSE user_stats.avatar_url END,
            platform_name = CASE
                WHEN excluded.platform_name IS NOT NULL AND excluded.platform_name != ''
                THEN excluded.platform_name ELSE user_stats.platform_name END,
            last_time = CASE
                WHEN user_stats.last_time IS NULL OR excluded.last_time > user_stats.last_time
                THEN excluded.last_time ELSE user_stats.last_time END,
            last_message_id = MAX(user_stats.last_message_id, excluded.last_message_id);

        INSERT INTO session_user_stats (
            session_id, user_id, message_count, visible_message_count,
            sender_name, avatar_url,
            platform_name, first_time, last_time, last_message_id
        ) VALUES (
            COALESCE(NULLIF(NEW.session_id, ''), 'legacy:archive'),
            NEW.user_id, 1,
            CASE WHEN NEW.is_recalled IS NULL OR NEW.is_recalled = 0 THEN 1 ELSE 0 END,
            NEW.sender_name, NEW.avatar_url, NEW.platform_name,
            NEW.timestamp, NEW.timestamp, NEW.id
        )
        ON CONFLICT(session_id, user_id) DO UPDATE SET
            message_count = session_user_stats.message_count + 1,
            visible_message_count = session_user_stats.visible_message_count + excluded.visible_message_count,
            first_time = CASE
                WHEN session_user_stats.first_time IS NULL OR excluded.first_time < session_user_stats.first_time
                THEN excluded.first_time ELSE session_user_stats.first_time END,
            sender_name = CASE
                WHEN excluded.last_message_id >= session_user_stats.last_message_id
                     AND excluded.sender_name IS NOT NULL AND excluded.sender_name != ''
                THEN excluded.sender_name ELSE session_user_stats.sender_name END,
            avatar_url = CASE
                WHEN excluded.avatar_url IS NOT NULL AND excluded.avatar_url != ''
                THEN excluded.avatar_url ELSE session_user_stats.avatar_url END,
            platform_name = CASE
                WHEN excluded.platform_name IS NOT NULL AND excluded.platform_name != ''
                THEN excluded.platform_name ELSE session_user_stats.platform_name END,
            last_time = CASE
                WHEN session_user_stats.last_time IS NULL OR excluded.last_time > session_user_stats.last_time
                THEN excluded.last_time ELSE session_user_stats.last_time END,
            last_message_id = MAX(session_user_stats.last_message_id, excluded.last_message_id);
    END;""")

    db.execute("DROP TRIGGER IF EXISTS trg_chat_history_user_stats_recall;")
    db.execute("""CREATE TRIGGER trg_chat_history_user_stats_recall
    AFTER UPDATE OF is_recalled ON chat_history
    WHEN NEW.user_id IS NOT NULL AND NEW.user_id != ''
         AND COALESCE(OLD.is_recalled, 0) != COALESCE(NEW.is_recalled, 0)
    BEGIN
        UPDATE user_stats
        SET visible_message_count = MAX(
            0,
            visible_message_count + CASE
                WHEN COALESCE(OLD.is_recalled, 0) = 0 AND COALESCE(NEW.is_recalled, 0) != 0 THEN -1
                WHEN COALESCE(OLD.is_recalled, 0) != 0 AND COALESCE(NEW.is_recalled, 0) = 0 THEN 1
                ELSE 0 END
        )
        WHERE user_id = NEW.user_id;

        UPDATE session_user_stats
        SET visible_message_count = MAX(
            0,
            visible_message_count + CASE
                WHEN COALESCE(OLD.is_recalled, 0) = 0 AND COALESCE(NEW.is_recalled, 0) != 0 THEN -1
                WHEN COALESCE(OLD.is_recalled, 0) != 0 AND COALESCE(NEW.is_recalled, 0) = 0 THEN 1
                ELSE 0 END
        )
        WHERE session_id = COALESCE(NULLIF(NEW.session_id, ''), 'legacy:archive')
          AND user_id = NEW.user_id;
    END;""")

    if not validate:
        return
    source_row = db.execute(
        "SELECT COUNT(*) as cnt, "
        "SUM(CASE WHEN is_recalled IS NULL OR is_recalled = 0 THEN 1 ELSE 0 END) as visible_cnt "
        "FROM chat_history "
        "WHERE user_id IS NOT NULL AND user_id != '';"
    ).fetchone()
    global_row = db.execute(
        "SELECT COALESCE(SUM(message_count), 0) as cnt, "
        "COALESCE(SUM(visible_message_count), 0) as visible_cnt FROM user_stats;"
    ).fetchone()
    session_row = db.execute(
        "SELECT COALESCE(SUM(message_count), 0) as cnt, "
        "COALESCE(SUM(visible_message_count), 0) as visible_cnt FROM session_user_stats;"
    ).fetchone()
    source_count = int(source_row["cnt"] or 0) if source_row else 0
    source_visible = int(source_row["visible_cnt"] or 0) if source_row else 0
    global_count = int(global_row["cnt"] or 0) if global_row else 0
    global_visible = int(global_row["visible_cnt"] or 0) if global_row else 0
    session_count = int(session_row["cnt"] or 0) if session_row else 0
    session_visible = int(session_row["visible_cnt"] or 0) if session_row else 0
    if (
        global_count != source_count
        or session_count != source_count
        or global_visible != source_visible
        or session_visible != source_visible
    ):
        logger.warning("Chat Archive: 用户汇总表与 chat_history 不一致，正在重建。")
        rebuild_user_stats(db)


def rebuild_user_stats(db, user_ids=None):
    """Rebuild global and per-session user summaries in two bounded scans."""
    if user_ids is not None:
        user_ids = sorted({str(uid) for uid in user_ids if uid})
        if not user_ids:
            return
        placeholders = ",".join("?" for _ in user_ids)
        condition = f" AND user_id IN ({placeholders})"
        db.execute(
            f"DELETE FROM user_stats WHERE user_id IN ({placeholders})", user_ids
        )
        db.execute(
            f"DELETE FROM session_user_stats WHERE user_id IN ({placeholders})",
            user_ids,
        )
    else:
        user_ids = []
        condition = ""
        db.execute("DELETE FROM user_stats;")
        db.execute("DELETE FROM session_user_stats;")
    db.execute(
        f"""WITH grouped AS (
            SELECT user_id,
                   COUNT(*) as message_count,
                   SUM(CASE WHEN is_recalled IS NULL OR is_recalled = 0 THEN 1 ELSE 0 END) as visible_message_count,
                   MIN(timestamp) as first_time,
                   MAX(timestamp) as last_time,
                   MAX(id) as latest_id,
                   MAX(CASE WHEN sender_name IS NOT NULL AND sender_name != '' THEN id END) as sender_name_id,
                   MAX(CASE WHEN avatar_url IS NOT NULL AND avatar_url != '' THEN id END) as avatar_id,
                   MAX(CASE WHEN platform_name IS NOT NULL AND platform_name != '' THEN id END) as platform_id
            FROM chat_history
            WHERE user_id IS NOT NULL AND user_id != '' {condition}
            GROUP BY user_id
        )
        INSERT INTO user_stats (
            user_id, message_count, visible_message_count,
            sender_name, avatar_url, platform_name,
            first_time, last_time, last_message_id
        )
        SELECT grouped.user_id,
               grouped.message_count,
               grouped.visible_message_count,
               COALESCE(named.sender_name, latest.sender_name),
               COALESCE(avatar.avatar_url, latest.avatar_url, ''),
               COALESCE(platform.platform_name, latest.platform_name, ''),
               grouped.first_time,
               grouped.last_time,
               grouped.latest_id
        FROM grouped
        JOIN chat_history latest ON latest.id = grouped.latest_id
        LEFT JOIN chat_history named ON named.id = grouped.sender_name_id
        LEFT JOIN chat_history avatar ON avatar.id = grouped.avatar_id
        LEFT JOIN chat_history platform ON platform.id = grouped.platform_id;
    """,
        user_ids,
    )
    db.execute(
        f"""WITH grouped AS (
            SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive') as normalized_session_id,
                   user_id,
                   COUNT(*) as message_count,
                   SUM(CASE WHEN is_recalled IS NULL OR is_recalled = 0 THEN 1 ELSE 0 END) as visible_message_count,
                   MIN(timestamp) as first_time,
                   MAX(timestamp) as last_time,
                   MAX(id) as latest_id,
                   MAX(CASE WHEN sender_name IS NOT NULL AND sender_name != '' THEN id END) as sender_name_id,
                   MAX(CASE WHEN avatar_url IS NOT NULL AND avatar_url != '' THEN id END) as avatar_id,
                   MAX(CASE WHEN platform_name IS NOT NULL AND platform_name != '' THEN id END) as platform_id
            FROM chat_history
            WHERE user_id IS NOT NULL AND user_id != '' {condition}
            GROUP BY COALESCE(NULLIF(session_id, ''), 'legacy:archive'), user_id
        )
        INSERT INTO session_user_stats (
            session_id, user_id, message_count, visible_message_count,
            sender_name, avatar_url,
            platform_name, first_time, last_time, last_message_id
        )
        SELECT grouped.normalized_session_id,
               grouped.user_id,
               grouped.message_count,
               grouped.visible_message_count,
               COALESCE(named.sender_name, latest.sender_name),
               COALESCE(avatar.avatar_url, latest.avatar_url, ''),
               COALESCE(platform.platform_name, latest.platform_name, ''),
               grouped.first_time,
               grouped.last_time,
               grouped.latest_id
        FROM grouped
        JOIN chat_history latest ON latest.id = grouped.latest_id
        LEFT JOIN chat_history named ON named.id = grouped.sender_name_id
        LEFT JOIN chat_history avatar ON avatar.id = grouped.avatar_id
        LEFT JOIN chat_history platform ON platform.id = grouped.platform_id;
    """,
        user_ids,
    )


def migrate_v9(db):
    """Retire indexes unused by the supported plugin query surface."""
    for index_name in (
        "idx_platform_id",
        "idx_platform_session",
        "idx_avatar_user",
        "idx_guild_avatar",
    ):
        db.execute(f"DROP INDEX IF EXISTS {index_name};")


def migrate_v10(db):
    """Add maintained user/profile summary tables."""
    ensure_user_stats(db, validate=True)


def migrate_v13(db):
    """Retire redundant indexes that amplify every archive write."""
    for index_name in (
        "idx_session",
        "idx_user_timestamp",
        "idx_session_recall_id_desc",
        "idx_session_user_recall_ts",
        "idx_has_image",
        "idx_has_video",
        "idx_has_face",
        "idx_has_voice",
    ):
        db.execute(f"DROP INDEX IF EXISTS {index_name};")


def migrate_telegram_channel_messages(db) -> bool:
    """Atomically normalize legacy Telegram channel rows and summaries."""
    row = db.execute("""
        SELECT COUNT(*) as cnt
        FROM chat_history
        WHERE platform_name = 'telegram'
          AND user_id LIKE '-%'
          AND message_type = 'GroupMessage';
    """).fetchone()
    if not row or int(row["cnt"] or 0) == 0:
        return False

    savepoint = "telegram_channel_migration"
    db.execute(f"SAVEPOINT {savepoint};")
    try:
        db.execute("""
            UPDATE chat_history
            SET message_type = 'ChannelMessage',
                session_id = REPLACE(
                    session_id,
                    ':GroupMessage:',
                    ':ChannelMessage:'
                )
            WHERE platform_name = 'telegram'
              AND user_id LIKE '-%'
              AND message_type = 'GroupMessage';
        """)
        rebuild_session_stats(db)
        rebuild_user_stats(db)
        db.execute(f"RELEASE SAVEPOINT {savepoint};")
    except Exception:
        try:
            db.execute_quiet(f"ROLLBACK TO SAVEPOINT {savepoint};")
            db.execute_quiet(f"RELEASE SAVEPOINT {savepoint};")
        except Exception as rollback_error:
            logger.error(
                "Chat Archive: Telegram migration savepoint rollback failed: "
                f"{rollback_error}"
            )
            db.rollback()
        raise
    return True


def init_db():
    """Initialize the archive schema while serializing concurrent callers."""
    with _INIT_DB_LOCK:
        _initialize_database()


def _initialize_database():
    """Perform schema initialization while the caller holds the init lock."""
    db_path = Path(DB_PATH).expanduser()
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = get_db_connection()
    try:
        # 检测表是否已存在
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_history';"
        )
        table_exists = cursor.fetchone() is not None

        previous_version = 0
        if not table_exists:
            # 1. 全新数据库：直接建立包含所有最新列的完整表，免去中间迁移步骤
            db.execute("""CREATE TABLE chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                sender_name TEXT,
                message TEXT,
                timestamp INTEGER,
                session_id TEXT,
                message_type TEXT,
                session_name TEXT,
                msg_id TEXT,
                is_recalled INTEGER DEFAULT 0,
                has_image INTEGER DEFAULT 0,
                has_video INTEGER DEFAULT 0,
                msg_kind TEXT DEFAULT 'text',
                platform_id TEXT,
                platform_name TEXT,
                avatar_url TEXT,
                guild_avatar_url TEXT
            )""")
            # 将 user_version 置为当前最高迁移版本
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
            db.commit()
        else:
            # 2. 存量数据库：基于 PRAGMA user_version 进行有序增量迁移
            cursor = db.execute("PRAGMA user_version;")
            row = cursor.fetchone()
            current_version = row["user_version"] if row else 0
            previous_version = int(current_version or 0)

            migrations = [
                (1, migrate_v1),
                (2, migrate_v2),
                (3, migrate_v3),
                (4, migrate_v4),
                (5, migrate_v5),
                (6, migrate_v6),
                (7, migrate_v7),
                (8, migrate_v8),
                (9, migrate_v9),
                (10, migrate_v10),
            ]

            for version, migrate_func in migrations:
                if current_version < version:
                    migrate_func(db)
                    db.execute(f"PRAGMA user_version = {version};")
                    db.commit()

            # v5 is additive/idempotent; this repairs partially migrated schemas
            # without doing an expensive media LIKE backfill on every startup.
            if not all(
                column_exists(db, "chat_history", col)
                for col in ("has_image", "has_video", "msg_kind")
            ):
                migrate_v5(db)
            if not all(
                column_exists(db, "chat_history", col)
                for col in ("platform_id", "platform_name")
            ):
                migrate_v6(db)
            if not column_exists(db, "chat_history", "avatar_url"):
                migrate_v7(db)
            if not column_exists(db, "chat_history", "guild_avatar_url"):
                migrate_v8(db)
            if previous_version < 13:
                migrate_v13(db)

        # 3. 始终确保必要的高性能索引已创建（幂等）
        db.execute("CREATE INDEX IF NOT EXISTS idx_msg_id ON chat_history(msg_id);")
        # The global member ranking uses this ordering to avoid a temporary
        # GROUP BY B-tree; the wider timestamp index is not an equivalent plan.
        db.execute("CREATE INDEX IF NOT EXISTS idx_user ON chat_history(user_id);")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_timestamp ON chat_history(session_id, timestamp);"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_timestamp ON chat_history(timestamp);"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_id_desc ON chat_history(session_id, id DESC);"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_ts_id_desc ON chat_history(user_id, timestamp DESC, id DESC);"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_user_ts_id_desc ON chat_history(session_id, user_id, timestamp DESC, id DESC);"
        )
        ensure_media_flags(db)
        fts_ready = ensure_fts_search(
            db,
            verify_external_content=(
                table_exists and previous_version < FTS_EXTERNAL_CONTENT_CHECK_VERSION
            ),
        )
        if table_exists and previous_version < SCHEMA_VERSION and fts_ready:
            # Mark the one-time external-content integrity verification only
            # after it succeeds. A failed rebuild is retried next startup.
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
            db.commit()
        ensure_session_stats(db)
        ensure_user_stats(db)
        try:
            from .archive_management import ensure_management_schema
        except ImportError:
            from archive_management import ensure_management_schema
        ensure_management_schema(db)

        # Do not commit a half-migrated archive if either summary rebuild fails.
        migrate_telegram_channel_messages(db)

        db.commit()
        analysis_limit = None
        try:
            # A fresh pooled connection has no query history. Ask SQLite to
            # inspect every table so it can choose selective indexes such as
            # idx_msg_id instead of scanning a large session. SQLite < 3.46
            # needs an explicit bound to keep this startup maintenance cheap.
            if sqlite3.sqlite_version_info < (3, 46, 0):
                row = db.execute_quiet("PRAGMA analysis_limit;").fetchone()
                analysis_limit = int(row.get("analysis_limit", 0) or 0) if row else 0
                db.execute_quiet("PRAGMA analysis_limit=1000;").fetchone()
            db.execute_quiet("PRAGMA optimize=0x10002;").fetchall()
        except Exception as e:
            logger.warning(f"Chat Archive: SQLite 查询统计维护失败: {e}")
        finally:
            if analysis_limit is not None:
                with suppress(Exception):
                    db.execute_quiet(
                        f"PRAGMA analysis_limit={analysis_limit};"
                    ).fetchone()
        for sqlite_path in (
            db_path,
            Path(str(db_path) + "-wal"),
            Path(str(db_path) + "-shm"),
        ):
            with suppress(Exception):
                if sqlite_path.exists():
                    sqlite_path.chmod(0o600)
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
