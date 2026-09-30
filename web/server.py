from __future__ import annotations

import asyncio
import datetime
import json
import secrets
import os
import time
import threading
import ipaddress
import socket
from contextlib import suppress
from functools import lru_cache
from urllib.parse import urlparse
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

try:
    from ..db_config import get_db_connection, init_db
except ImportError:
    try:
        import sys
        sys.path.append(str(Path(__file__).resolve().parent.parent))
        from db_config import get_db_connection, init_db
    except Exception:
        raise RuntimeError("无法加载 db_config 模块。如果是独立解耦运行，请使用 'python -m astrbot_plugin_chat_archive.web.server' 从 AstrBot 的 plugins 目录执行。")

from astrbot.api import logger

try:
    from ..config import (
        get_config_path,
        get_data_dir,
        load_allowed_media_domains,
        load_api_key,
        load_media_max_bytes,
        load_allow_fake_ip,
    )
except ImportError:
    try:
        import sys

        sys.path.append(str(Path(__file__).resolve().parent.parent))
        from config import (
            get_config_path,
            get_data_dir,
            load_allowed_media_domains,
            load_api_key,
            load_media_max_bytes,
            load_allow_fake_ip,
        )
    except Exception:
        raise RuntimeError("无法加载 config 模块。")

API_KEY = load_api_key()


def _hostname_matches_allowlist(hostname: str, domains: frozenset[str]) -> bool:
    host = (hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _ip_is_public(ip: ipaddress._BaseAddress, allow_fake_ip: bool = False) -> bool:
    if allow_fake_ip and isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.IPv4Network("198.18.0.0/15"):
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _hostname_resolves_to_public_ips(hostname: str) -> bool:
    """Block localhost/private/link-local destinations before proxying media."""
    allow_fake_ip = load_allow_fake_ip()
    try:
        try:
            ip = ipaddress.ip_address(hostname)
            return _ip_is_public(ip, allow_fake_ip=allow_fake_ip)
        except ValueError:
            pass

        infos = await asyncio.to_thread(
            socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM
        )
        if not infos:
            return False
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not _ip_is_public(ip, allow_fake_ip=allow_fake_ip):
                logger.warning(f"Media proxy rejected non-public DNS target {hostname} -> {ip}")
                return False
        return True
    except Exception as e:
        logger.warning(f"Media proxy DNS safety check failed for {hostname}: {e}")
        return False


MEDIA_MAX_BYTES = load_media_max_bytes()
ALLOWED_MEDIA_DOMAINS = load_allowed_media_domains()

@lru_cache(maxsize=1)
def load_right_align_ids():
    """加载管理员 ID 列表用于 WebUI 中的消息右对齐显示。

    注意: 使用 @lru_cache 缓存，管理员列表变更后需要重启服务才能生效。
    """
    right_align_ids = {"astrbot", "bot", "99999"}
    try:
        data_dir = get_data_dir()
        config_dir = data_dir.parent.parent / "config"
        if not config_dir.exists():
            config_dir = Path(__file__).resolve().parent.parent.parent.parent / "config"
        if config_dir.exists():
            for f in config_dir.glob("abconf_*.json"):
                with open(f, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                    if "admins_id" in data:
                        for admin in data["admins_id"]:
                            admin_str = str(admin).strip()
                            right_align_ids.add(admin_str)
                            if admin_str.startswith("UID: "):
                                right_align_ids.add(admin_str.replace("UID: ", ""))
    except Exception as e:
        logger.error(f"加载管理员列表失败: {e}")
    return frozenset(right_align_ids)

app = FastAPI(title="Chat Archive Admin Panel")


@app.on_event("startup")
async def startup_init_db():
    """Ensure standalone WebUI runs with the latest database schema."""
    await asyncio.to_thread(init_db)

current_dir = Path(__file__).resolve().parent
static_dir = current_dir / "static"
templates_dir = current_dir / "templates"
cache_static_dir = get_data_dir() / "web_cache"

cors_origins_env = os.environ.get("ARCHIVE_CORS_ORIGINS", os.environ.get("CORS_ORIGINS", "http://localhost:8090"))
cors_origins = [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)

# Session token store: maps random token -> expiry timestamp.
# Tokens are issued on login and validated in auth middleware,
# so the raw API Key is never stored in cookies.
_SESSION_TOKENS: dict[str, float] = {}
_SESSION_TOKENS_LOCK = threading.Lock()
_SESSION_TOKEN_MAX_AGE = 24 * 3600  # 24 hours


def _create_session_token() -> str:
    token = secrets.token_urlsafe(32)
    expiry = time.time() + _SESSION_TOKEN_MAX_AGE
    with _SESSION_TOKENS_LOCK:
        # Prune expired tokens to prevent unbounded growth
        now = time.time()
        expired = [k for k, v in _SESSION_TOKENS.items() if v < now]
        for k in expired:
            del _SESSION_TOKENS[k]
        _SESSION_TOKENS[token] = expiry
    return token


def _validate_session_token(token: str) -> bool:
    if not token:
        return False
    with _SESSION_TOKENS_LOCK:
        expiry = _SESSION_TOKENS.get(token)
        if expiry is None:
            return False
        if time.time() > expiry:
            del _SESSION_TOKENS[token]
            return False
        return True


def _revoke_session_token(token: str) -> None:
    if not token:
        return
    with _SESSION_TOKENS_LOCK:
        _SESSION_TOKENS.pop(token, None)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    is_public_static = path.startswith("/static/") and not path.startswith("/static/cache/")
    if not API_KEY:
        # 如果 API_KEY 为空，仅允许访问登录页、验证接口和非缓存静态资源。
        if path not in ["/", "/api/auth/verify"] and not is_public_static:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "API Key is not configured. Access is disabled for security reasons."}
            )
        return await call_next(request)

    is_public = path in ["/", "/api/auth/verify"] or is_public_static

    if is_public:
        return await call_next(request)

    # Support API Key via header (direct key) or HttpOnly cookie (session token).
    # Never accept API keys in URLs.
    header_key = request.headers.get("X-API-Key", "")
    cookie_token = request.cookies.get("archive_auth", "")

    authenticated = False
    if header_key and secrets.compare_digest(header_key, API_KEY):
        authenticated = True
    elif cookie_token and _validate_session_token(cookie_token):
        authenticated = True

    if not authenticated:
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized", "message": "Invalid API Key"},
        )
    return await call_next(request)

# Cache media is mounted separately and remains protected by auth middleware.
app.mount("/static/cache", StaticFiles(directory=str(cache_static_dir), check_dir=False), name="cache")
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(templates_dir))

