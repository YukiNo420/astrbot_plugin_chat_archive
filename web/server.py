from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import mimetypes
import os
import re
import secrets
import stat
import threading
import time
from contextlib import asynccontextmanager, suppress
from functools import lru_cache
from pathlib import Path
from typing import Literal

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

try:
    from ..archive_management import ArchiveManager, ManagementConflict
except ImportError:
    import sys

    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from archive_management import ArchiveManager, ManagementConflict

try:
    from ..db_config import (
        DB_PATH,
        add_message_search_condition,
        get_db_connection,
        init_db,
    )
except ImportError:
    try:
        import sys

        sys.path.append(str(Path(__file__).resolve().parent.parent))
        from db_config import (
            DB_PATH,
            add_message_search_condition,
            get_db_connection,
            init_db,
        )
    except Exception:
        raise RuntimeError(
            "无法加载 db_config 模块。如果是独立解耦运行，请使用 'python -m astrbot_plugin_chat_archive.web.server' 从 AstrBot 的 plugins 目录执行。"
        )

try:
    from ..serializer import sanitize_cq_media_codes
except ImportError:
    try:
        import sys

        sys.path.append(str(Path(__file__).resolve().parent.parent))
        from serializer import sanitize_cq_media_codes
    except Exception as exc:
        raise RuntimeError(
            "无法加载 serializer 模块；WebUI 已拒绝以未净化消息模式启动。"
        ) from exc

try:
    from ..media_cache import (
        GENERIC_MEDIA_TYPES,
        PinnedPublicResolver,
        is_passive_media_file,
        read_media_prefix,
        sniff_passive_media,
        validate_remote_media_url,
    )
except ImportError:
    try:
        import sys

        sys.path.append(str(Path(__file__).resolve().parent.parent))
        from media_cache import (
            GENERIC_MEDIA_TYPES,
            PinnedPublicResolver,
            is_passive_media_file,
            read_media_prefix,
            sniff_passive_media,
            validate_remote_media_url,
        )
    except Exception:
        raise RuntimeError("无法加载 media_cache 模块。")

from astrbot.api import logger

try:
    from ..config import (
        get_config_path,
        get_data_dir,
        get_legacy_data_dir,
        load_allow_fake_ip,
        load_allowed_media_domains,
        load_api_key,
        load_auth_failure_limit,
        load_auth_lockout_seconds,
        load_media_max_bytes,
    )
except ImportError:
    try:
        import sys

        sys.path.append(str(Path(__file__).resolve().parent.parent))
        from config import (
            get_config_path,
            get_data_dir,
            get_legacy_data_dir,
            load_allow_fake_ip,
            load_allowed_media_domains,
            load_api_key,
            load_auth_failure_limit,
            load_auth_lockout_seconds,
            load_media_max_bytes,
        )
    except Exception:
        raise RuntimeError("无法加载 config 模块。")

API_KEY = load_api_key()


MEDIA_MAX_BYTES = load_media_max_bytes()
ALLOWED_MEDIA_DOMAINS = load_allowed_media_domains()
_SAFE_MEDIA_TYPES = frozenset(
    {
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/ogg",
        "video/quicktime",
        "video/webm",
        "video/x-m4v",
        "video/x-matroska",
        "video/x-msvideo",
        "audio/aac",
        "audio/amr",
        "audio/flac",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "audio/x-m4a",
        "audio/x-wav",
    }
)
_INLINE_CACHE_SUFFIXES = frozenset(
    {
        ".avif",
        ".aac",
        ".amr",
        ".flac",
        ".gif",
        ".jpeg",
        ".jpg",
        ".png",
        ".webp",
        ".mp4",
        ".m4v",
        ".ogg",
        ".oga",
        ".mp3",
        ".wav",
        ".webm",
        ".mov",
        ".mkv",
        ".avi",
    }
)
_ATTACHMENT_CACHE_SUFFIXES = frozenset({".dat", ".pdf", ".zip"})
_SAFE_CACHE_SUFFIXES = _INLINE_CACHE_SUFFIXES | _ATTACHMENT_CACHE_SUFFIXES
_CACHE_MEDIA_TYPES = {
    ".aac": "audio/aac",
    ".amr": "audio/amr",
    ".avif": "image/avif",
    ".flac": "audio/flac",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}
_CACHE_FILENAME_RE = re.compile(
    r"(?:[0-9a-f]{32}|telegram_(?:avatar|chat_avatar|media)_[0-9a-f]{32})"
    r"\.(?:aac|amr|avif|flac|gif|jpe?g|png|webp|mp4|m4v|ogg|oga|mp3|wav|webm|mov|mkv|avi|dat|pdf|zip)",
    flags=re.IGNORECASE,
)
_MEDIA_SNIFF_CACHE: dict[str, tuple[float, str]] = {}
_MEDIA_SNIFF_CACHE_LOCK = threading.Lock()
_MEDIA_SNIFF_CACHE_TTL = 300
_MEDIA_SNIFF_CACHE_MAX_ENTRIES = 2048


def _media_sniff_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _get_cached_sniffed_media_type(url: str) -> str:
    key = _media_sniff_cache_key(url)
    now = time.time()
    with _MEDIA_SNIFF_CACHE_LOCK:
        entry = _MEDIA_SNIFF_CACHE.get(key)
        if not entry:
            return ""
        expires_at, media_type = entry
        if expires_at <= now:
            _MEDIA_SNIFF_CACHE.pop(key, None)
            return ""
        return media_type


