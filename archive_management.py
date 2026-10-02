"""Recoverable message management; cached attachments are never removed here."""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import threading
import time
from pathlib import Path

BATCH_LIMIT = 500
PREVIEW_TTL = 300


class ManagementConflict(ValueError):
    pass


def ensure_management_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS archive_trash (
        original_id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,
        session_id TEXT NOT NULL, row_json TEXT NOT NULL
    )""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_archive_trash_operation ON archive_trash(operation_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_archive_trash_session ON archive_trash(session_id)"
    )
    db.execute("""CREATE TABLE IF NOT EXISTS archive_management_operations (
        operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        created_at INTEGER NOT NULL, reason TEXT NOT NULL, message_count INTEGER NOT NULL
    )""")


def session_scope(session_id, message_id=0, before_ts=0, start_ts=0, end_ts=0):
    """Build a parameterized session scope with inclusive start and end bounds."""
    sid = str(session_id or "").strip()
    if not sid or len(sid) > 256:
        raise ValueError("必须指定完整会话 ID")
    for value in (message_id, before_ts, start_ts, end_ts):
        if type(value) is not int or value < 0:
            raise ValueError("消息 ID 或时间无效")
    if start_ts and end_ts and start_ts > end_ts:
        raise ValueError("开始时间不能晚于结束时间")
    where = (
        "COALESCE(NULLIF(session_id, ''), 'legacy:archive') = ?"
        if sid == "legacy:archive"
        else "session_id = ?"
    )
    params = [sid]
    for value, condition in (
        (message_id, "id = ?"),
        (before_ts, "timestamp < ?"),
        (start_ts, "timestamp >= ?"),
        (end_ts, "timestamp <= ?"),
    ):
        if value:
            where += " AND " + condition
            params.append(value)
    return where, params


def _scope_snapshot(db, where, params):
    """Fingerprint every selected row without keeping the whole archive in memory.

    Args:
        db: Connection owned by the caller's read or write transaction.
        where: Parameterized message selection predicate.
        params: Values bound to the selection predicate.

    Returns:
        The exact row count, content fingerprint, highest ID and text byte count.
    """
    digest = hashlib.sha256()
    last_id = count = message_bytes = 0
    while True:
        rows = db.execute(
            f"SELECT * FROM chat_history WHERE {where} AND id > ? ORDER BY id LIMIT ?",
            [*params, last_id, BATCH_LIMIT],
        ).fetchall()
        if not rows:
            break
        digest.update(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode())
        count += len(rows)
        message_bytes += sum(len((row["message"] or "").encode()) for row in rows)
        last_id = rows[-1]["id"]
    return {
        "count": count,
        "fingerprint": digest.hexdigest(),
        "max_id": last_id,
        "message_utf8_bytes": message_bytes,
    }


def _refresh_session(db, sid, rows=(), *, user_ids=None):
    """Refresh affected summaries without exceeding SQLite parameter limits.

    Args:
        db: Connection within the caller's transaction.
        sid: Session whose latest message and count must be rebuilt.
        rows: Small legacy batch used to identify affected users.
        user_ids: Optional complete set of affected users from a chunked operation.
    """
    # Production materialized user rankings must be rebuilt in this transaction.
    try:
        from .db_config import rebuild_user_stats
    except ImportError:
        from db_config import rebuild_user_stats
    if user_ids is None:
        records = [json.loads(r["row_json"]) if "row_json" in r else r for r in rows]
        user_ids = {r.get("user_id") for r in records}
    users = list(user_ids)
    for start in range(0, len(users), BATCH_LIMIT):
        rebuild_user_stats(db, users[start : start + BATCH_LIMIT])
    where, params = session_scope(sid)
    db.execute("DELETE FROM session_stats WHERE session_id = ?", [sid])
    db.execute(
        f"""INSERT INTO session_stats
        (session_id,message_type,session_name,last_msg,sender_name,last_time,last_message_id,message_count)
        SELECT ?,COALESCE(message_type,'legacy'),session_name,message,sender_name,timestamp,id,
               (SELECT COUNT(*) FROM chat_history WHERE {where})
        FROM chat_history WHERE {where} ORDER BY id DESC LIMIT 1""",
        [sid, *params, *params],
    )