_DASHBOARD_CACHE: dict[str, tuple[float, dict]] = {}
_DASHBOARD_CACHE_LOCK = threading.Lock()
_STATS_CACHE: dict[str, tuple[float, dict]] = {}
_STATS_CACHE_LOCK = threading.Lock()


def _load_dashboard_cache_ttl() -> int:
    try:
        ttl = int(os.environ.get("ARCHIVE_DASHBOARD_CACHE_TTL", "30") or "30")
    except (TypeError, ValueError):
        ttl = 30
    return max(5, min(ttl, 300))


_DASHBOARD_CACHE_TTL = _load_dashboard_cache_ttl()


def _load_stats_cache_ttl() -> int:
    try:
        ttl = int(os.environ.get("ARCHIVE_STATS_CACHE_TTL", "15") or "15")
    except (TypeError, ValueError):
        ttl = 15
    return max(0, min(ttl, 300))


def _load_history_message_max_chars() -> int:
    try:
        value = int(os.environ.get("ARCHIVE_HISTORY_MESSAGE_MAX_CHARS", "12000") or "12000")
    except (TypeError, ValueError):
        value = 12000
    return max(512, min(value, 200000))


_STATS_CACHE_TTL = _load_stats_cache_ttl()
_HISTORY_MESSAGE_MAX_CHARS = _load_history_message_max_chars()


_ALLOWED_PRAGMA_TABLES = frozenset({"chat_history", "session_stats"})


def _table_has_columns(db, table: str, columns: tuple[str, ...]) -> bool:
    if table not in _ALLOWED_PRAGMA_TABLES:
        return False
    try:
        rows = db.execute(f"PRAGMA table_info({table});").fetchall()
        existing = {row["name"] for row in rows}
        return all(col in existing for col in columns)
    except Exception:
        return False


def _where_clause(conditions: list[str]) -> str:
    return " WHERE " + " AND ".join(conditions)


def _fetch_latest_sender_profiles(
    db,
    user_ids: list[str],
    conditions: list[str],
    params: list,
) -> dict[str, dict]:
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    has_avatar = _table_has_columns(db, "chat_history", ("avatar_url",))
    has_platform = _table_has_columns(db, "chat_history", ("platform_name",))
    avatar_expr = "avatar_url" if has_avatar else "''"
    platform_expr = "platform_name" if has_platform else "''"
    scoped_conditions = [
        condition for condition in conditions
        if condition and condition != "1=1"
    ]
    name_conditions = [
        f"user_id IN ({placeholders})",
        *scoped_conditions,
        (
            "(sender_name IS NOT NULL AND sender_name != '')"
            if not has_avatar
            else "((sender_name IS NOT NULL AND sender_name != '') OR (avatar_url IS NOT NULL AND avatar_url != ''))"
        ),
    ]
    name_sql = f"""
        SELECT user_id, sender_name, avatar_url, platform_name
        FROM (
            SELECT user_id,
                   sender_name,
                   {avatar_expr} as avatar_url,
                   {platform_expr} as platform_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY user_id
                       ORDER BY
                           CASE WHEN {avatar_expr} IS NOT NULL AND {avatar_expr} != '' THEN 0 ELSE 1 END,
                           timestamp DESC,
                           id DESC
                   ) as rn
            FROM chat_history {_where_clause(name_conditions)}
        ) ranked
        WHERE rn = 1
    """
    rows = db.execute(name_sql, [*user_ids, *params]).fetchall()
    return {
        str(row["user_id"]): {
            "sender_name": str(row["sender_name"] or ""),
            "avatar_url": str(row["avatar_url"] or ""),
            "platform_name": str(row["platform_name"] or ""),
        }
        for row in rows
    }