def _cache_sniffed_media_type(url: str, media_type: str) -> None:
    key = _media_sniff_cache_key(url)
    now = time.time()
    with _MEDIA_SNIFF_CACHE_LOCK:
        stale_keys = [
            cache_key
            for cache_key, (expires_at, _media_type) in _MEDIA_SNIFF_CACHE.items()
            if expires_at <= now
        ]
        for stale_key in stale_keys:
            _MEDIA_SNIFF_CACHE.pop(stale_key, None)
        _MEDIA_SNIFF_CACHE[key] = (now + _MEDIA_SNIFF_CACHE_TTL, media_type)
        while len(_MEDIA_SNIFF_CACHE) > _MEDIA_SNIFF_CACHE_MAX_ENTRIES:
            _MEDIA_SNIFF_CACHE.pop(next(iter(_MEDIA_SNIFF_CACHE)))


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
                with open(f, encoding="utf-8-sig") as fh:
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ensure standalone WebUI runs with the latest database schema."""
    if not getattr(_app.state, "schema_ready", False):
        data_dir = get_data_dir()
        destination_db = (data_dir / "chat_history.db").resolve()
        if Path(DB_PATH).expanduser().resolve() == destination_db:
            legacy_db = get_legacy_data_dir() / "chat_history.db"
            if legacy_db.exists():
                if destination_db.exists():
                    detail = "both legacy and destination databases exist"
                else:
                    detail = "the legacy database still needs migration"
                raise RuntimeError(
                    "Chat Archive standalone WebUI refused to initialize: "
                    f"{detail}. Start or reload the AstrBot plugin first so it "
                    "can complete the exclusive database migration."
                )
        await asyncio.to_thread(init_db)
    yield


app = FastAPI(title="Chat Archive Admin Panel", lifespan=lifespan)

current_dir = Path(__file__).resolve().parent
static_dir = current_dir / "static"
templates_dir = current_dir / "templates"
cache_static_dir = get_data_dir() / "web_cache"
legacy_cache_static_dir = current_dir.parent / "data" / "web_cache"
_CACHE_STATIC_DIRS = tuple(
    dict.fromkeys(
        path.resolve(strict=False)
        for path in (cache_static_dir, legacy_cache_static_dir)
    )
)

cors_origins_env = os.environ.get(
    "ARCHIVE_CORS_ORIGINS", os.environ.get("CORS_ORIGINS", "http://localhost:8090")
)
cors_origins = [
    origin.strip() for origin in cors_origins_env.split(",") if origin.strip()
]

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
_AUTH_FAILURES: dict[str, tuple[int, float, float]] = {}
_AUTH_FAILURES_LOCK = threading.Lock()
_AUTH_FAILURE_LIMIT = load_auth_failure_limit()
_AUTH_FAILURE_WINDOW = 300
_AUTH_LOCKOUT_SECONDS = load_auth_lockout_seconds()
_AUTH_BODY_MAX_BYTES = 4096
_API_KEY_LOCK = threading.Lock()


def _set_api_key(value: str) -> bool:
    """Apply a new runtime key and revoke state only when the value changes."""
    global API_KEY

    normalized = str(value or "").strip()
    with _API_KEY_LOCK:
        if normalized == API_KEY:
            return False

        # Fail closed during the very small rotation window, then revoke every
        # credential and lockout record derived from the previous key.
        API_KEY = ""
        with _SESSION_TOKENS_LOCK:
            _SESSION_TOKENS.clear()
        with _AUTH_FAILURES_LOCK:
            _AUTH_FAILURES.clear()
        API_KEY = normalized
        return True


def _refresh_runtime_policy() -> None:
    """Refresh policy values that can safely change without rebuilding FastAPI."""
    global ALLOWED_MEDIA_DOMAINS, MEDIA_MAX_BYTES
    global _AUTH_FAILURE_LIMIT, _AUTH_LOCKOUT_SECONDS

    next_domains = load_allowed_media_domains()
    next_media_max = load_media_max_bytes()
    next_failure_limit = load_auth_failure_limit()
    next_lockout_seconds = load_auth_lockout_seconds()

    media_changed = (
        next_domains != ALLOWED_MEDIA_DOMAINS or next_media_max != MEDIA_MAX_BYTES
    )
    auth_changed = (
        next_failure_limit != _AUTH_FAILURE_LIMIT
        or next_lockout_seconds != _AUTH_LOCKOUT_SECONDS
    )

    ALLOWED_MEDIA_DOMAINS = next_domains
    MEDIA_MAX_BYTES = next_media_max
    _AUTH_FAILURE_LIMIT = next_failure_limit
    _AUTH_LOCKOUT_SECONDS = next_lockout_seconds

    if media_changed:
        with _MEDIA_SNIFF_CACHE_LOCK:
            _MEDIA_SNIFF_CACHE.clear()
    if auth_changed:
        with _AUTH_FAILURES_LOCK:
            _AUTH_FAILURES.clear()


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


def _auth_client_key(request: Request) -> str:
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") if client else ""
    return str(host or "unknown")


def _auth_retry_after(client_key: str) -> int:
    now = time.time()
    with _AUTH_FAILURES_LOCK:
        expired = [
            key
            for key, (_count, window_start, locked_until) in _AUTH_FAILURES.items()
            if locked_until <= now and now - window_start > _AUTH_FAILURE_WINDOW
        ]
        for key in expired:
            _AUTH_FAILURES.pop(key, None)

        entry = _AUTH_FAILURES.get(client_key)
        if not entry:
            return 0
        _count, _window_start, locked_until = entry
        if locked_until > now:
            return max(1, int(locked_until - now))
        if locked_until:
            _AUTH_FAILURES.pop(client_key, None)
        return 0


def _record_auth_failure(client_key: str) -> int:
    now = time.time()
    with _AUTH_FAILURES_LOCK:
        count, window_start, locked_until = _AUTH_FAILURES.get(
            client_key, (0, now, 0.0)
        )
        if locked_until > now:
            return max(1, int(locked_until - now))
        if locked_until:
            count = 0
            window_start = now
            locked_until = 0.0
        if now - window_start > _AUTH_FAILURE_WINDOW:
            count = 0
            window_start = now
        count += 1
        if count >= _AUTH_FAILURE_LIMIT:
            locked_until = now + _AUTH_LOCKOUT_SECONDS
            _AUTH_FAILURES[client_key] = (count, window_start, locked_until)
            return _AUTH_LOCKOUT_SECONDS
        _AUTH_FAILURES[client_key] = (count, window_start, 0.0)
        return 0


def _clear_auth_failures(client_key: str) -> None:
    with _AUTH_FAILURES_LOCK:
        _AUTH_FAILURES.pop(client_key, None)


async def _read_limited_json_body(
    request: Request, max_bytes: int = _AUTH_BODY_MAX_BYTES
) -> dict:
    content_length = request.headers.get("content-length", "")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="Request body too large")
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    return data if isinstance(data, dict) else {}


def _constant_time_text_equal(left: str, right: str) -> bool:
    """Compare arbitrary Unicode secrets without compare_digest's str limit."""
    return secrets.compare_digest(
        str(left or "").encode("utf-8"),
        str(right or "").encode("utf-8"),
    )


def _append_vary(response, *values: str) -> None:
    existing = {
        item.strip()
        for item in response.headers.get("Vary", "").split(",")
        if item.strip()
    }
    existing.update(value for value in values if value)
    if existing:
        response.headers["Vary"] = ", ".join(sorted(existing, key=str.lower))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS":
        # Let CORSMiddleware validate and answer preflight requests. No endpoint
        # data is exposed by OPTIONS itself.
        return await call_next(request)

    is_public_static = path.startswith("/static/") and not path.startswith(
        "/static/cache/"
    )
    public_paths = {"/", "/api/auth/status", "/api/auth/verify"}
    if not API_KEY:
        # 如果 API_KEY 为空，仅允许访问登录页、验证接口和非缓存静态资源。
        if path not in public_paths and not is_public_static:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized",
                    "message": "API Key is not configured. Access is disabled for security reasons.",
                },
            )
        return await call_next(request)

    is_public = path in public_paths or is_public_static

    if is_public:
        return await call_next(request)

    # Support API Key via header (direct key) or HttpOnly cookie (session token).
    # Never accept API keys in URLs.
    header_key = request.headers.get("X-API-Key", "")
    cookie_token = request.cookies.get("archive_auth", "")

    # A valid browser session does not participate in API-key guessing limits.
    if cookie_token and _validate_session_token(cookie_token):
        request.state.archive_principal = hashlib.sha256(
            ("cookie:" + cookie_token).encode()
        ).hexdigest()
        return await call_next(request)

    if header_key:
        client_key = _auth_client_key(request)
        retry_after = _auth_retry_after(client_key)
        if retry_after:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "error": "Too Many Requests",
                    "message": "Too many failed authentication attempts",
                },
            )
        if _constant_time_text_equal(header_key, API_KEY):
            _clear_auth_failures(client_key)
            request.state.archive_principal = hashlib.sha256(
                ("header:" + header_key).encode()
            ).hexdigest()
            return await call_next(request)
        retry_after = _record_auth_failure(client_key)
        if retry_after:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "error": "Too Many Requests",
                    "message": "Too many failed authentication attempts",
                },
            )

    return JSONResponse(
        status_code=401,
        content={"error": "Unauthorized", "message": "Invalid API Key"},
    )


@app.middleware("http")
async def response_security_headers(request: Request, call_next):
    """Prevent authenticated content from being stored or MIME-sniffed."""
    response = await call_next(request)
    path = request.url.path
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if path == "/" or path.startswith("/api/") or path.startswith("/static/cache/"):
        response.headers["Cache-Control"] = "private, no-store"
        _append_vary(response, "Cookie", "X-API-Key")
    elif path.startswith("/static/"):
        # The WebUI is upgraded in place. Revalidate public assets so an old
        # script cannot keep running against a newer server/authentication API.
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    if path == "/":
        response.headers.setdefault(
            "Content-Security-Policy",
            "frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
        )
        response.headers.setdefault("X-Frame-Options", "DENY")
    if path.startswith("/static/cache/") or path == "/api/proxy/image":
        response.headers.setdefault(
            "Content-Security-Policy",
            "sandbox; default-src 'none'",
        )
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    return response


def _cached_file_magic_is_safe(path: Path) -> bool:
    """Reject active content, including SVG disguised with a raster suffix."""
    suffix = path.suffix.lower()
    if path.is_symlink() or suffix not in _SAFE_CACHE_SUFFIXES or not path.is_file():
        return False
    if suffix not in _ATTACHMENT_CACHE_SUFFIXES:
        return is_passive_media_file(path)

    # Documents are only produced by Telegram caching and are served as
    # downloads with nosniff + a sandboxed CSP.
    if not path.name.lower().startswith("telegram_media_"):
        return False
    if suffix == ".dat":
        return True
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
    except OSError:
        return False
    if suffix == ".pdf":
        return header.startswith(b"%PDF-")
    if suffix == ".zip":
        return header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    return False


@app.get("/static/cache/{filename}", name="cache")
async def get_cached_media(filename: str):
    """Serve only hash-named passive media from the new or legacy cache."""
    if not _CACHE_FILENAME_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Cached media not found")

    for directory in _CACHE_STATIC_DIRS:
        candidate = directory / filename
        if await asyncio.to_thread(_cached_file_magic_is_safe, candidate):
            attachment = candidate.suffix.lower() in _ATTACHMENT_CACHE_SUFFIXES
            media_type = (
                "application/octet-stream"
                if attachment
                else _CACHE_MEDIA_TYPES.get(candidate.suffix.lower())
                or mimetypes.guess_type(filename)[0]
                or "application/octet-stream"
            )
            return FileResponse(
                candidate,
                media_type=media_type,
                filename=filename if attachment else None,
                content_disposition_type="attachment" if attachment else "inline",
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Security-Policy": "sandbox; default-src 'none'",
                    "X-Content-Type-Options": "nosniff",
                },
            )
    raise HTTPException(status_code=404, detail="Cached media not found")