class ArchiveManager:
    def __init__(self, connection_factory, cache_dir):
        self.connection_factory = connection_factory
        self.cache_dir = Path(cache_dir)
        self._previews = {}
        self._lock = threading.Lock()

    def _select(self, db, session_id, message_id=0, before_ts=0, start_ts=0, end_ts=0):
        where, params = session_scope(
            session_id, message_id, before_ts, start_ts, end_ts
        )
        total = db.execute(
            f"SELECT COUNT(*) AS count FROM chat_history WHERE {where}", params
        ).fetchone()["count"]
        rows = db.execute(
            f"SELECT * FROM chat_history WHERE {where} ORDER BY id LIMIT ?",
            [*params, BATCH_LIMIT],
        ).fetchall()
        return rows, total

    def preview(
        self, principal, session_id, message_id=0, before_ts=0, start_ts=0, end_ts=0
    ):
        """Snapshot all messages in the range for one explicit confirmation."""
        where, params = session_scope(
            session_id, message_id, before_ts, start_ts, end_ts
        )
        with self.connection_factory() as db:
            db.execute("BEGIN")
            snapshot = _scope_snapshot(db, where, params)
            db.rollback()
        total = snapshot["count"]
        if not total:
            return {
                "preview_token": None,
                "session_id": str(session_id).strip(),
                "count": 0,
                "matched_count": 0,
                "remaining_count": 0,
                "expires_in": PREVIEW_TTL,
                "attachments_preserved": True,
            }
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._previews = {
                key: value
                for key, value in self._previews.items()
                if value["expires"] > now
            }
            if len(self._previews) >= 128:
                raise ManagementConflict("预览过多，请稍后重试")
            self._previews[token] = {
                "principal": principal,
                "expires": now + PREVIEW_TTL,
                "session_id": str(session_id).strip(),
                "fingerprint": snapshot["fingerprint"],
                "message_id": message_id,
                "before_ts": before_ts,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "max_id": snapshot["max_id"],
                "matched_count": total,
            }
        return {
            "preview_token": token,
            "session_id": str(session_id).strip(),
            "count": total,
            "matched_count": total,
            "remaining_count": 0,
            "message_utf8_bytes": snapshot["message_utf8_bytes"],
            "attachments_preserved": True,
            "expires_in": PREVIEW_TTL,
        }

    def _remove(self, db, rows, session_id, reason, recoverable=True):
        operation = secrets.token_urlsafe(24)
        db.execute(
            "INSERT INTO archive_management_operations VALUES (?,?,?,?,?)",
            [operation, session_id, int(time.time()), reason, len(rows)],
        )
        for row in rows:
            if recoverable:
                db.execute(
                    "INSERT INTO archive_trash VALUES (?,?,?,?)",
                    [
                        row["id"],
                        operation,
                        session_id,
                        json.dumps(row, ensure_ascii=False),
                    ],
                )
            db.execute("DELETE FROM chat_history WHERE id = ?", [row["id"]])
        _refresh_session(db, session_id, rows)
        return {
            "operation_id": operation,
            "count": len(rows),
            "recoverable": recoverable,
            "attachments_preserved": True,
        }

    def export(self, principal, preview_token):
        """Write every matching message to a temporary JSON download in bounded chunks.

        Args:
            principal: Authenticated owner of the preview.
            preview_token: Unexpired preview binding the session and time range.

        Returns:
            A rewound binary temporary file. The response owns closing it.
        """
        with self._lock:
            preview = self._previews.get(preview_token)
            if (
                not preview
                or preview["expires"] <= time.time()
                or preview["principal"] != principal
            ):
                raise ManagementConflict("预览已失效，请重新预览")
        backup = tempfile.TemporaryFile(mode="w+b")
        try:
            with self.connection_factory() as db:
                db.execute("BEGIN")
                where, params = session_scope(
                    preview["session_id"],
                    preview["message_id"],
                    preview["before_ts"],
                    preview["start_ts"],
                    preview["end_ts"],
                )
                where += " AND id <= ?"
                params.append(preview["max_id"])
                snapshot = _scope_snapshot(db, where, params)
                total = snapshot["count"]
                if (
                    total != preview["matched_count"]
                    or snapshot["fingerprint"] != preview["fingerprint"]
                ):
                    raise ManagementConflict("消息范围已变化，请重新预览")
                metadata = {
                    "format": "chat-archive-message-backup-v1",
                    "session_id": preview["session_id"],
                    "exported_at": int(time.time()),
                    "start_ts": preview["start_ts"],
                    "end_ts": preview["end_ts"],
                    "message_count": total,
                    "attachments_included": False,
                }
                backup.write(
                    (
                        json.dumps(metadata, ensure_ascii=False)[:-1] + ',"messages":['
                    ).encode()
                )
                last_id = 0
                first = True
                while True:
                    rows = db.execute(
                        f"SELECT * FROM chat_history WHERE {where} AND id > ? ORDER BY id LIMIT 1000",
                        [*params, last_id],
                    ).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        if not first:
                            backup.write(b",")
                        backup.write(
                            json.dumps(
                                row, ensure_ascii=False, separators=(",", ":")
                            ).encode()
                        )
                        first = False
                    last_id = rows[-1]["id"]
                backup.write(b"]}")
                db.rollback()
            backup.seek(0)
            return backup
        except BaseException:
            backup.close()
            raise

    def delete(
        self,
        principal,
        preview_token,
        confirm_session_id,
        delete_mode="trash",
        confirm_permanent="",
        confirm_count=0,
    ):
        """Delete the entire confirmed snapshot atomically using bounded chunks.

        Args:
            principal: Authenticated owner of the preview.
            preview_token: One-use snapshot token.
            confirm_session_id: Session named in the confirmation dialog.
            delete_mode: Move to trash or permanently delete.
            confirm_permanent: Explicit confirmation text for permanent deletion.
            confirm_count: Exact total shown and accepted in the dialog.

        Returns:
            Operation identifier, total count and recoverability.
        """
        if delete_mode not in {"trash", "permanent"}:
            raise ValueError("删除方式无效")
        if delete_mode == "permanent" and confirm_permanent != "永久删除":
            raise ValueError("永久删除需要明确的二次确认")
        with self._lock:
            preview = self._previews.get(preview_token)
            if (
                not preview
                or preview["expires"] <= time.time()
                or preview["principal"] != principal
            ):
                raise ManagementConflict("预览已失效，请重新预览")
            if confirm_session_id != preview["session_id"]:
                raise ValueError("确认的会话 ID 不匹配")
            if (
                type(confirm_count) is not int
                or confirm_count != preview["matched_count"]
            ):
                raise ValueError("请确认完整消息数量后重试")
            # Consume before acquiring a database lock: a confirmation cannot be replayed.
            del self._previews[preview_token]
        with self.connection_factory() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                where, params = session_scope(
                    preview["session_id"],
                    preview["message_id"],
                    preview["before_ts"],
                    preview["start_ts"],
                    preview["end_ts"],
                )
                where += " AND id <= ?"
                params.append(preview["max_id"])
                snapshot = _scope_snapshot(db, where, params)
                if (
                    snapshot["count"] != confirm_count
                    or snapshot["fingerprint"] != preview["fingerprint"]
                ):
                    raise ManagementConflict("消息范围已变化，请重新预览")
                operation = secrets.token_urlsafe(24)
                recoverable = delete_mode == "trash"
                db.execute(
                    "INSERT INTO archive_management_operations VALUES (?,?,?,?,?)",
                    [
                        operation,
                        preview["session_id"],
                        int(time.time()),
                        "manual" if recoverable else "permanent",
                        confirm_count,
                    ],
                )
                last_id = removed = 0
                users = set()
                while True:
                    rows = db.execute(
                        f"SELECT * FROM chat_history WHERE {where} AND id > ? ORDER BY id LIMIT ?",
                        [*params, last_id, BATCH_LIMIT],
                    ).fetchall()
                    if not rows:
                        break
                    ids = [row["id"] for row in rows]
                    users.update(row.get("user_id") for row in rows)
                    if recoverable:
                        db.executemany(
                            "INSERT INTO archive_trash VALUES (?,?,?,?)",
                            [
                                (
                                    row["id"],
                                    operation,
                                    preview["session_id"],
                                    json.dumps(row, ensure_ascii=False),
                                )
                                for row in rows
                            ],
                        )
                    placeholders = ",".join("?" for _ in ids)
                    db.execute(
                        f"DELETE FROM chat_history WHERE id IN ({placeholders})", ids
                    )
                    if db.rowcount != len(ids):
                        raise ManagementConflict("消息数量已变化，操作已回滚")
                    removed += len(ids)
                    last_id = ids[-1]
                if removed != confirm_count:
                    raise ManagementConflict("消息数量已变化，操作已回滚")
                _refresh_session(db, preview["session_id"], user_ids=users)
                result = {
                    "operation_id": operation,
                    "count": removed,
                    "recoverable": recoverable,
                    "attachments_preserved": True,
                }
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise

    def trash(self, session_id):
        session_scope(session_id)
        with self.connection_factory() as db:
            return db.execute(
                """SELECT o.operation_id,o.created_at,o.reason,COUNT(t.original_id) AS count
                FROM archive_management_operations o JOIN archive_trash t USING(operation_id)
                WHERE t.session_id = ? GROUP BY o.operation_id ORDER BY o.created_at DESC LIMIT 50""",
                [session_id],
            ).fetchall()

    def restore(self, operation_id, session_id):
        """Restore all chunks of one operation, rolling everything back on conflict.

        Args:
            operation_id: Recoverable deletion operation to restore.
            session_id: Exact session owning the operation.

        Returns:
            Restored message count and success flag.
        """
        session_scope(session_id)
        with self.connection_factory() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                expected = db.execute(
                    "SELECT COUNT(*) AS count FROM archive_trash WHERE operation_id = ? AND session_id = ?",
                    [operation_id, session_id],
                ).fetchone()["count"]
                if not expected:
                    raise ManagementConflict("找不到可恢复的记录")
                columns = [
                    info["name"]
                    for info in db.execute("PRAGMA table_info(chat_history)").fetchall()
                ]
                quoted_columns = ",".join(
                    '"' + name.replace('"', '""') + '"' for name in columns
                )
                last_id = restored = 0
                users = set()
                while True:
                    items = db.execute(
                        "SELECT original_id,row_json FROM archive_trash WHERE operation_id = ? AND session_id = ? AND original_id > ? ORDER BY original_id LIMIT ?",
                        [operation_id, session_id, last_id, BATCH_LIMIT],
                    ).fetchall()
                    if not items:
                        break
                    rows = [json.loads(item["row_json"]) for item in items]
                    for item, row in zip(items, rows):
                        if set(row) != set(columns):
                            raise ManagementConflict(
                                "数据库字段已变化，请先备份并人工核对"
                            )
                        if (
                            row["id"] != item["original_id"]
                            or (row.get("session_id") or "legacy:archive") != session_id
                        ):
                            raise ManagementConflict("回收站消息范围不匹配，操作已回滚")
                    ids = [row["id"] for row in rows]
                    placeholders = ",".join("?" for _ in ids)
                    if db.execute(
                        f"SELECT id FROM chat_history WHERE id IN ({placeholders}) LIMIT 1",
                        ids,
                    ).fetchone():
                        raise ManagementConflict("原消息 ID 已被占用，未覆盖现有消息")
                    db.executemany(
                        f"INSERT INTO chat_history ({quoted_columns}) VALUES ({','.join('?' for _ in columns)})",
                        [[row[col] for col in columns] for row in rows],
                    )
                    users.update(row.get("user_id") for row in rows)
                    restored += len(rows)
                    last_id = items[-1]["original_id"]
                if restored != expected:
                    raise ManagementConflict("回收站消息数量已变化，操作已回滚")
                db.execute(
                    "DELETE FROM archive_trash WHERE operation_id = ? AND session_id = ?",
                    [operation_id, session_id],
                )
                _refresh_session(db, session_id, user_ids=users)
                db.commit()
                return {"count": restored, "restored": True}
            except Exception:
                db.rollback()
                raise

    def storage(self, session_id):
        where, params = session_scope(session_id)
        with self.connection_factory() as db:
            active = db.execute(
                f"SELECT COUNT(*) AS count,COALESCE(SUM(LENGTH(CAST(message AS BLOB))),0) AS message_utf8_bytes FROM chat_history WHERE {where}",
                params,
            ).fetchone()
            trash = db.execute(
                "SELECT COUNT(*) AS count,COALESCE(SUM(LENGTH(CAST(row_json AS BLOB))),0) AS payload_bytes FROM archive_trash WHERE session_id = ?",
                [session_id],
            ).fetchone()
            page_size = next(iter(db.execute("PRAGMA page_size").fetchone().values()))
            pages = next(iter(db.execute("PRAGMA page_count").fetchone().values()))
            free = next(iter(db.execute("PRAGMA freelist_count").fetchone().values()))
        return {
            "active": active,
            "trash": trash,
            "database_allocated_bytes": page_size * pages,
            "database_free_page_bytes": page_size * free,
            "attachments_preserved": True,
        }

    def apply_retention(
        self,
        days,
        session_ids,
        now=None,
        global_scope=False,
        excluded_session_ids=None,
        should_stop=None,
    ):
        # Default disabled; global scope must be explicitly true to include every session.
        if (
            type(days) is not int
            or days <= 0
            or days > 36500
            or not isinstance(session_ids, list)
            or (not session_ids and global_scope is not True)
        ):
            return []
        excluded_session_ids = excluded_session_ids or []
        if not isinstance(excluded_session_ids, list):
            raise ValueError("排除会话必须是列表")
        for sid in [*session_ids, *excluded_session_ids]:
            if not isinstance(sid, str):
                raise ValueError("保留策略必须使用完整会话 ID 列表")
            session_scope(sid)
        selected = {sid.strip() for sid in session_ids}
        if global_scope is True:
            with self.connection_factory() as db:
                all_ids = {
                    row["sid"]
                    for row in db.execute(
                        "SELECT DISTINCT COALESCE(NULLIF(session_id,''),'legacy:archive') AS sid FROM chat_history"
                    ).fetchall()
                }
            selected = selected & all_ids if selected else all_ids
        selected -= {sid.strip() for sid in excluded_session_ids}
        cutoff = int(time.time() if now is None else now) - days * 86400
        if cutoff <= 0:
            return []
        results = []
        for sid in sorted(selected):
            if should_stop is not None and should_stop():
                break
            with self.connection_factory() as db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    rows, _ = self._select(db, sid, before_ts=cutoff)
                    if rows:
                        results.append(self._remove(db, rows, sid, "retention"))
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
        return results