def _fetch_user_counts(
    db,
    conditions: list[str],
    params: list,
    limit: int,
    offset: int = 0,
) -> list[dict]:
    rows = db.execute(
        f"""
            SELECT user_id, COUNT(*) as cnt
            FROM chat_history {_where_clause(conditions)}
            GROUP BY user_id
            ORDER BY cnt DESC, user_id ASC
            LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    if not rows:
        return []

    user_ids = [str(row["user_id"]) for row in rows]
    latest_profiles = _fetch_latest_sender_profiles(db, user_ids, conditions, params)
    return [
        {
            "user_id": str(row["user_id"]),
            "sender_name": latest_profiles.get(str(row["user_id"]), {}).get("sender_name") or str(row["user_id"]),
            "avatar_url": latest_profiles.get(str(row["user_id"]), {}).get("avatar_url", ""),
            "platform_name": latest_profiles.get(str(row["user_id"]), {}).get("platform_name", ""),
            "cnt": int(row["cnt"] or 0),
        }
        for row in rows
    ]


def _fetch_latest_session_profiles(db, session_ids: list[str], bot_ids: list[str] = None) -> dict[str, dict]:
    if not session_ids:
        return {}
    has_avatar = _table_has_columns(db, "chat_history", ("avatar_url",))
    has_platform = _table_has_columns(db, "chat_history", ("platform_name",))
    has_guild_avatar = _table_has_columns(db, "chat_history", ("guild_avatar_url",))
    if not has_avatar and not has_platform:
        return {}

    if not bot_ids:
        bot_ids = ["bot", "99999", "astrbot"]

    placeholders = ",".join("?" for _ in session_ids)
    bot_placeholders = ",".join("?" for _ in bot_ids)

    guild_priority_expr = ""
    if has_guild_avatar:
        guild_priority_expr = "CASE WHEN message_type IN ('channel', 'ChannelMessage', 'group', 'GroupMessage', 'server') AND guild_avatar_url IS NOT NULL AND guild_avatar_url != '' THEN 0 ELSE 1 END,"

    if has_guild_avatar:
        avatar_expr = "CASE WHEN platform_name IN ('discord', 'kook', 'teamspeak', 'telegram') AND message_type IN ('channel', 'ChannelMessage', 'group', 'GroupMessage', 'server') THEN COALESCE(NULLIF(guild_avatar_url, ''), avatar_url) ELSE avatar_url END"
    else:
        avatar_expr = "avatar_url" if has_avatar else "''"

    platform_column = "platform_name" if has_platform else "'' as platform_name"
    rows = db.execute(
        f"""
            SELECT session_id, avatar_url, platform_name
            FROM (
                SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive') as session_id,
                       {avatar_expr} as avatar_url,
                       {platform_column},
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(NULLIF(session_id, ''), 'legacy:archive')
                           ORDER BY
                               {guild_priority_expr}
                               CASE WHEN {avatar_expr} IS NOT NULL AND {avatar_expr} != '' THEN 0 ELSE 1 END,
                               timestamp DESC,
                               id DESC
                       ) as rn
                FROM chat_history
                WHERE COALESCE(NULLIF(session_id, ''), 'legacy:archive') IN ({placeholders})
                  AND user_id NOT IN ({bot_placeholders})
            ) ranked
            WHERE rn = 1
        """,
        [*session_ids, *bot_ids],
    ).fetchall()
    return {
        str(row["session_id"]): {
            "avatar_url": str(row["avatar_url"] or ""),
            "platform_name": str(row["platform_name"] or ""),
        }
        for row in rows
    }


async def _close_media_upstream(response: httpx.Response | None, client: httpx.AsyncClient | None):
    if response is not None:
        with suppress(Exception):
            await response.aclose()
    if client is not None:
        with suppress(Exception):
            await client.aclose()


def _session_display_name(session_id: str, message_type: str, session_name: str = "", sender_name: str = "") -> str:
    session_id = str(session_id or "legacy:archive")
    message_type = str(message_type or "legacy")
    if session_id == "legacy:archive":
        return "📦 历史记录 (未分类)"
    if session_name and str(session_name).strip():
        return str(session_name).strip()
    short_id = session_id.split(":")[-1] if ":" in session_id else session_id
    mt = message_type.lower()
    if "group" in mt:
        return f"群聊: {short_id}"
    if "channel" in mt:
        return f"频道: {short_id}"
    if "friend" in mt:
        return sender_name or short_id
    return session_id


def _message_preview(message: str, max_len: int = 120) -> str:
    text = str(message or "")
    for marker, label in (
        ("[CQ:image", "[图片"),
        ("[CQ:video", "[视频"),
        ("[CQ:record", "[语音"),
        ("[CQ:file", "[文件"),
    ):
        text = text.replace(marker, label)
    text = " ".join(text.split())
    return text[: max_len - 1] + "…" if len(text) > max_len else text


def _dashboard_range_days(range_key: str) -> tuple[str, int]:
    normalized = str(range_key or "30d").lower().strip()
    mapping = {"1d": 1, "7d": 7, "30d": 30}
    if normalized not in mapping:
        normalized = "30d"
    return normalized, mapping[normalized]


def _compute_dashboard(range_key: str, recent_limit: int) -> dict:
    db = None
    try:
        db = get_db_connection()
        has_media_columns = _table_has_columns(db, "chat_history", ("has_image", "has_video", "msg_kind"))
        if not has_media_columns:
            logger.warning("Dashboard media columns are missing; attempting one schema refresh before serving aggregates.")
            try:
                db.close()
                db = None
                init_db()
            except Exception as e:
                logger.warning(f"Dashboard schema refresh failed; media aggregates will be omitted: {e}")
            finally:
                if db is None:
                    db = get_db_connection()
            has_media_columns = _table_has_columns(db, "chat_history", ("has_image", "has_video", "msg_kind"))

        local_now = datetime.datetime.now().astimezone()
        local_today_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = int(local_today_midnight.timestamp())
        local_offset = int(local_now.utcoffset().total_seconds()) if local_now.utcoffset() else 0
        range_key, trend_days = _dashboard_range_days(range_key)
        trend_start_dt = local_today_midnight - datetime.timedelta(days=trend_days - 1)
        trend_start = int(trend_start_dt.timestamp())

        # Measure Summary Time
        t_summary_start = time.perf_counter()
        try:
            row = db.execute("SELECT COALESCE(SUM(message_count), 0) as c, COUNT(*) as sessions FROM session_stats").fetchone()
            total_messages = int(row["c"] or 0) if row else 0
            total_sessions = int(row["sessions"] or 0) if row else 0
        except Exception:
            total_messages = int(db.execute("SELECT COUNT(*) as c FROM chat_history").fetchone()["c"] or 0)
            total_sessions = int(db.execute("""
                SELECT COUNT(*) as c FROM (
                    SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive')
                    FROM chat_history
                    GROUP BY COALESCE(NULLIF(session_id, ''), 'legacy:archive')
                )
            """).fetchone()["c"] or 0)

        today_messages = int(db.execute(
            "SELECT COUNT(*) as c FROM chat_history WHERE timestamp >= ?",
            [today_start],
        ).fetchone()["c"] or 0)

        if has_media_columns:
            total_images = int(db.execute("SELECT COUNT(*) as c FROM chat_history WHERE has_image = 1").fetchone()["c"] or 0)
            total_videos = int(db.execute("SELECT COUNT(*) as c FROM chat_history WHERE has_video = 1").fetchone()["c"] or 0)
            type_rows = db.execute("""
                SELECT COALESCE(NULLIF(msg_kind, ''), 'text') as kind, COUNT(*) as cnt
                FROM chat_history
                GROUP BY msg_kind
            """).fetchall()
        else:
            logger.warning("Dashboard media columns unavailable; skipping expensive LIKE fallback scan.")
            total_images = 0
            total_videos = 0
            type_rows = [
                {"kind": "text", "cnt": total_messages},
            ]
        time_summary_ms = round((time.perf_counter() - t_summary_start) * 1000, 2)

        # Measure Type Distribution Time
        t_type_start = time.perf_counter()
        text_count = 0
        image_count = 0
        other_count = 0
        for r_row in type_rows:
            kind = str(r_row.get("kind", "text") if isinstance(r_row, dict) else r_row["kind"] or "text")
            count = int(r_row.get("cnt", 0) if isinstance(r_row, dict) else r_row["cnt"] or 0)
            if kind == "text":
                text_count += count
            elif kind == "image":
                image_count += count
            else:
                other_count += count

        message_type_distribution = []
        if text_count > 0:
            message_type_distribution.append({"type": "text", "name": "文本", "count": text_count})
        if image_count > 0:
            message_type_distribution.append({"type": "image", "name": "图片", "count": image_count})
        if other_count > 0:
            message_type_distribution.append({"type": "other", "name": "其他", "count": other_count})
        message_type_distribution.sort(key=lambda item: item["count"], reverse=True)
        time_type_ms = round((time.perf_counter() - t_type_start) * 1000, 2)

        # Measure Trend Time
        t_trend_start = time.perf_counter()
        if range_key == "1d":
            trend_start_dt = local_now - datetime.timedelta(hours=23)
            trend_start_dt = trend_start_dt.replace(minute=0, second=0, microsecond=0)
            trend_start = int(trend_start_dt.timestamp())

            trend_rows = db.execute(f"""
                SELECT CAST((timestamp + {local_offset}) / 3600 AS INTEGER) as hour_bucket, COUNT(*) as cnt
                FROM chat_history
                WHERE timestamp >= ?
                GROUP BY hour_bucket
                ORDER BY hour_bucket
            """, [trend_start]).fetchall()
            trend_map = {int(row["hour_bucket"]): int(row["cnt"] or 0) for row in trend_rows}
            activity_trend = []
            for idx in range(24):
                hour_dt = trend_start_dt + datetime.timedelta(hours=idx)
                bucket = int((int(hour_dt.timestamp()) + local_offset) / 3600)
                activity_trend.append({
                    "date": hour_dt.strftime("%m-%d %H:00"),
                    "count": trend_map.get(bucket, 0)
                })
        else:
            trend_rows = db.execute(f"""
                SELECT CAST((timestamp + {local_offset}) / 86400 AS INTEGER) as day_bucket, COUNT(*) as cnt
                FROM chat_history
                WHERE timestamp >= ?
                GROUP BY day_bucket
                ORDER BY day_bucket
            """, [trend_start]).fetchall()
            trend_map = {int(row["day_bucket"]): int(row["cnt"] or 0) for row in trend_rows}
            activity_trend = []
            for idx in range(trend_days):
                day_dt = trend_start_dt + datetime.timedelta(days=idx)
                bucket = int((int(day_dt.timestamp()) + local_offset) / 86400)
                activity_trend.append({"date": day_dt.strftime("%Y-%m-%d"), "count": trend_map.get(bucket, 0)})
        time_trend_ms = round((time.perf_counter() - t_trend_start) * 1000, 2)

        # Measure Top Groups Time
        t_groups_start = time.perf_counter()
        try:
            group_rows = db.execute("""
                SELECT session_id, session_name, message_type, message_count, last_time, last_msg, sender_name
                FROM session_stats
                WHERE message_type IN ('group', 'GroupMessage', 'channel', 'ChannelMessage') OR LOWER(COALESCE(message_type, '')) LIKE '%group%' OR LOWER(COALESCE(message_type, '')) LIKE '%channel%'
                ORDER BY message_count DESC, last_time DESC
                LIMIT 10
            """).fetchall()
        except Exception:
            group_rows = db.execute("""
                SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive') as session_id,
                       session_name,
                       COALESCE(message_type, 'legacy') as message_type,
                       COUNT(*) as message_count,
                       MAX(timestamp) as last_time,
                       '' as last_msg,
                       '' as sender_name
                FROM chat_history
                WHERE message_type IN ('group', 'GroupMessage', 'channel', 'ChannelMessage') OR LOWER(COALESCE(message_type, '')) LIKE '%group%' OR LOWER(COALESCE(message_type, '')) LIKE '%channel%'
                GROUP BY COALESCE(NULLIF(session_id, ''), 'legacy:archive')
                ORDER BY message_count DESC, last_time DESC
                LIMIT 10
            """).fetchall()
        top_groups = []
        for row in group_rows:
            sid = str(row["session_id"] or "legacy:archive")
            mt = str(row["message_type"] or "legacy")
            top_groups.append({
                "session_id": sid,
                "session_name": row["session_name"] or "",
                "name": _session_display_name(sid, mt, row["session_name"] or "", row["sender_name"] or ""),
                "message_type": mt,
                "message_count": int(row["message_count"] or 0),
                "last_time": int(row["last_time"] or 0),
                "last_msg": _message_preview(row["last_msg"] or "", 100),
            })
        time_groups_ms = round((time.perf_counter() - t_groups_start) * 1000, 2)

        # Measure Database Size
        try:
            page_count_row = db.execute("PRAGMA page_count;").fetchone()
            page_size_row = db.execute("PRAGMA page_size;").fetchone()

            if isinstance(page_count_row, dict):
                page_count = list(page_count_row.values())[0]
            else:
                page_count = page_count_row[0]

            if isinstance(page_size_row, dict):
                page_size = list(page_size_row.values())[0]
            else:
                page_size = page_size_row[0]

            db_size_bytes = int(page_count or 0) * int(page_size or 0)
            db_size_mb = round(db_size_bytes / (1024 * 1024), 2)
        except Exception as e:
            logger.error(f"Failed to measure database size: {e}")
            db_size_mb = 0.0

        performance = {
            "db_size_mb": db_size_mb,
            "time_summary_ms": time_summary_ms,
            "time_type_ms": time_type_ms,
            "time_trend_ms": time_trend_ms,
            "time_groups_ms": time_groups_ms,
            "total_db_time_ms": round(time_summary_ms + time_type_ms + time_trend_ms + time_groups_ms, 2),
            "cache_hit": False
        }

        return {
            "summary": {
                "total_messages": total_messages,
                "today_messages": today_messages,
                "total_sessions": total_sessions,
                "total_images": total_images,
                "total_videos": total_videos,
            },
            "activity_trend": activity_trend,
            "top_groups": top_groups,
            "message_type_distribution": message_type_distribution,
            "range": range_key,
            "performance": performance,
            "generated_at": int(time.time()),
            "cache_ttl": _DASHBOARD_CACHE_TTL,
        }
    finally:
        if db:
            db.close()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        return templates.TemplateResponse(
            request=request, name="index.html", context={"request": request}
        )
    except TypeError:
        return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/auth/verify")
async def verify_auth(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    provided_key = str(data.get("api_key", "")).strip()
    if not API_KEY:
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": "API Key is not configured"},
        )
    if provided_key and secrets.compare_digest(provided_key, API_KEY):
        session_token = _create_session_token()
        resp = JSONResponse(content={"success": True})
        resp.set_cookie(
            "archive_auth",
            session_token,
            max_age=_SESSION_TOKEN_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return resp
    return JSONResponse(status_code=401, content={"success": False, "message": "Invalid API Key"})

@app.post("/api/auth/logout")
async def logout_auth(request: Request):
    cookie_token = request.cookies.get("archive_auth", "")
    _revoke_session_token(cookie_token)
    resp = JSONResponse(content={"success": True})
    resp.delete_cookie(
        "archive_auth",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp

@app.get("/api/history")
def get_history(
    keyword: str = Query("", max_length=200),
    user_id: str = Query("", max_length=128),
    session_id: str = Query("", max_length=256),
    time_start: int = Query(0, ge=0),
    time_end: int = Query(0, ge=0),
    page: int = Query(1, ge=1, le=100000),
    limit: int = Query(50, ge=1, le=200),
    cursor: int = Query(0, ge=0),
    include_total: bool = Query(False),
    full_message: bool = Query(False),
):
    db = None
    try:
        db = get_db_connection()

        conditions = ["1=1"]
        params = []

        if keyword:
            conditions.append("message LIKE ? ESCAPE '\\'")
            safe_keyword = (
                keyword.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            params.append(f"%{safe_keyword}%")
        if user_id:
            conditions.append("user_id = ?")
            params.append(str(user_id))
        if session_id == "legacy:archive":
            conditions.append(
                "(session_id IS NULL OR session_id = '' OR session_id = 'legacy:archive')"
            )
        elif session_id:
            conditions.append("session_id = ?")
            params.append(str(session_id))
        if time_start:
            conditions.append("timestamp >= ?")
            params.append(time_start)
        if time_end:
            conditions.append("timestamp <= ?")
            params.append(time_end)

        where_cl = " WHERE " + " AND ".join(conditions)
        has_avatar = _table_has_columns(db, "chat_history", ("avatar_url",))
        has_platform = _table_has_columns(db, "chat_history", ("platform_id", "platform_name"))
        avatar_select = "avatar_url" if has_avatar else "'' as avatar_url"
        platform_select = (
            "platform_id, platform_name"
            if has_platform
            else "'' as platform_id, '' as platform_name"
        )
        if full_message:
            select_columns = """
                id, user_id, sender_name, message,
                COALESCE(LENGTH(message), 0) as message_length,
                0 as message_truncated,
                timestamp, session_id, message_type, session_name, msg_id, is_recalled,
                {avatar_select}, {platform_select}
            """.format(avatar_select=avatar_select, platform_select=platform_select)
            select_params = []
        else:
            select_columns = """
                id, user_id, sender_name,
                CASE
                    WHEN COALESCE(LENGTH(message), 0) > ? THEN SUBSTR(message, 1, ?)
                    ELSE message
                END as message,
                COALESCE(LENGTH(message), 0) as message_length,
                CASE WHEN COALESCE(LENGTH(message), 0) > ? THEN 1 ELSE 0 END as message_truncated,
                timestamp, session_id, message_type, session_name, msg_id, is_recalled,
                {avatar_select}, {platform_select}
            """.format(avatar_select=avatar_select, platform_select=platform_select)
            select_params = [_HISTORY_MESSAGE_MAX_CHARS, _HISTORY_MESSAGE_MAX_CHARS, _HISTORY_MESSAGE_MAX_CHARS]

        query_conditions = list(conditions)
        query_params = list(params)

        if cursor > 0:
            query_conditions.append("id < ?")
            query_params.append(cursor)
            where_cl_cursor = " WHERE " + " AND ".join(query_conditions)
            query = f"SELECT {select_columns} FROM chat_history {where_cl_cursor} ORDER BY id DESC LIMIT ?"
            query_params.append(limit)
        else:
            # Subquery pagination optimization to avoid performance degradation on deep offsets
            query = f"SELECT {select_columns} FROM chat_history WHERE id IN (SELECT id FROM chat_history {where_cl} ORDER BY id DESC LIMIT ? OFFSET ?) ORDER BY id DESC"
            offset = (page - 1) * limit
            query_params.extend([limit, offset])

        records = db.execute(query, [*select_params, *query_params]).fetchall()

        if records:
            next_cursor = records[-1]["id"]
            has_more = len(records) == limit
        else:
            next_cursor = 0
            has_more = False

        # Exact COUNT(*) can dominate latency on large archives. Keep the legacy
        # `total` field, but only compute it when explicitly requested. For the
        # common unfiltered session view, use session_stats as a cheap compatible
        # estimate that is exact for append-only history.
        total = None
        total_exact = False
        if include_total:
            count_query = f"SELECT COUNT(*) as total FROM chat_history {where_cl}"
            total = db.execute(count_query, params).fetchone()["total"]
            total_exact = True
        elif session_id and not keyword and not user_id and not time_start and not time_end:
            stats_id = "legacy:archive" if session_id == "legacy:archive" else str(session_id)
            try:
                row = db.execute(
                    "SELECT message_count FROM session_stats WHERE session_id = ?",
                    [stats_id],
                ).fetchone()
                if row:
                    total = row["message_count"]
                    total_exact = True
            except Exception:
                total = None

        right_ids = load_right_align_ids()
        record_user_ids_set = {
            str(row.get("user_id", "")).strip()
            for row in records
            if str(row.get("user_id", "")).strip()
        }
        record_user_ids = sorted(record_user_ids_set)

        # Extract referenced user IDs from CQ:at codes in messages to resolve their nicknames
        import re
        referenced_user_ids = set()
        for row in records:
            msg_text = str(row.get("message") or "")
            for match in re.finditer(r"\[CQ:at,qq=(\d+)\]", msg_text):
                referenced_user_ids.add(match.group(1))

        latest_profiles = _fetch_latest_sender_profiles(
            db,
            record_user_ids,
            conditions,
            params,
        )

        # Query globally to resolve names and avatars for all involved users (including AT'ed users)
        all_profile_user_ids = sorted(record_user_ids_set | referenced_user_ids)
        global_profiles = _fetch_latest_sender_profiles(
            db,
            all_profile_user_ids,
            [],
            [],
        )

        # Combine profiles: scoped takes precedence, global as fallback
        user_profiles = {}
        for uid in all_profile_user_ids:
            profile = global_profiles.get(uid, {}).copy() if global_profiles.get(uid) else {}
            if uid in latest_profiles:
                profile.update({k: v for k, v in latest_profiles[uid].items() if v})
            user_profiles[uid] = profile

        processed_records = []
        for r in records:
            item = dict(r)
            uid = str(item.get("user_id", "")).strip()
            sname = str(item.get("sender_name", "")).strip()
            msg_type = str(item.get("message_type", "")).strip().lower()
            profile = user_profiles.get(uid, {})
            if not item.get("avatar_url") and profile.get("avatar_url"):
                item["avatar_url"] = profile["avatar_url"]
            if not item.get("platform_name") and profile.get("platform_name"):
                item["platform_name"] = profile["platform_name"]

            is_right = False
            is_bot = sname.lower() == "bot" or "bot" in uid.lower() or uid == "99999"
            is_admin = uid in right_ids

            if is_bot:
                is_right = True
            elif is_admin and "group" in msg_type:
                is_right = True

            item["is_right"] = is_right
            processed_records.append(item)

        return JSONResponse(
            content={
                "success": True,
                "data": processed_records,
                "total": total,
                "page": page,
                "limit": limit,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "total_exact": total_exact,
                "user_profiles": user_profiles,
            }
        )
    except Exception as e:
        logger.error(f"WebUI get_history error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if db:
            db.close()

@app.get("/api/proxy/image")
async def proxy_image(url: str = Query(..., max_length=4096)):
    """
    代理媒体请求，解决 NTQQ 域名 (multimedia.nt.qq.com.cn) 的跨域与 Referer 限制。
    支持图片和视频的代理流式传输。
    已修复：域名提取 SSRF 漏洞 与 &amp; 实体字符容错。
    """
    # 容错：替换转义的 &amp;
    url = url.replace("&amp;", "&")

    # 安全校验：仅允许代理指定的域名（支持显式白名单及其子域名）
    try:
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ("http", "https"):
            raise HTTPException(status_code=400, detail="Invalid URL scheme")
        hostname = (parsed_url.hostname or "").lower().rstrip(".")
        if not hostname:
            raise HTTPException(status_code=400, detail="Invalid URL")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    if not _hostname_matches_allowlist(hostname, ALLOWED_MEDIA_DOMAINS):
        raise HTTPException(status_code=403, detail="Forbidden domain")
    if not await _hostname_resolves_to_public_ips(hostname):
        raise HTTPException(status_code=403, detail="Forbidden DNS target")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        "Referer": "https://q.qq.com/",
    }
    client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        request = client.build_request("GET", url, headers=headers)
        response = await client.send(request, stream=True)
    except Exception as e:
        await _close_media_upstream(None, client)
        logger.error(f"Media proxy open stream error for {url}: {e}")
        raise HTTPException(status_code=502, detail="Media upstream unavailable")

    if response.status_code != 200:
        await _close_media_upstream(response, client)
        raise HTTPException(status_code=response.status_code, detail="Media upstream failed")

    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not (content_type.startswith("image/") or content_type.startswith("video/")):
        await _close_media_upstream(response, client)
        logger.warning(f"Media proxy rejected non-image/video response: {url}, content-type={content_type or 'unknown'}")
        raise HTTPException(status_code=415, detail="Unsupported media type")

    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MEDIA_MAX_BYTES:
                await _close_media_upstream(response, client)
                logger.warning(f"Media proxy rejected oversized response by content-length: {url}, size={content_length}")
                raise HTTPException(status_code=413, detail="Media too large")
        except ValueError:
            pass

    async def stream_media():
        downloaded = 0
        try:
            async for chunk in response.aiter_bytes():
                downloaded += len(chunk)
                if downloaded > MEDIA_MAX_BYTES:
                    logger.warning(f"Media proxy stopped oversized stream: {url}, size>{MEDIA_MAX_BYTES}")
                    return
                yield chunk
        except Exception as e:
            logger.error(f"Media proxy error for {url}: {e}")
        finally:
            await _close_media_upstream(response, client)

    return StreamingResponse(
        stream_media(),
        media_type=content_type,
        background=BackgroundTask(_close_media_upstream, response, client),
    )


def _find_cached_telegram_avatar(cache_dir: Path, cache_key: str) -> str:
    if not cache_dir or not cache_dir.exists():
        return ""
    import hashlib
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:32]
    for suffix in (".jpg", ".png", ".jpeg", ".webp", ".gif"):
        filename = f"telegram_avatar_{digest}{suffix}"
        if (cache_dir / filename).exists():
            return f"/static/cache/{filename}"
    return ""


@app.get("/api/sessions")
def get_sessions(request: Request):
    db = None
    try:
        db = get_db_connection()
        query = """
            SELECT session_id,
                   COALESCE(message_type, 'legacy') as message_type,
                   last_time,
                   last_msg,
                   sender_name,
                   session_name,
                   message_count as count
            FROM session_stats
            ORDER BY last_time DESC
        """
        try:
            sessions = db.execute(query).fetchall()
        except Exception:
            # Fallback for old databases or external callers that have not run init_db.
            query = """
                SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive') as session_id,
                       COALESCE(message_type, 'legacy') as message_type,
                       timestamp as last_time,
                       message as last_msg,
                       sender_name,
                       session_name,
                       0 as count
                FROM chat_history
                WHERE id IN (
                    SELECT MAX(id)
                    FROM chat_history
                    GROUP BY COALESCE(NULLIF(session_id, ''), 'legacy:archive')
                )
                ORDER BY last_time DESC
            """
            sessions = db.execute(query).fetchall()

        plugin = getattr(request.app.state, "plugin", None)
        bot_ids = plugin.get_bot_ids() if plugin else ["bot", "99999", "astrbot"]

        session_profiles = _fetch_latest_session_profiles(
            db,
            [str(s["session_id"] or "legacy:archive") for s in sessions],
            bot_ids=bot_ids,
        )
        friend_profile_ids = []
        for session in sessions:
            mt = str(session.get("message_type", "") or "").lower()
            if "friend" in mt:
                sid = str(session.get("session_id", "") or "")
                friend_profile_ids.append(sid.split(":")[-1] if ":" in sid else sid)
        friend_profiles = _fetch_latest_sender_profiles(
            db,
            [uid for uid in friend_profile_ids if uid],
            [],
            [],
        )
        for s in sessions:
            s_id = s["session_id"]
            if s_id == "legacy:archive":
                s["name"] = "📦 历史记录 (未分类)"
                s["avatar"] = ""
                s["platform_name"] = ""
                continue

            name = s_id
            avatar = ""
            profile = session_profiles.get(str(s_id), {})
            profile_avatar = profile.get("avatar_url", "")
            platform_name = str(profile.get("platform_name", "")).lower()
            s_platform = s_id.split(":", 1)[0].lower() if ":" in s_id else platform_name
            s["platform_name"] = platform_name or s_platform

            # Check for cached Telegram avatar directly on disk first to avoid speaker avatar lock
            if s_platform == "telegram":
                if s["message_type"] in ["group", "GroupMessage", "channel", "ChannelMessage"]:
                    chat_id = s_id.split(":")[-1] if ":" in s_id else s_id
                    cached_avatar = _find_cached_telegram_avatar(cache_static_dir, f"telegram:chat:{chat_id}")
                    if cached_avatar:
                        profile_avatar = cached_avatar
                elif s["message_type"] in ["friend", "FriendMessage"]:
                    user_id = s_id.split(":")[-1] if ":" in s_id else s_id
                    cached_avatar = _find_cached_telegram_avatar(cache_static_dir, f"telegram:user:{user_id}")
                    if cached_avatar:
                        profile_avatar = cached_avatar

            if s["message_type"] in ["group", "GroupMessage", "channel", "ChannelMessage"]:
                group_id = s_id.split(":")[-1] if ":" in s_id else s_id
                if profile_avatar:
                    avatar = profile_avatar
                elif s_platform not in ("telegram", "discord"):
                    avatar = f"https://p.qlogo.cn/gh/{group_id}/{group_id}/100/"

                db_name = s.get("session_name")
                if db_name and db_name.strip():
                    name = db_name.strip()
                else:
                    if s["message_type"] in ["channel", "ChannelMessage"]:
                        name = f"频道: {group_id}"
                    else:
                        name = f"群聊: {group_id}"

            elif s["message_type"] in ["friend", "FriendMessage"]:
                user_id = s_id.split(":")[-1] if ":" in s_id else s_id
                friend_profile = friend_profiles.get(str(user_id), {})
                if not profile_avatar:
                    profile_avatar = friend_profile.get("avatar_url", "")
                    platform_name = platform_name or str(friend_profile.get("platform_name", "")).lower()
                if profile_avatar:
                    avatar = profile_avatar
                elif s_platform not in ("telegram", "discord"):
                    avatar = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"

                # Retrieve the friend's nickname from their profile first
                friend_name = friend_profile.get("sender_name", "").strip()
                db_name = s.get("session_name", "").strip()

                if friend_name:
                    name = friend_name
                elif db_name:
                    name = db_name
                elif s["sender_name"] and s["sender_name"].strip() and s["sender_name"].strip().lower() != "bot":
                    name = s["sender_name"].strip()
                else:
                    name = user_id

            s["name"] = name
            s["avatar"] = avatar

        return JSONResponse(content={"success": True, "data": sessions})
    except Exception as e:
        logger.error(f"WebUI get_sessions error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if db:
            db.close()

@app.get("/api/dashboard")
def get_dashboard(
    range: str = Query("30d", max_length=8),
    recent_limit: int = Query(12, ge=1, le=50),
    refresh: bool = Query(False),
):
    range_key, _ = _dashboard_range_days(range)
    cache_key = f"{range_key}:{recent_limit}"
    now = time.time()
    if not refresh:
        with _DASHBOARD_CACHE_LOCK:
            cached = _DASHBOARD_CACHE.get(cache_key)
            if cached and cached[0] > now:
                payload = dict(cached[1])
                payload["cached"] = True
                if "performance" in payload:
                    payload["performance"] = dict(payload["performance"])
                    payload["performance"]["cache_hit"] = True
                return JSONResponse(content={"success": True, "data": payload})

    try:
        data = _compute_dashboard(range_key, recent_limit)
        data["cached"] = False
        with _DASHBOARD_CACHE_LOCK:
            _DASHBOARD_CACHE[cache_key] = (now + _DASHBOARD_CACHE_TTL, data)
        return JSONResponse(content={"success": True, "data": data})
    except Exception as e:
        logger.error(f"WebUI get_dashboard error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/stats")
def get_stats(
    session_id: str = Query("", max_length=256),
    user_id: str = Query("", max_length=128),
    time_start: int = Query(0, ge=0),
    time_end: int = Query(0, ge=0),
    is_private: int = Query(0, ge=0, le=1),
):
    db = None
    try:
        cache_key = json.dumps(
            [session_id, user_id, int(time_start or 0), int(time_end or 0), int(is_private or 0)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        now = time.time()
        if _STATS_CACHE_TTL > 0:
            with _STATS_CACHE_LOCK:
                cached = _STATS_CACHE.get(cache_key)
                if cached and cached[0] > now:
                    return JSONResponse(content={"success": True, "data": cached[1]})

        db = get_db_connection()

        conditions = ["1=1"]
        params = []
        if session_id == "legacy:archive":
            conditions.append(
                "(session_id IS NULL OR session_id = '' OR session_id = 'legacy:archive')"
            )
        elif session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if time_start:
            conditions.append("timestamp >= ?")
            params.append(time_start)
        if time_end:
            conditions.append("timestamp <= ?")
            params.append(time_end)

        where_cl = " WHERE " + " AND ".join(conditions)
        has_media_columns = _table_has_columns(db, "chat_history", ("has_image", "has_video", "msg_kind"))

        local_now = datetime.datetime.now().astimezone()
        local_today_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = int(local_today_midnight.timestamp())
        local_offset = int(local_now.utcoffset().total_seconds())

        slot_columns = ",\n".join(
            f"SUM(CASE WHEN CAST(((timestamp + {local_offset}) / 7200) % 12 AS INTEGER) = {slot} THEN 1 ELSE 0 END) as slot_{slot}"
            for slot in range(12)
        )
        summary_row = db.execute(
            f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) as today_total,
                       {slot_columns}
                FROM chat_history {where_cl}
            """,
            [today_start, *params],
        ).fetchone()
        total = int(summary_row["total"] or 0) if summary_row else 0
        today_total = int(summary_row["today_total"] or 0) if summary_row else 0
        distribution = [
            int(summary_row[f"slot_{slot}"] or 0) if summary_row else 0
            for slot in range(12)
        ]

        top_conditions = [
            *conditions,
            "user_id IS NOT NULL",
            "user_id != ''",
            "user_id != '0'",
            "(is_recalled IS NULL OR is_recalled = 0)",
        ]
        top_rows = _fetch_user_counts(db, top_conditions, params, 30)
        top_users = []
        for r in top_rows:
            top_users.append({
                "user_id": r["user_id"],
                "sender_name": r["sender_name"],
                "avatar_url": r.get("avatar_url", ""),
                "platform_name": r.get("platform_name", ""),
                "count": r["cnt"]
            })

        # Active days (if filtering by user_id)
        active_days = 0
        avg_text_len = 0
        message_types = []
        if user_id:
            image_expr = (
                "SUM(CASE WHEN has_image = 1 THEN 1 ELSE 0 END)"
                if has_media_columns
                else "SUM(CASE WHEN message LIKE '%[CQ:image%' THEN 1 ELSE 0 END)"
            )
            detail_row = db.execute(
                f"""
                    SELECT COUNT(DISTINCT CAST(timestamp / 86400 AS INTEGER)) as days,
                           AVG(CASE
                               WHEN message NOT LIKE '[CQ:%' AND message NOT LIKE '<Event%' THEN LENGTH(message)
                               ELSE NULL
                           END) as avg_len,
                           {image_expr} as image_count
                    FROM chat_history {where_cl}
                """,
                params,
            ).fetchone()
            active_days = int(detail_row["days"] or 0) if detail_row else 0
            avg_text_len = round(detail_row["avg_len"]) if detail_row and detail_row["avg_len"] else 0

            image_count = int(detail_row["image_count"] or 0) if detail_row else 0
            text_count = max(total - image_count, 0)
            message_types = [
                {"name": "文本消息", "value": text_count},
                {"name": "图片消息", "value": image_count},
            ]

        payload = {
            "total_messages": total,
            "today_messages": today_total,
            "active_days": active_days,
            "avg_text_length": avg_text_len,
            "time_distribution": distribution,
            "top_users": top_users,
            "message_types": message_types if message_types else None,
        }
        if _STATS_CACHE_TTL > 0:
            with _STATS_CACHE_LOCK:
                _STATS_CACHE[cache_key] = (now + _STATS_CACHE_TTL, payload)
        return JSONResponse(content={"success": True, "data": payload})
    except Exception as e:
        logger.error(f"WebUI get_stats error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if db:
            db.close()

@app.get("/api/members")
def get_members(
    session_id: str = Query("", max_length=256),
    keyword: str = Query("", max_length=100),
    time_start: int = Query(0, ge=0),
    time_end: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    db = None
    try:
        db = get_db_connection()

        conditions = [
            "user_id IS NOT NULL",
            "user_id != ''",
            "user_id != '0'",
            "(is_recalled IS NULL OR is_recalled = 0)",
        ]
        params = []
        if session_id == "legacy:archive":
            conditions.append(
                "(session_id IS NULL OR session_id = '' OR session_id = 'legacy:archive')"
            )
        elif session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if time_start:
            conditions.append("timestamp >= ?")
            params.append(time_start)
        if time_end:
            conditions.append("timestamp <= ?")
            params.append(time_end)
        if keyword:
            safe_keyword = (
                keyword.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            conditions.append("(sender_name LIKE ? ESCAPE '\\' OR user_id LIKE ? ESCAPE '\\')")
            params.extend([f"%{safe_keyword}%", f"%{safe_keyword}%"])

        where_cl = " WHERE " + " AND ".join(conditions)

        count_sql = f"""
            SELECT COUNT(*) as total FROM (
                SELECT user_id FROM chat_history {where_cl} GROUP BY user_id
            )
        """
        total = db.execute(count_sql, params).fetchone()["total"]

        rows = _fetch_user_counts(db, conditions, params, limit, offset)
        members = [
            {
                "user_id": r["user_id"],
                "sender_name": r["sender_name"],
                "avatar_url": r.get("avatar_url", ""),
                "platform_name": r.get("platform_name", ""),
                "count": r["cnt"],
            }
            for r in rows
        ]

        return JSONResponse(
            content={
                "success": True,
                "data": {
                    "members": members,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + len(members) < total,
                },
            }
        )
    except Exception as e:
        logger.error(f"WebUI get_members error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if db:
            db.close()

def _load_custom_apis():
    try:
        import importlib.util
        import stat
        data_dir = get_data_dir()
        ext_dir = data_dir.parent.parent / "chat_archive_ext"
        if not ext_dir.exists():
            ext_dir = Path(__file__).resolve().parent.parent.parent.parent / "chat_archive_ext"

        if ext_dir.exists():
            # Security check: directory must not be world-writable
            try:
                dir_stat = ext_dir.stat()
                if dir_stat.st_mode & stat.S_IWOTH:
                    logger.error(f"Chat Archive Ext: 拒绝加载自定义 API 扩展。原因：扩展目录 {ext_dir} 对其他用户可写，存在严重安全风险，请修改其权限 (如 chmod 700)。")
                    return
            except Exception as e:
                logger.error(f"Chat Archive Ext: 读取扩展目录属性失败: {e}")
                return

            for f in ext_dir.glob("*.py"):
                if f.name.startswith("_"):
                    continue
                try:
                    # Security check 1: prevent path traversal via symlinks
                    resolved_f = f.resolve()
                    if not str(resolved_f).startswith(str(ext_dir.resolve())):
                        logger.error(f"Chat Archive Ext: 拒绝加载自定义 API {f.name}。原因：试图跨越目录的路径探测攻击。")
                        continue

                    # Security check 2: file must not be world-writable
                    file_stat = f.stat()
                    if file_stat.st_mode & stat.S_IWOTH:
                        logger.error(f"Chat Archive Ext: 拒绝加载自定义 API {f.name}。原因：文件对其他用户可写，存在潜在代码注入风险，请修改权限 (如 chmod 600)。")
                        continue

                    spec = importlib.util.spec_from_file_location(f.stem, str(f))
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        if hasattr(module, "register"):
                            module.register(app, get_db_connection)
                            logger.info(f"Chat Archive Ext: 成功加载自定义 API [{f.name}]")
                except Exception as ex:
                    logger.error(f"Chat Archive Ext: 加载自定义 API {f.name} 失败: {ex}")
    except Exception as e:
        logger.error(f"Chat Archive Ext: 扫描自定义 API 失败: {e}")

# NOTE: Custom APIs are loaded in AdminServer.run_in_thread() after API_KEY is configured.
# For standalone usage (`python server.py`), see __main__ block below.
_custom_apis_loaded = False


class AdminServer:
    def __init__(self, plugin_instance, host: str = "127.0.0.1", port: int = 8090, api_key: str = "", cache_dir: Path = None):
        self.plugin = plugin_instance
        app.state.plugin = plugin_instance
        self.host = os.environ.get("ARCHIVE_HOST", "").strip() or host
        try:
            self.port = int(os.environ.get("ARCHIVE_PORT", "").strip() or port)
        except (TypeError, ValueError):
            self.port = 8090

        global API_KEY
        API_KEY = os.environ.get("ARCHIVE_API_KEY", "").strip() or api_key or API_KEY

        self.config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = None

    def run_in_thread(self):
        """在守护线程中启动 Uvicorn，避免阻塞 AstrBot 主进程事件循环"""
        if self.thread and self.thread.is_alive():
            return

        def _run():
            try:
                asyncio.run(self.server.serve())
            except (Exception, SystemExit) as exc:
                logger.error(f"Chat Archive WebUI failed on {self.host}:{self.port}: {exc}")
                return
            if not self.server.started:
                logger.error(f"Chat Archive WebUI did not start on {self.host}:{self.port}; check dependencies, bind address and port conflicts.")

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()

        # Load custom extension APIs after server starts, when API_KEY is set
        global _custom_apis_loaded
        if not _custom_apis_loaded:
            _load_custom_apis()
            _custom_apis_loaded = True

        logger.info(f"Chat Archive WebUI startup requested on http://{self.host}:{self.port}")

    async def stop(self):
        """插件卸载时优雅关闭后台服务"""
        self.server.should_exit = True
        if self.thread and self.thread.is_alive():
            await asyncio.to_thread(self.thread.join, timeout=5.0)
            logger.info("Chat Archive WebUI stopped.")


if __name__ == "__main__":
    # In standalone execution, try loading API_KEY from env or config file
    if not API_KEY:
        API_KEY = os.environ.get("ARCHIVE_API_KEY", "").strip()
    if not API_KEY:
        # Try loading from JSON config file
        try:
            config_path = get_config_path()
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8-sig") as fh:
                    cfg_data = json.load(fh)
                    API_KEY = cfg_data.get("web_server", {}).get("api_key", "")
        except Exception:
            pass
    if not API_KEY:
        # If API_KEY is still not set, raise RuntimeError to prevent unauthenticated public internet exposure!
        raise RuntimeError("CRITICAL SECURITY ERROR: api_key is not configured in environment (ARCHIVE_API_KEY) or config file! Standalone web server cannot start in unauthenticated mode.")

    _load_custom_apis()
    host = os.environ.get("ARCHIVE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("ARCHIVE_PORT", "8090"))
    except ValueError:
        port = 8090
    uvicorn.run(app, host=host, port=port)