app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(templates_dir))

_DASHBOARD_CACHE: dict[str, tuple[float, dict]] = {}
_DASHBOARD_CACHE_LOCK = threading.Lock()
_STATS_CACHE: dict[str, tuple[float, dict]] = {}
_STATS_CACHE_LOCK = threading.Lock()
_DASHBOARD_CACHE_MAX_ENTRIES = 32
_STATS_CACHE_MAX_ENTRIES = 512


def _set_ttl_cache(
    cache: dict[str, tuple[float, dict]],
    key: str,
    expires_at: float,
    value: dict,
    max_entries: int,
):
    """Store a small in-process TTL cache entry and prune expired/old keys."""
    cache[key] = (expires_at, value)
    now = time.time()
    stale_keys = [k for k, (expiry, _) in cache.items() if expiry <= now]
    for stale_key in stale_keys:
        cache.pop(stale_key, None)
    while len(cache) > max_entries:
        oldest_key = min(cache, key=lambda k: cache[k][0])
        cache.pop(oldest_key, None)


_ARCHIVE_MANAGER = ArchiveManager(get_db_connection, cache_static_dir)


class ManagementPreview(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)
    message_id: int = Field(default=0, ge=0)
    before_ts: int = Field(default=0, ge=0)
    start_ts: int = Field(default=0, ge=0)
    end_ts: int = Field(default=0, ge=0)


class ManagementDelete(BaseModel):
    preview_token: str = Field(min_length=1, max_length=128)
    confirm_session_id: str = Field(min_length=1, max_length=256)
    confirm_count: int = Field(gt=0)
    delete_mode: str = Field(default="trash", max_length=16)
    confirm_permanent: str = Field(default="", max_length=16)


class ManagementExport(BaseModel):
    preview_token: str = Field(min_length=1, max_length=128)


class ManagementRestore(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)


def _management_principal(request):
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "不允许跨站管理操作")
    return request.state.archive_principal


def _management_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ManagementConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.error(f"Chat Archive management transaction failed: {exc}")
        raise HTTPException(503, "数据库操作失败；未确认成功，请查看日志") from exc


def _management_body(body):
    return body.model_dump() if hasattr(body, "model_dump") else body.dict()


def invalidate_statistics():
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE.clear()
    with _STATS_CACHE_LOCK:
        _STATS_CACHE.clear()


@app.post("/api/manage/preview")
def management_preview(body: ManagementPreview, request: Request):
    return _management_call(
        _ARCHIVE_MANAGER.preview,
        _management_principal(request),
        **_management_body(body),
    )


@app.post("/api/manage/delete")
def management_delete(body: ManagementDelete, request: Request):
    result = _management_call(
        _ARCHIVE_MANAGER.delete,
        _management_principal(request),
        **_management_body(body),
    )
    invalidate_statistics()
    return result


@app.post("/api/manage/export")
def management_export(body: ManagementExport, request: Request):
    result = _management_call(
        _ARCHIVE_MANAGER.export, _management_principal(request), body.preview_token
    )
    return StreamingResponse(
        iter(lambda: result.read(65536), b""),
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=chat-archive-messages.json",
            "Cache-Control": "no-store",
        },
        background=BackgroundTask(result.close),
    )


@app.post("/api/manage/restore")
def management_restore(body: ManagementRestore, request: Request):
    _management_principal(request)
    result = _management_call(_ARCHIVE_MANAGER.restore, **_management_body(body))
    invalidate_statistics()
    return result


@app.get("/api/manage/trash")
def management_trash(session_id: str = Query(min_length=1, max_length=256)):
    return {"operations": _management_call(_ARCHIVE_MANAGER.trash, session_id)}


@app.get("/api/manage/storage")
def management_storage(session_id: str = Query(min_length=1, max_length=256)):
    return _management_call(_ARCHIVE_MANAGER.storage, session_id)


def _load_dashboard_cache_ttl() -> int:
    try:
        ttl = int(os.environ.get("ARCHIVE_DASHBOARD_CACHE_TTL", "30") or "30")
    except (TypeError, ValueError):
        ttl = 30
    return max(5, min(ttl, 300))


_DASHBOARD_CACHE_TTL = _load_dashboard_cache_ttl()


def _load_stats_cache_ttl() -> int:
    try:
        ttl = int(os.environ.get("ARCHIVE_STATS_CACHE_TTL", "60") or "60")
    except (TypeError, ValueError):
        ttl = 60
    return max(0, min(ttl, 300))


def _load_history_message_max_chars() -> int:
    try:
        value = int(
            os.environ.get("ARCHIVE_HISTORY_MESSAGE_MAX_CHARS", "12000") or "12000"
        )
    except (TypeError, ValueError):
        value = 12000
    return max(512, min(value, 200000))


_STATS_CACHE_TTL = _load_stats_cache_ttl()
_HISTORY_MESSAGE_MAX_CHARS = _load_history_message_max_chars()


_ALLOWED_PRAGMA_TABLES = frozenset(
    {
        "chat_history",
        "session_stats",
        "user_stats",
        "session_user_stats",
    }
)


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


@lru_cache(maxsize=4096)
def _static_cache_exists_cached(filename: str, bucket: int) -> bool:
    del bucket
    return any(
        _cached_file_magic_is_safe(directory / filename)
        for directory in _CACHE_STATIC_DIRS
    )


def _available_static_cache_url(url: str) -> str:
    url = str(url or "")
    if not url.startswith("/static/cache/"):
        return url
    filename = url.rsplit("/", 1)[-1]
    if not filename or "/" in filename or "\\" in filename:
        return ""
    # Short TTL avoids repeated disk exists() calls while still noticing cleanup.
    bucket = int(time.time() // 30)
    return url if _static_cache_exists_cached(filename, bucket) else ""


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
        condition for condition in conditions if condition and condition != "1=1"
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
            "avatar_url": _available_static_cache_url(str(row["avatar_url"] or "")),
            "platform_name": str(row["platform_name"] or ""),
        }
        for row in rows
    }


