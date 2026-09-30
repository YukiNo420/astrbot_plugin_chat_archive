"""Recoverable message management; cached attachments are never removed here."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import re
import secrets
import stat
import threading
import time

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


def session_scope(session_id):
    sid = str(session_id or "").strip()
    if not sid or len(sid) > 256:
        raise ValueError("必须指定完整会话 ID")
    if sid == "legacy:archive":
        return "COALESCE(NULLIF(session_id, ''), 'legacy:archive') = ?", [sid]
    return "session_id = ?", [sid]


def _fingerprint(rows):
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _cache_names(message):
    names = set()
    for marker in re.findall(r"\[CQ:(?:image|video|record),([^\]]+)\]", message or ""):
        match = re.search(r"(?:^|,)url=([^,]+)", marker)
        if match:
            url = html.unescape(match.group(1))
            local = re.fullmatch(r"/static/cache/([A-Za-z0-9_.-]+)", url)
            if local and local.group(1) not in {".", ".."}:
                names.add(local.group(1))
    return names


def _refresh_session(db, sid):
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

    def _select(self, db, session_id, message_id=0, before_ts=0):
        where, params = session_scope(session_id)
        if message_id:
            if message_id < 1:
                raise ValueError("消息 ID 无效")
            where += " AND id = ?"
            params.append(message_id)
        if before_ts:
            if before_ts < 1:
                raise ValueError("截止时间无效")
            where += " AND timestamp < ?"
            params.append(before_ts)
        total = db.execute(
            f"SELECT COUNT(*) AS count FROM chat_history WHERE {where}", params
        ).fetchone()["count"]
        rows = db.execute(
            f"SELECT * FROM chat_history WHERE {where} ORDER BY id LIMIT ?",
            [*params, BATCH_LIMIT],
        ).fetchall()
        return rows, total

    def preview(self, principal, session_id, message_id=0, before_ts=0):
        with self.connection_factory() as db:
            rows, total = self._select(db, session_id, message_id, before_ts)
            if not rows:
                raise ValueError("没有符合条件的消息")
            names = set().union(*(_cache_names(row["message"]) for row in rows))
            selected_ids = {row["id"] for row in rows}
            shared = set()
            for name in names:
                candidates = db.execute(
                    "SELECT id,message FROM chat_history WHERE message LIKE ? ESCAPE '\\'",
                    ["%/static/cache/" + name.replace("_", "\\_") + "%"],
                ).fetchall()
                if any(
                    row["id"] not in selected_ids
                    and name in _cache_names(row["message"])
                    for row in candidates
                ):
                    shared.add(name)
                if name not in shared:
                    trashed = db.execute(
                        "SELECT row_json FROM archive_trash WHERE row_json LIKE ? ESCAPE '\\'",
                        ["%/static/cache/" + name.replace("_", "\\_") + "%"],
                    ).fetchall()
                    if any(
                        name
                        in _cache_names(json.loads(item["row_json"]).get("message"))
                        for item in trashed
                    ):
                        shared.add(name)
            cached_bytes = 0
            for name in names:
                file = self.cache_dir / name
                try:
                    info = file.lstat()
                    if stat.S_ISREG(info.st_mode):
                        cached_bytes += info.st_size
                except OSError:
                    pass
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
                "ids": [row["id"] for row in rows],
                "fingerprint": _fingerprint(rows),
            }
        return {
            "preview_token": token,
            "session_id": str(session_id).strip(),
            "count": len(rows),
            "matched_count": total,
            "remaining_count": total - len(rows),
            "message_utf8_bytes": sum(
                len((row["message"] or "").encode()) for row in rows
            ),
            "cached_attachment_bytes": cached_bytes,
            "cached_attachment_count": len(names),
            "shared_attachment_count": len(shared),
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
        _refresh_session(db, session_id)
        return {
            "operation_id": operation,
            "count": len(rows),
            "recoverable": recoverable,
            "attachments_preserved": True,
        }

    def export(self, principal, preview_token):
        with self._lock:
            preview = self._previews.get(preview_token)
            if (
                not preview
                or preview["expires"] <= time.time()
                or preview["principal"] != principal
            ):
                raise ManagementConflict("预览已失效，请重新预览")
        with self.connection_factory() as db:
            placeholders = ",".join("?" for _ in preview["ids"])
            rows = db.execute(
                f"SELECT * FROM chat_history WHERE id IN ({placeholders}) ORDER BY id",
                preview["ids"],
            ).fetchall()
            if _fingerprint(rows) != preview["fingerprint"]:
                raise ManagementConflict("消息在预览后发生变化，请重新预览")
        return {
            "format": "chat-archive-message-backup-v1",
            "session_id": preview["session_id"],
            "exported_at": int(time.time()),
            "messages": rows,
            "attachments_included": False,
        }

    def delete(
        self,
        principal,
        preview_token,
        confirm_session_id,
        delete_mode="trash",
        confirm_permanent="",
    ):
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
            # Consume before acquiring a database lock: a confirmation cannot be replayed.
            del self._previews[preview_token]
        with self.connection_factory() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" for _ in preview["ids"])
                rows = db.execute(
                    f"SELECT * FROM chat_history WHERE id IN ({placeholders}) ORDER BY id",
                    preview["ids"],
                ).fetchall()
                if _fingerprint(rows) != preview["fingerprint"]:
                    raise ManagementConflict("消息在预览后发生变化，请重新预览")
                result = self._remove(
                    db,
                    rows,
                    preview["session_id"],
                    "manual" if delete_mode == "trash" else "permanent",
                    delete_mode == "trash",
                )
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
        session_scope(session_id)
        with self.connection_factory() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute(
                    "SELECT row_json FROM archive_trash WHERE operation_id = ? AND session_id = ? ORDER BY original_id",
                    [operation_id, session_id],
                ).fetchall()
                if not rows:
                    raise ManagementConflict("找不到可恢复的记录")
                if len(rows) > BATCH_LIMIT:
                    raise ManagementConflict("恢复批次过大，请先备份并人工核对")
                columns = [
                    info["name"]
                    for info in db.execute("PRAGMA table_info(chat_history)").fetchall()
                ]
                quoted_columns = ",".join(
                    '"' + name.replace('"', '""') + '"' for name in columns
                )
                for item in rows:
                    row = json.loads(item["row_json"])
                    if set(row) != set(columns):
                        raise ManagementConflict("数据库字段已变化，请先备份并人工核对")
                    if db.execute(
                        "SELECT id FROM chat_history WHERE id = ?", [row["id"]]
                    ).fetchone():
                        raise ManagementConflict("原消息 ID 已被占用，未覆盖现有消息")
                    db.execute(
                        f"INSERT INTO chat_history ({quoted_columns}) VALUES ({','.join('?' for _ in columns)})",
                        [row[col] for col in columns],
                    )
                db.execute(
                    "DELETE FROM archive_trash WHERE operation_id = ? AND session_id = ?",
                    [operation_id, session_id],
                )
                _refresh_session(db, session_id)
                db.commit()
                return {"count": len(rows), "restored": True}
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