def _fetch_materialized_sender_profiles(
    db,
    user_ids: list[str],
    *,
    session_id: str = "",
) -> dict[str, dict]:
    """Resolve profiles from maintained summary tables in one indexed query."""
    if not user_ids:
        return {}
    if not _table_has_columns(
        db,
        "user_stats",
        ("user_id", "sender_name", "avatar_url", "platform_name"),
    ):
        return _fetch_latest_sender_profiles(db, user_ids, [], [])

    placeholders = ",".join("?" for _ in user_ids)
    normalized_session_id = str(session_id or "")
    if normalized_session_id:
        if normalized_session_id == "legacy:archive":
            normalized_session_id = "legacy:archive"
        rows = db.execute(
            f"""
                SELECT global_stats.user_id,
                       COALESCE(NULLIF(scoped.sender_name, ''), global_stats.sender_name, '') as sender_name,
                       COALESCE(NULLIF(scoped.avatar_url, ''), global_stats.avatar_url, '') as avatar_url,
                       COALESCE(NULLIF(scoped.platform_name, ''), global_stats.platform_name, '') as platform_name
                FROM user_stats global_stats
                LEFT JOIN session_user_stats scoped
                  ON scoped.user_id = global_stats.user_id AND scoped.session_id = ?
                WHERE global_stats.user_id IN ({placeholders})
            """,
            [normalized_session_id, *user_ids],
        ).fetchall()
    else:
        rows = db.execute(
            f"""
                SELECT user_id,
                       COALESCE(sender_name, '') as sender_name,
                       COALESCE(avatar_url, '') as avatar_url,
                       COALESCE(platform_name, '') as platform_name
                FROM user_stats
                WHERE user_id IN ({placeholders})
            """,
            user_ids,
        ).fetchall()
    return {
        str(row["user_id"]): {
            "sender_name": str(row["sender_name"] or ""),
            "avatar_url": _available_static_cache_url(str(row["avatar_url"] or "")),
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
    profile_session_id: str = "",
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
    latest_profiles = _fetch_materialized_sender_profiles(
        db,
        user_ids,
        session_id=profile_session_id,
    )
    return [
        {
            "user_id": str(row["user_id"]),
            "sender_name": latest_profiles.get(str(row["user_id"]), {}).get(
                "sender_name"
            )
            or str(row["user_id"]),
            "avatar_url": latest_profiles.get(str(row["user_id"]), {}).get(
                "avatar_url", ""
            ),
            "platform_name": latest_profiles.get(str(row["user_id"]), {}).get(
                "platform_name", ""
            ),
            "cnt": int(row["cnt"] or 0),
        }
        for row in rows
    ]


def _fetch_user_counts_from_stats(
    db,
    *,
    session_id: str = "",
    keyword: str = "",
    limit: int,
    offset: int = 0,
) -> list[dict]:
    """Read member counts and profiles from maintained summary tables."""
    table = "session_user_stats" if session_id else "user_stats"
    if not _table_has_columns(
        db,
        table,
        (
            "user_id",
            "visible_message_count",
            "sender_name",
            "avatar_url",
            "platform_name",
        ),
    ):
        return []
    conditions = [
        "user_id IS NOT NULL",
        "user_id != ''",
        "user_id != '0'",
        "visible_message_count > 0",
    ]
    params: list = []
    if session_id:
        conditions.append("session_id = ?")
        params.append(
            "legacy:archive" if session_id == "legacy:archive" else session_id
        )
    if keyword:
        conditions.append(
            "(INSTR(LOWER(COALESCE(sender_name, '')), LOWER(?)) > 0 "
            "OR INSTR(LOWER(user_id), LOWER(?)) > 0)"
        )
        params.extend([keyword, keyword])
    rows = db.execute(
        f"""
            SELECT user_id,
                   COALESCE(NULLIF(sender_name, ''), user_id) as sender_name,
                   COALESCE(avatar_url, '') as avatar_url,
                   COALESCE(platform_name, '') as platform_name,
                   visible_message_count as cnt
            FROM {table}
            WHERE {" AND ".join(conditions)}
            ORDER BY visible_message_count DESC, user_id ASC
            LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [
        {
            "user_id": str(row["user_id"]),
            "sender_name": str(row["sender_name"] or row["user_id"]),
            "avatar_url": _available_static_cache_url(str(row["avatar_url"] or "")),
            "platform_name": str(row["platform_name"] or ""),
            "cnt": int(row["cnt"] or 0),
        }
        for row in rows
    ]


def _count_users_from_stats(db, *, session_id: str = "", keyword: str = "") -> int:
    table = "session_user_stats" if session_id else "user_stats"
    conditions = [
        "user_id IS NOT NULL",
        "user_id != ''",
        "user_id != '0'",
        "visible_message_count > 0",
    ]
    params: list = []
    if session_id:
        conditions.append("session_id = ?")
        params.append(
            "legacy:archive" if session_id == "legacy:archive" else session_id
        )
    if keyword:
        conditions.append(
            "(INSTR(LOWER(COALESCE(sender_name, '')), LOWER(?)) > 0 "
            "OR INSTR(LOWER(user_id), LOWER(?)) > 0)"
        )
        params.extend([keyword, keyword])
    row = db.execute(
        f"SELECT COUNT(*) as cnt FROM {table} WHERE {' AND '.join(conditions)}",
        params,
    ).fetchone()
    return int(row["cnt"] or 0) if row else 0


def _fetch_latest_session_profiles(
    db, session_ids: list[str], bot_ids: list[str] = None
) -> dict[str, dict]:
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
            "avatar_url": _available_static_cache_url(str(row["avatar_url"] or "")),
            "platform_name": str(row["platform_name"] or ""),
        }
        for row in rows
    }


async def _close_media_upstream(
    response: aiohttp.ClientResponse | None,
    client: aiohttp.ClientSession | None,
):
    if response is not None:
        with suppress(Exception):
            response.release()
            await response.wait_for_close()
    if client is not None:
        with suppress(Exception):
            await client.close()


def _parse_content_range(value: str, *, max_total_bytes: int) -> tuple[int, int, int]:
    """Validate a single byte range and its complete object size."""
    match = re.fullmatch(
        r"bytes\s+(\d+)-(\d+)/(\d+)",
        str(value or "").strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("missing or malformed Content-Range")
    start, end, total = (int(part) for part in match.groups())
    if total <= 0 or start > end or end >= total:
        raise ValueError("invalid Content-Range bounds")
    if total > max_total_bytes:
        raise OverflowError("partial response belongs to oversized media")
    return start, end, total


def _session_display_name(
    session_id: str, message_type: str, session_name: str = "", sender_name: str = ""
) -> str:
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
    text = sanitize_cq_media_codes(str(message or ""))
    for pattern, label in (
        (r"\[CQ:image(?:,[^\]]*)?\]", "[图片]"),
        (r"\[CQ:video(?:,[^\]]*)?\]", "[视频]"),
        (r"\[CQ:record(?:,[^\]]*)?\]", "[语音]"),
        (r"\[CQ:file(?:,[^\]]*)?\]", "[文件]"),
    ):
        text = re.sub(pattern, label, text, flags=re.IGNORECASE)
    text = " ".join(text.split())
    return text[: max_len - 1] + "…" if len(text) > max_len else text


def _dashboard_range_days(range_key: str) -> tuple[str, int]:
    normalized = str(range_key or "30d").lower().strip()
    mapping = {"1d": 1, "7d": 7, "30d": 30}
    if normalized not in mapping:
        normalized = "30d"
    return normalized, mapping[normalized]


def _compute_dashboard(range_key: str) -> dict:
    db = None
    try:
        db = get_db_connection()
        has_media_columns = _table_has_columns(
            db, "chat_history", ("has_image", "has_video", "msg_kind")
        )
        if not has_media_columns:
            logger.warning(
                "Dashboard media columns are missing; attempting one schema refresh before serving aggregates."
            )
            try:
                db.close()
                db = None
                init_db()
            except Exception as e:
                logger.warning(
                    f"Dashboard schema refresh failed; media aggregates will be omitted: {e}"
                )
            finally:
                if db is None:
                    db = get_db_connection()
            has_media_columns = _table_has_columns(
                db, "chat_history", ("has_image", "has_video", "msg_kind")
            )

        local_now = datetime.datetime.now().astimezone()
        local_today_midnight = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        today_start = int(local_today_midnight.timestamp())
        local_offset = (
            int(local_now.utcoffset().total_seconds()) if local_now.utcoffset() else 0
        )
        range_key, trend_days = _dashboard_range_days(range_key)
        trend_start_dt = local_today_midnight - datetime.timedelta(days=trend_days - 1)
        trend_start = int(trend_start_dt.timestamp())

        # Measure Summary Time
        t_summary_start = time.perf_counter()
        try:
            row = db.execute(
                "SELECT COALESCE(SUM(message_count), 0) as c, COUNT(*) as sessions FROM session_stats"
            ).fetchone()
            total_messages = int(row["c"] or 0) if row else 0
            total_sessions = int(row["sessions"] or 0) if row else 0
        except Exception:
            total_messages = int(
                db.execute("SELECT COUNT(*) as c FROM chat_history").fetchone()["c"]
                or 0
            )
            total_sessions = int(
                db.execute("""
                SELECT COUNT(*) as c FROM (
                    SELECT COALESCE(NULLIF(session_id, ''), 'legacy:archive')
                    FROM chat_history
                    GROUP BY COALESCE(NULLIF(session_id, ''), 'legacy:archive')
                )
            """).fetchone()["c"]
                or 0
            )

        today_messages = int(
            db.execute(
                "SELECT COUNT(*) as c FROM chat_history WHERE timestamp >= ?",
                [today_start],
            ).fetchone()["c"]
            or 0
        )

        if has_media_columns:
            total_images = int(
                db.execute(
                    "SELECT COUNT(*) as c FROM chat_history WHERE has_image = 1"
                ).fetchone()["c"]
                or 0
            )
            total_videos = int(
                db.execute(
                    "SELECT COUNT(*) as c FROM chat_history WHERE has_video = 1"
                ).fetchone()["c"]
                or 0
            )
            type_rows = db.execute("""
                SELECT COALESCE(NULLIF(msg_kind, ''), 'text') as kind, COUNT(*) as cnt
                FROM chat_history
                GROUP BY msg_kind
            """).fetchall()
        else:
            logger.warning(
                "Dashboard media columns unavailable; skipping expensive LIKE fallback scan."
            )
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
            kind = str(
                r_row.get("kind", "text")
                if isinstance(r_row, dict)
                else r_row["kind"] or "text"
            )
            count = int(
                r_row.get("cnt", 0) if isinstance(r_row, dict) else r_row["cnt"] or 0
            )
            if kind == "text":
                text_count += count
            elif kind == "image":
                image_count += count
            else:
                other_count += count

        message_type_distribution = []
        if text_count > 0:
            message_type_distribution.append(
                {"type": "text", "name": "文本", "count": text_count}
            )
        if image_count > 0:
            message_type_distribution.append(
                {"type": "image", "name": "图片", "count": image_count}
            )
        if other_count > 0:
            message_type_distribution.append(
                {"type": "other", "name": "其他", "count": other_count}
            )
        message_type_distribution.sort(key=lambda item: item["count"], reverse=True)
        time_type_ms = round((time.perf_counter() - t_type_start) * 1000, 2)

        # Measure Trend Time
        t_trend_start = time.perf_counter()
        if range_key == "1d":
            trend_start_dt = local_now - datetime.timedelta(hours=23)
            trend_start_dt = trend_start_dt.replace(minute=0, second=0, microsecond=0)
            trend_start = int(trend_start_dt.timestamp())

            trend_rows = db.execute(
                f"""
                SELECT CAST((timestamp + {local_offset}) / 3600 AS INTEGER) as hour_bucket, COUNT(*) as cnt
                FROM chat_history
                WHERE timestamp >= ?
                GROUP BY hour_bucket
                ORDER BY hour_bucket
            """,
                [trend_start],
            ).fetchall()
            trend_map = {
                int(row["hour_bucket"]): int(row["cnt"] or 0) for row in trend_rows
            }
            activity_trend = []
            for idx in range(24):
                hour_dt = trend_start_dt + datetime.timedelta(hours=idx)
                bucket = int((int(hour_dt.timestamp()) + local_offset) / 3600)
                activity_trend.append(
                    {
                        "date": hour_dt.strftime("%m-%d %H:00"),
                        "count": trend_map.get(bucket, 0),
                    }
                )
        else:
            trend_rows = db.execute(
                f"""
                SELECT CAST((timestamp + {local_offset}) / 86400 AS INTEGER) as day_bucket, COUNT(*) as cnt
                FROM chat_history
                WHERE timestamp >= ?
                GROUP BY day_bucket
                ORDER BY day_bucket
            """,
                [trend_start],
            ).fetchall()
            trend_map = {
                int(row["day_bucket"]): int(row["cnt"] or 0) for row in trend_rows
            }
            activity_trend = []
            for idx in range(trend_days):
                day_dt = trend_start_dt + datetime.timedelta(days=idx)
                bucket = int((int(day_dt.timestamp()) + local_offset) / 86400)
                activity_trend.append(
                    {
                        "date": day_dt.strftime("%Y-%m-%d"),
                        "count": trend_map.get(bucket, 0),
                    }
                )
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
            top_groups.append(
                {
                    "session_id": sid,
                    "session_name": row["session_name"] or "",
                    "name": _session_display_name(
                        sid, mt, row["session_name"] or "", row["sender_name"] or ""
                    ),
                    "message_type": mt,
                    "message_count": int(row["message_count"] or 0),
                    "last_time": int(row["last_time"] or 0),
                    "last_msg": _message_preview(row["last_msg"] or "", 100),
                }
            )
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
            "total_db_time_ms": round(
                time_summary_ms + time_type_ms + time_trend_ms + time_groups_ms, 2
            ),
            "cache_hit": False,
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


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Probe the HttpOnly session without generating an expected 401."""
    token = request.cookies.get("archive_auth", "")
    return JSONResponse(
        content={
            "success": True,
            "configured": bool(API_KEY),
            "authenticated": bool(API_KEY and _validate_session_token(token)),
        }
    )


@app.post("/api/auth/verify")
async def verify_auth(request: Request):
    client_key = _auth_client_key(request)
    retry_after = _auth_retry_after(client_key)
    if retry_after:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={"success": False, "message": "Too many failed login attempts"},
        )

    try:
        data = await _read_limited_json_body(request)
    except HTTPException as exc:
        if exc.status_code in (400, 413):
            retry_after = _record_auth_failure(client_key)
            if retry_after:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                    content={
                        "success": False,
                        "message": "Too many failed login attempts",
                    },
                )
        raise
    provided_key = str(data.get("api_key", "")).strip()
    if not API_KEY:
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": "API Key is not configured"},
        )
    if provided_key and _constant_time_text_equal(provided_key, API_KEY):
        _clear_auth_failures(client_key)
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
    retry_after = _record_auth_failure(client_key)
    if retry_after:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={"success": False, "message": "Too many failed login attempts"},
        )
    return JSONResponse(
        status_code=401, content={"success": False, "message": "Invalid API Key"}
    )


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
    record_id: int = Query(0, ge=0),
    time_start: int = Query(0, ge=0),
    time_end: int = Query(0, ge=0),
    page: int = Query(1, ge=1, le=100000),
    limit: int = Query(50, ge=1, le=200),
    cursor: int = Query(0, ge=0),
    include_total: bool = Query(False),
    full_message: bool = Query(False),
    search_mode: Literal["literal", "terms"] = Query("literal"),
):
    db = None
    try:
        db = get_db_connection()

        conditions = ["1=1"]
        params = []

        if keyword:
            add_message_search_condition(
                db, conditions, params, keyword, search_mode=search_mode
            )
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
        if record_id:
            conditions.append("id = ?")
            params.append(record_id)
        if time_start:
            conditions.append("timestamp >= ?")
            params.append(time_start)
        if time_end:
            conditions.append("timestamp <= ?")
            params.append(time_end)

        where_cl = " WHERE " + " AND ".join(conditions)
        has_avatar = _table_has_columns(db, "chat_history", ("avatar_url",))
        has_platform = _table_has_columns(
            db, "chat_history", ("platform_id", "platform_name")
        )
        avatar_select = "avatar_url" if has_avatar else "'' as avatar_url"
        platform_select = (
            "platform_id, platform_name"
            if has_platform
            else "'' as platform_id, '' as platform_name"
        )
        if full_message:
            select_columns = f"""
                id, user_id, sender_name, message,
                COALESCE(archive_text_length(message), 0) as message_length,
                0 as message_truncated,
                timestamp, session_id, message_type, session_name, msg_id, is_recalled,
                {avatar_select}, {platform_select}
            """
            select_params = []
        else:
            select_columns = f"""
                id, user_id, sender_name,
                CASE
                    WHEN COALESCE(archive_text_length(message), 0) > ?
                    THEN archive_text_prefix(message, ?)
                    ELSE message
                END as message,
                COALESCE(archive_text_length(message), 0) as message_length,
                CASE WHEN COALESCE(archive_text_length(message), 0) > ? THEN 1 ELSE 0 END as message_truncated,
                timestamp, session_id, message_type, session_name, msg_id, is_recalled,
                {avatar_select}, {platform_select}
            """
            select_params = [
                _HISTORY_MESSAGE_MAX_CHARS,
                _HISTORY_MESSAGE_MAX_CHARS,
                _HISTORY_MESSAGE_MAX_CHARS,
            ]

        query_conditions = list(conditions)
        query_params = list(params)

        fetch_limit = limit + 1
        if cursor > 0:
            query_conditions.append("id < ?")
            query_params.append(cursor)
            where_cl_cursor = " WHERE " + " AND ".join(query_conditions)
            query = f"SELECT {select_columns} FROM chat_history {where_cl_cursor} ORDER BY id DESC LIMIT ?"
            query_params.append(fetch_limit)
        else:
            # Subquery pagination optimization to avoid performance degradation on deep offsets
            query = f"SELECT {select_columns} FROM chat_history WHERE id IN (SELECT id FROM chat_history {where_cl} ORDER BY id DESC LIMIT ? OFFSET ?) ORDER BY id DESC"
            offset = (page - 1) * limit
            query_params.extend([fetch_limit, offset])

        records = db.execute(query, [*select_params, *query_params]).fetchall()
        has_more = len(records) > limit
        records = records[:limit]
        if records:
            next_cursor = records[-1]["id"]
        else:
            next_cursor = 0

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
        elif (
            session_id
            and not keyword
            and not user_id
            and not record_id
            and not time_start
            and not time_end
        ):
            stats_id = (
                "legacy:archive" if session_id == "legacy:archive" else str(session_id)
            )
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
        # Extract referenced user IDs from CQ:at codes in messages to resolve their nicknames
        import re

        referenced_user_ids = set()
        for row in records:
            msg_text = str(row.get("message") or "")
            for match in re.finditer(r"\[CQ:at,qq=(\d+)\]", msg_text):
                referenced_user_ids.add(match.group(1))

        # Resolve current senders and referenced users from maintained summary
        # tables. This replaces two ROW_NUMBER() scans over chat_history.
        all_profile_user_ids = sorted(record_user_ids_set | referenced_user_ids)
        user_profiles = _fetch_materialized_sender_profiles(
            db,
            all_profile_user_ids,
            session_id=session_id,
        )

        processed_records = []
        for r in records:
            item = dict(r)
            uid = str(item.get("user_id", "")).strip()
            sname = str(item.get("sender_name", "")).strip()
            msg_type = str(item.get("message_type", "")).strip().lower()
            profile = user_profiles.get(uid, {})
            item["avatar_url"] = _available_static_cache_url(item.get("avatar_url", ""))
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
async def proxy_image(request: Request, url: str = Query(..., max_length=4096)):
    """
    代理媒体请求，解决 NTQQ 域名 (multimedia.nt.qq.com.cn) 的跨域与 Referer 限制。
    支持图片和视频的代理流式传输。
    已修复：域名提取 SSRF 漏洞 与 &amp; 实体字符容错。
    """
    # 容错：替换转义的 &amp;
    url = url.replace("&amp;", "&")

    # 安全校验：仅允许代理指定的域名（支持显式白名单及其子域名）
    try:
        _parsed_url, _hostname, _upstream_port = validate_remote_media_url(
            url,
            ALLOWED_MEDIA_DOMAINS,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid URL")
    except PermissionError as exc:
        detail = "Forbidden port" if "port" in str(exc) else "Forbidden domain"
        raise HTTPException(status_code=403, detail=detail)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        "Referer": "https://q.qq.com/",
    }
    range_header = request.headers.get("range", "").strip()
    if range_header:
        if not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header):
            raise HTTPException(status_code=416, detail="Invalid byte range")
        headers["Range"] = range_header

    resolver = PinnedPublicResolver(allow_fake_ip=load_allow_fake_ip())
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        ttl_dns_cache=0,
    )
    client = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
        auto_decompress=False,
        trust_env=False,
    )
    response = None
    try:
        response = await client.get(
            url,
            headers=headers,
            allow_redirects=False,
        )
    except Exception as e:
        await _close_media_upstream(response, client)
        logger.error(f"Media proxy open stream error for {url}: {e}")
        raise HTTPException(status_code=502, detail="Media upstream unavailable")

    if response.status not in (200, 206):
        await _close_media_upstream(response, client)
        raise HTTPException(status_code=response.status, detail="Media upstream failed")

    expected_partial_bytes = 0
    range_start = 0
    if response.status == 206:
        if not range_header:
            await _close_media_upstream(response, client)
            raise HTTPException(
                status_code=502, detail="Unexpected partial media response"
            )
        try:
            range_start, range_end, _range_total = _parse_content_range(
                response.headers.get("content-range", ""),
                max_total_bytes=MEDIA_MAX_BYTES,
            )
            expected_partial_bytes = range_end - range_start + 1
        except OverflowError:
            await _close_media_upstream(response, client)
            raise HTTPException(status_code=413, detail="Media too large")
        except ValueError:
            await _close_media_upstream(response, client)
            raise HTTPException(status_code=502, detail="Invalid upstream byte range")

    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    sniffed_prefix = b""
    if content_type in GENERIC_MEDIA_TYPES:
        if response.status == 206 and range_start != 0:
            content_type = _get_cached_sniffed_media_type(url)
            if content_type not in _SAFE_MEDIA_TYPES:
                await _close_media_upstream(response, client)
                raise HTTPException(
                    status_code=415,
                    detail="Generic media range must be validated from byte zero first",
                )
        else:
            try:
                sniffed_prefix = await read_media_prefix(response.content)
            except Exception as exc:
                await _close_media_upstream(response, client)
                logger.warning(f"Media proxy failed to read media prefix: {url}: {exc}")
                raise HTTPException(
                    status_code=502, detail="Media upstream unavailable"
                )
            sniffed = sniff_passive_media(sniffed_prefix)
            if sniffed is None:
                await _close_media_upstream(response, client)
                logger.warning(
                    f"Media proxy rejected unrecognized generic media: {url}"
                )
                raise HTTPException(status_code=415, detail="Unsupported media type")
            content_type, _sniffed_extension = sniffed
            if content_type not in _SAFE_MEDIA_TYPES:
                await _close_media_upstream(response, client)
                raise HTTPException(status_code=415, detail="Unsupported media type")
            _cache_sniffed_media_type(url, content_type)
    elif content_type not in _SAFE_MEDIA_TYPES:
        await _close_media_upstream(response, client)
        logger.warning(
            f"Media proxy rejected active/unsupported media: {url}, content-type={content_type or 'unknown'}"
        )
        raise HTTPException(status_code=415, detail="Unsupported media type")

    content_length = response.headers.get("content-length")
    if content_length:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise ValueError
            if (
                expected_partial_bytes
                and parsed_content_length != expected_partial_bytes
            ):
                await _close_media_upstream(response, client)
                raise HTTPException(
                    status_code=502, detail="Invalid upstream range length"
                )
            if not expected_partial_bytes and parsed_content_length > MEDIA_MAX_BYTES:
                await _close_media_upstream(response, client)
                logger.warning(
                    f"Media proxy rejected oversized response by content-length: {url}, size={content_length}"
                )
                raise HTTPException(status_code=413, detail="Media too large")
        except ValueError:
            if expected_partial_bytes:
                await _close_media_upstream(response, client)
                raise HTTPException(
                    status_code=502, detail="Invalid upstream range length"
                )

    async def stream_media():
        downloaded = len(sniffed_prefix)
        try:
            if sniffed_prefix:
                if expected_partial_bytes and downloaded > expected_partial_bytes:
                    yield sniffed_prefix[:expected_partial_bytes]
                    logger.warning(f"Media proxy stopped overlong range prefix: {url}")
                    return
                if downloaded > MEDIA_MAX_BYTES:
                    logger.warning(f"Media proxy stopped oversized prefix: {url}")
                    return
                yield sniffed_prefix
            async for chunk in response.content.iter_chunked(64 * 1024):
                next_downloaded = downloaded + len(chunk)
                if expected_partial_bytes and next_downloaded > expected_partial_bytes:
                    remaining = expected_partial_bytes - downloaded
                    if remaining > 0:
                        yield chunk[:remaining]
                    logger.warning(f"Media proxy stopped overlong range stream: {url}")
                    return
                downloaded = next_downloaded
                if downloaded > MEDIA_MAX_BYTES:
                    logger.warning(
                        f"Media proxy stopped oversized stream: {url}, size>{MEDIA_MAX_BYTES}"
                    )
                    return
                yield chunk
        except Exception as e:
            logger.error(f"Media proxy error for {url}: {e}")
        finally:
            await _close_media_upstream(response, client)

    downstream_headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cross-Origin-Resource-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
        "Vary": "Cookie, Range, X-API-Key",
    }
    for header_name in ("accept-ranges", "content-range", "etag", "last-modified"):
        if response.headers.get(header_name):
            downstream_headers[header_name.title()] = response.headers[header_name]

    return StreamingResponse(
        stream_media(),
        status_code=response.status,
        media_type=content_type,
        headers=downstream_headers,
    )


@lru_cache(maxsize=4096)
def _find_cached_telegram_avatar_cached(
    cache_dir_str: str, cache_key: str, bucket: int
) -> str:
    del bucket
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:32]
    cache_dirs = tuple(
        dict.fromkeys(
            [
                Path(cache_dir_str).resolve(strict=False),
                *_CACHE_STATIC_DIRS,
            ]
        )
    )
    for cache_dir in cache_dirs:
        if not cache_dir.exists():
            continue
        for prefix in ("telegram_avatar", "telegram_chat_avatar", "telegram_media"):
            for suffix in (".jpg", ".png", ".jpeg", ".webp", ".gif"):
                filename = f"{prefix}_{digest}{suffix}"
                if _cached_file_magic_is_safe(cache_dir / filename):
                    return f"/static/cache/{filename}"
    return ""


def _find_cached_telegram_avatar(cache_dir: Path, cache_key: str) -> str:
    if not cache_dir:
        return ""
    return _find_cached_telegram_avatar_cached(
        str(cache_dir), cache_key, int(time.time() // 30)
    )


@app.get("/api/sessions")
def get_sessions(request: Request):
    db = None
    try:
        db = get_db_connection()
        has_stat_profiles = _table_has_columns(
            db, "session_stats", ("avatar_url", "platform_name", "guild_avatar_url")
        )
        profile_select = (
            "avatar_url, platform_name, guild_avatar_url,"
            if has_stat_profiles
            else "'' as avatar_url, '' as platform_name, '' as guild_avatar_url,"
        )
        query = f"""
            SELECT session_id,
                   COALESCE(message_type, 'legacy') as message_type,
                   last_time,
                   last_msg,
                   sender_name,
                   session_name,
                   message_count as count,
                   {profile_select}
                   1 as _from_stats
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
                       0 as count,
                       '' as avatar_url,
                       '' as platform_name,
                       '' as guild_avatar_url,
                       0 as _from_stats
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

        session_profile_ids = [
            str(s["session_id"] or "legacy:archive")
            for s in sessions
            if not s.get("_from_stats")
            and not (
                str(s.get("avatar_url") or "").strip()
                and str(s.get("platform_name") or "").strip()
            )
        ]
        session_profiles = (
            _fetch_latest_session_profiles(
                db,
                session_profile_ids,
                bot_ids=bot_ids,
            )
            if session_profile_ids
            else {}
        )
        friend_profile_ids = []
        for session in sessions:
            mt = str(session.get("message_type", "") or "").lower()
            if "friend" in mt:
                sid = str(session.get("session_id", "") or "")
                friend_profile_ids.append(sid.split(":")[-1] if ":" in sid else sid)
        friend_profiles = _fetch_materialized_sender_profiles(
            db,
            [uid for uid in friend_profile_ids if uid],
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
            profile_avatar = _available_static_cache_url(
                s.get("guild_avatar_url")
                or s.get("avatar_url")
                or profile.get("avatar_url", "")
            )
            platform_name = str(
                s.get("platform_name") or profile.get("platform_name", "")
            ).lower()
            s_platform = s_id.split(":", 1)[0].lower() if ":" in s_id else platform_name
            s["platform_name"] = platform_name or s_platform

            # Check for cached Telegram avatar directly on disk first to avoid speaker avatar lock
            if s_platform == "telegram":
                if s["message_type"] in [
                    "group",
                    "GroupMessage",
                    "channel",
                    "ChannelMessage",
                ]:
                    chat_id = s_id.split(":")[-1] if ":" in s_id else s_id
                    cached_avatar = _find_cached_telegram_avatar(
                        cache_static_dir, f"telegram:chat:{chat_id}"
                    )
                    if cached_avatar:
                        profile_avatar = cached_avatar
                elif s["message_type"] in ["friend", "FriendMessage"]:
                    user_id = s_id.split(":")[-1] if ":" in s_id else s_id
                    cached_avatar = _find_cached_telegram_avatar(
                        cache_static_dir, f"telegram:user:{user_id}"
                    )
                    if cached_avatar:
                        profile_avatar = cached_avatar

            if s["message_type"] in [
                "group",
                "GroupMessage",
                "channel",
                "ChannelMessage",
            ]:
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
                    profile_avatar = _available_static_cache_url(
                        friend_profile.get("avatar_url", "")
                    )
                    platform_name = (
                        platform_name
                        or str(friend_profile.get("platform_name", "")).lower()
                    )
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
                elif (
                    s["sender_name"]
                    and s["sender_name"].strip()
                    and s["sender_name"].strip().lower() != "bot"
                ):
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
    # Kept as an accepted query parameter for older clients; dashboard data no
    # longer contains a recent slice that depends on this value.
    del recent_limit
    cache_key = range_key
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
        data = _compute_dashboard(range_key)
        data["cached"] = False
        with _DASHBOARD_CACHE_LOCK:
            _set_ttl_cache(
                _DASHBOARD_CACHE,
                cache_key,
                now + _DASHBOARD_CACHE_TTL,
                data,
                _DASHBOARD_CACHE_MAX_ENTRIES,
            )
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
    # Accepted for compatibility with older frontends. Session type is already
    # encoded by session_id and this value never changes the query.
    del is_private
    db = None
    try:
        cache_key = json.dumps(
            [session_id, user_id, int(time_start or 0), int(time_end or 0)],
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

        # Browsers commonly send a range ending at "now" even when that bound
        # already covers the whole archive.  Removing redundant global bounds
        # lets the materialized session/user statistics serve the request.
        if time_start:
            first_row = db.execute(
                """
                SELECT timestamp FROM chat_history
                WHERE timestamp IS NOT NULL
                ORDER BY timestamp ASC LIMIT 1
                """
            ).fetchone()
            if first_row and time_start <= int(first_row["timestamp"]):
                time_start = 0
        if time_end:
            last_row = db.execute(
                """
                SELECT timestamp FROM chat_history
                WHERE timestamp IS NOT NULL
                ORDER BY timestamp DESC LIMIT 1
                """
            ).fetchone()
            if last_row and time_end >= int(last_row["timestamp"]):
                time_end = 0

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
        has_media_columns = _table_has_columns(
            db, "chat_history", ("has_image", "has_video", "msg_kind")
        )

        local_now = datetime.datetime.now().astimezone()
        local_today_midnight = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
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
        if not user_id and not time_start and not time_end:
            top_rows = _fetch_user_counts_from_stats(
                db,
                session_id=session_id,
                limit=30,
            )
        else:
            top_rows = _fetch_user_counts(
                db,
                top_conditions,
                params,
                30,
                profile_session_id=session_id,
            )
        top_users = []
        for r in top_rows:
            top_users.append(
                {
                    "user_id": r["user_id"],
                    "sender_name": r["sender_name"],
                    "avatar_url": r.get("avatar_url", ""),
                    "platform_name": r.get("platform_name", ""),
                    "count": r["cnt"],
                }
            )

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
                               WHEN message NOT LIKE '[CQ:%' AND message NOT LIKE '<Event%'
                               THEN archive_text_length(message)
                               ELSE NULL
                           END) as avg_len,
                           {image_expr} as image_count
                    FROM chat_history {where_cl}
                """,
                params,
            ).fetchone()
            active_days = int(detail_row["days"] or 0) if detail_row else 0
            avg_text_len = (
                round(detail_row["avg_len"])
                if detail_row and detail_row["avg_len"]
                else 0
            )

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
                _set_ttl_cache(
                    _STATS_CACHE,
                    cache_key,
                    now + _STATS_CACHE_TTL,
                    payload,
                    _STATS_CACHE_MAX_ENTRIES,
                )
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
    include_total: bool = Query(False),
):
    db = None
    try:
        db = get_db_connection()

        if time_start:
            first_row = db.execute(
                """
                SELECT timestamp FROM chat_history
                WHERE timestamp IS NOT NULL
                ORDER BY timestamp ASC LIMIT 1
                """
            ).fetchone()
            if first_row and time_start <= int(first_row["timestamp"]):
                time_start = 0
        if time_end:
            last_row = db.execute(
                """
                SELECT timestamp FROM chat_history
                WHERE timestamp IS NOT NULL
                ORDER BY timestamp DESC LIMIT 1
                """
            ).fetchone()
            if last_row and time_end >= int(last_row["timestamp"]):
                time_end = 0

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
                keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            conditions.append(
                "(sender_name LIKE ? ESCAPE '\\' OR user_id LIKE ? ESCAPE '\\')"
            )
            params.extend([f"%{safe_keyword}%", f"%{safe_keyword}%"])

        where_cl = " WHERE " + " AND ".join(conditions)
        use_materialized_stats = bool(session_id) and not time_start and not time_end

        total = None
        if include_total:
            if use_materialized_stats:
                total = _count_users_from_stats(
                    db,
                    session_id=session_id,
                    keyword=keyword,
                )
            else:
                count_sql = f"""
                    SELECT COUNT(*) as total FROM (
                        SELECT user_id FROM chat_history {where_cl} GROUP BY user_id
                    )
                """
                total = db.execute(count_sql, params).fetchone()["total"]

        # Fetch one extra row to infer has_more without a second GROUP BY count.
        if use_materialized_stats:
            rows = _fetch_user_counts_from_stats(
                db,
                session_id=session_id,
                keyword=keyword,
                limit=limit + 1,
                offset=offset,
            )
        else:
            rows = _fetch_user_counts(
                db,
                conditions,
                params,
                limit + 1,
                offset,
                profile_session_id=session_id,
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
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
                    "total": total
                    if total is not None
                    else offset + len(members) + (1 if has_more else 0),
                    "total_exact": bool(include_total),
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more
                    if total is None
                    else offset + len(members) < total,
                },
            }
        )
    except Exception as e:
        logger.error(f"WebUI get_members error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if db:
            db.close()


def _extension_path_is_trusted(path: Path, *, expect_directory: bool) -> bool:
    """Only load extension code owned by this service (or root) and not shared-writable."""
    try:
        if path.is_symlink():
            return False
        path_stat = path.stat()
    except OSError:
        return False
    expected_type = stat.S_ISDIR if expect_directory else stat.S_ISREG
    if not expected_type(path_stat.st_mode):
        return False
    if path_stat.st_uid not in {0, os.geteuid()}:
        return False
    return not bool(path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _load_custom_apis():
    try:
        import importlib.util

        data_dir = get_data_dir()
        ext_dir = data_dir.parent.parent / "chat_archive_ext"
        if not ext_dir.exists():
            ext_dir = (
                Path(__file__).resolve().parent.parent.parent.parent
                / "chat_archive_ext"
            )

        if ext_dir.exists():
            try:
                if not _extension_path_is_trusted(ext_dir, expect_directory=True):
                    logger.error(
                        "Chat Archive Ext: 拒绝加载自定义 API 扩展。"
                        f"扩展目录 {ext_dir} 必须由当前用户或 root 拥有、不可为符号链接，"
                        "且不可由组/其他用户写入。"
                    )
                    return
                resolved_ext_dir = ext_dir.resolve(strict=True)
            except Exception as e:
                logger.error(f"Chat Archive Ext: 读取扩展目录属性失败: {e}")
                return

            for f in ext_dir.glob("*.py"):
                if f.name.startswith("_"):
                    continue
                try:
                    if f.is_symlink():
                        logger.error(
                            f"Chat Archive Ext: 拒绝加载符号链接扩展 {f.name}。"
                        )
                        continue
                    resolved_f = f.resolve(strict=True)
                    try:
                        resolved_f.relative_to(resolved_ext_dir)
                    except ValueError:
                        logger.error(f"Chat Archive Ext: 拒绝加载越界扩展 {f.name}。")
                        continue
                    if not _extension_path_is_trusted(
                        resolved_f, expect_directory=False
                    ):
                        logger.error(
                            f"Chat Archive Ext: 拒绝加载自定义 API {f.name}。"
                            "文件必须由当前用户或 root 拥有，且不可由组/其他用户写入。"
                        )
                        continue

                    spec = importlib.util.spec_from_file_location(
                        f.stem, str(resolved_f)
                    )
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        if hasattr(module, "register"):
                            module.register(app, get_db_connection)
                            logger.info(
                                f"Chat Archive Ext: 成功加载自定义 API [{f.name}]"
                            )
                except Exception as ex:
                    logger.error(
                        f"Chat Archive Ext: 加载自定义 API {f.name} 失败: {ex}"
                    )
    except Exception as e:
        logger.error(f"Chat Archive Ext: 扫描自定义 API 失败: {e}")


# NOTE: Custom APIs are loaded in AdminServer.start() after API_KEY is configured.
# For standalone usage (`python server.py`), see __main__ block below.
_custom_apis_loaded = False
_WEB_STARTUP_TIMEOUT_SECONDS = 10.0
_WEB_STARTUP_JOIN_SECONDS = 2.0
_WEB_SHUTDOWN_JOIN_SECONDS = 5.0


class AdminServer:
    def __init__(
        self,
        plugin_instance,
        host: str = "127.0.0.1",
        port: int = 8090,
        api_key: str = "",
        cache_dir: Path = None,
    ):
        self.plugin = plugin_instance
        self.cache_dir = (
            Path(cache_dir).resolve(strict=False) if cache_dir else cache_static_dir
        )
        self.schema_ready = False
        self.host = os.environ.get("ARCHIVE_HOST", "").strip() or host
        try:
            self.port = int(os.environ.get("ARCHIVE_PORT", "").strip() or port)
        except (TypeError, ValueError):
            self.port = 8090
        self.api_key = str(api_key or "").strip()

        self.config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            timeout_graceful_shutdown=3,
        )
        self.server = uvicorn.Server(self.config)
        self.thread = None
        self._startup_error = None

    def _apply_runtime_state(self) -> None:
        """Apply this instance only when it is about to own the shared app."""
        global cache_static_dir, _CACHE_STATIC_DIRS

        app.state.plugin = self.plugin
        app.state.schema_ready = self.schema_ready

        next_cache_dirs = tuple(
            dict.fromkeys(
                (
                    self.cache_dir,
                    legacy_cache_static_dir.resolve(strict=False),
                )
            )
        )
        if cache_static_dir != self.cache_dir or _CACHE_STATIC_DIRS != next_cache_dirs:
            cache_static_dir = self.cache_dir
            _ARCHIVE_MANAGER.cache_dir = self.cache_dir
            _CACHE_STATIC_DIRS = next_cache_dirs
            _static_cache_exists_cached.cache_clear()
            _find_cached_telegram_avatar_cached.cache_clear()

        _refresh_runtime_policy()
        resolved_key = os.environ.get("ARCHIVE_API_KEY", "").strip() or self.api_key
        _set_api_key(resolved_key)

    async def start(self):
        """Start Uvicorn and wait until its listening socket is ready.

        Raises:
            RuntimeError: If the server thread exits or does not become ready.
        """
        if self.thread and self.thread.is_alive():
            if self.server.started:
                return
            raise RuntimeError("WebUI server thread is already running but not ready")
        self.thread = None
        if (
            getattr(self.server, "started", False)
            or getattr(self.server, "should_exit", False)
            or getattr(self.server, "force_exit", False)
        ):
            self.server = uvicorn.Server(self.config)

        self._apply_runtime_state()
        run_server = self.server
        run_server.should_exit = False
        run_server.force_exit = False
        self._startup_error = None

        def _run():
            try:
                asyncio.run(run_server.serve())
            except BaseException as e:
                if not run_server.should_exit:
                    self._startup_error = e

        run_thread = threading.Thread(target=_run, daemon=True)
        self.thread = run_thread
        run_thread.start()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _WEB_STARTUP_TIMEOUT_SECONDS
        while loop.time() < deadline:
            if run_server.started:
                break
            if self._startup_error is not None or not run_thread.is_alive():
                break
            await asyncio.sleep(0.05)

        if not run_server.started:
            run_server.should_exit = True
            if run_thread.is_alive():
                await asyncio.to_thread(
                    run_thread.join,
                    _WEB_STARTUP_JOIN_SECONDS,
                )
            if run_thread.is_alive():
                run_server.force_exit = True
                await asyncio.to_thread(
                    run_thread.join,
                    _WEB_STARTUP_JOIN_SECONDS,
                )
            error = self._startup_error
            detail = f": {error}" if error else ""
            if run_thread.is_alive():
                # Keep both handles intact so stop() can still reclaim a
                # lifespan that ignored the startup cancellation signal.
                raise RuntimeError(
                    f"WebUI failed to listen on {self.host}:{self.port}{detail}; "
                    "server thread is still running"
                )
            self.thread = None
            self.server = uvicorn.Server(self.config)
            raise RuntimeError(
                f"WebUI failed to listen on {self.host}:{self.port}{detail}"
            )

        # Load custom extension APIs after server starts, when API_KEY is set
        global _custom_apis_loaded
        if not _custom_apis_loaded:
            _load_custom_apis()
            _custom_apis_loaded = True

        logger.info(
            f"Chat Archive WebUI startup requested on http://{self.host}:{self.port}"
        )

    async def stop(self):
        """Stop Uvicorn and ensure its listening socket is released.

        Raises:
            RuntimeError: If the server thread survives graceful and forced shutdown.
        """
        thread = self.thread
        run_server = self.server
        if thread is None:
            if getattr(run_server, "started", False):
                self.server = uvicorn.Server(self.config)
            return

        run_server.should_exit = True
        if thread.is_alive():
            await asyncio.to_thread(
                thread.join,
                _WEB_SHUTDOWN_JOIN_SECONDS,
            )
        if thread.is_alive():
            logger.warning(
                "Chat Archive WebUI graceful shutdown timed out; forcing exit."
            )
            run_server.force_exit = True
            await asyncio.to_thread(
                thread.join,
                _WEB_SHUTDOWN_JOIN_SECONDS,
            )
        if thread.is_alive():
            raise RuntimeError(
                "Chat Archive WebUI thread did not stop; port may still be in use"
            )

        self.thread = None
        self._startup_error = None
        self.server = uvicorn.Server(self.config)
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
                with open(config_path, encoding="utf-8-sig") as fh:
                    cfg_data = json.load(fh)
                    API_KEY = str(
                        cfg_data.get("web_server", {}).get("api_key", "") or ""
                    ).strip()
        except Exception:
            pass
    if not API_KEY:
        # If API_KEY is still not set, raise RuntimeError to prevent unauthenticated public internet exposure!
        raise RuntimeError(
            "CRITICAL SECURITY ERROR: api_key is not configured in environment (ARCHIVE_API_KEY) or config file! Standalone web server cannot start in unauthenticated mode."
        )

    _load_custom_apis()
    host = os.environ.get("ARCHIVE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("ARCHIVE_PORT", "8090"))
    except ValueError:
        port = 8090
    uvicorn.run(app, host=host, port=port)
