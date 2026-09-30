from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from astrbot.api import logger

PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_NAME = "astrbot_plugin_chat_archive"

DEFAULT_ALLOWED_MEDIA_DOMAINS = {
    "multimedia.nt.qq.com.cn",
    "gchat.qpic.cn",
    "q.qlogo.cn",
    "p.qlogo.cn",
    "q1.qlogo.cn",
    "gxh.vip.qq.com",
}
DEFAULT_MAX_MEDIA_BYTES = 50 * 1024 * 1024


def expand_path(path_value: str, base_dir: Path | None = None) -> Path:
    """Expand ~, environment variables and relative paths consistently."""
    expanded = os.path.expandvars(str(path_value)).strip()
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = (base_dir or PLUGIN_DIR) / path
    return path.resolve()


def get_data_dir() -> Path:
    env_data_dir = os.environ.get("ARCHIVE_DATA_DIR", "").strip()
    if env_data_dir:
        return expand_path(env_data_dir, PLUGIN_DIR)
    try:
        from astrbot.api.star import StarTools

        # Helpers are not registered Star modules: caller inference fails here.
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME)).expanduser().resolve()
    except (ImportError, AttributeError, TypeError):
        # Older AstrBot versions and the standalone Web process use the same
        # persistent root, never the replaceable plugin checkout.
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            root = Path(get_astrbot_data_path())
        except ImportError:
            if PLUGIN_DIR.parent.name != "plugins":
                raise RuntimeError("Set ARCHIVE_DATA_DIR when running outside AstrBot data/plugins")
            root = PLUGIN_DIR.parent.parent
        data_dir = (root / "plugin_data" / PLUGIN_NAME).resolve()

    legacy = PLUGIN_DIR / "data"
    if legacy.resolve() != data_dir and legacy.exists() and any(legacy.iterdir()):
        raise RuntimeError(
            f"Legacy archive data found at {legacy}. Stop all archive writers and "
            f"back up this directory before moving its contents to {data_dir}. "
            "Do not overwrite an existing destination or upgrade/reinstall the "
            "plugin until migration is complete. See README migration instructions."
        )
    return data_dir


def get_static_cache_dir() -> Path:
    return get_data_dir() / "web_cache"


def get_config_path() -> Path:
    env_config_path = os.environ.get("ARCHIVE_CONFIG_PATH", "").strip()
    if env_config_path:
        return expand_path(env_config_path, PLUGIN_DIR)

    data_dir = get_data_dir()
    config_path = data_dir.parent.parent / "config" / "astrbot_plugin_chat_archive_config.json"
    if not config_path.exists():
        config_path = PLUGIN_DIR.parent.parent / "config" / "astrbot_plugin_chat_archive_config.json"
    return config_path


@lru_cache(maxsize=8)
def _load_plugin_config_cached(path: str, mtime_ns: int) -> dict[str, Any]:
    del mtime_ns
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read chat archive config: {e}")
        return {}


def load_plugin_config() -> dict[str, Any]:
    config_path = get_config_path()
    try:
        mtime_ns = config_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = -1
    return _load_plugin_config_cached(str(config_path), mtime_ns)


def get_config_section(section: str) -> dict[str, Any]:
    value = load_plugin_config().get(section, {})
    return value if isinstance(value, dict) else {}


def load_db_path() -> str:
    env_db_path = os.environ.get("ARCHIVE_DB_PATH", "").strip()
    data_dir = get_data_dir()
    if env_db_path:
        return str(expand_path(env_db_path, data_dir))

    custom_path = str(get_config_section("basic").get("db_path", "")).strip()
    if custom_path:
        return str(expand_path(custom_path, data_dir))
    return str(expand_path(str(data_dir / "chat_history.db"), data_dir))


def load_sqlite_journal_mode() -> str:
    allowed_modes = {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}
    mode = os.environ.get("ARCHIVE_SQLITE_JOURNAL_MODE", "").strip().upper()
    if not mode:
        mode = str(get_config_section("basic").get("sqlite_journal_mode", "WAL")).strip().upper()
    mode = mode or "WAL"
    if mode not in allowed_modes:
        logger.warning(f"Unsupported SQLite journal mode '{mode}', fallback to WAL.")
        mode = "WAL"
    return mode


def load_sqlite_pool_size() -> int:
    value = os.environ.get("ARCHIVE_SQLITE_MAX_CONNECTIONS", "").strip()
    if not value:
        value = str(get_config_section("basic").get("sqlite_max_connections", "")).strip()
    try:
        pool_size = int(value or 10)
    except (TypeError, ValueError):
        pool_size = 10
    return max(2, min(pool_size, 64))


def load_api_key() -> str:
    env_key = os.environ.get("ARCHIVE_API_KEY", "").strip()
    if env_key:
        return env_key
    return str(get_config_section("web_server").get("api_key", "") or "")


def load_media_max_bytes() -> int:
    value = os.environ.get("ARCHIVE_MEDIA_MAX_MB", "").strip()
    if not value:
        value = get_config_section("basic").get("media_max_mb", 50)
    try:
        mb = int(value)
    except (TypeError, ValueError):
        mb = 50
    mb = max(1, min(mb, 200))
    return mb * 1024 * 1024


def load_allowed_media_domains() -> frozenset[str]:
    env_domains = os.environ.get("ARCHIVE_ALLOWED_MEDIA_DOMAINS", "").strip()
    if env_domains:
        domains: Any = [part.strip() for part in env_domains.split(",")]
    else:
        domains = get_config_section("basic").get("allowed_media_domains", [])

    if isinstance(domains, str):
        domains = [part.strip() for part in domains.split(",")]
    cleaned = {
        str(domain).strip().lower().rstrip(".")
        for domain in domains
        if str(domain).strip()
    }
    return frozenset(cleaned or DEFAULT_ALLOWED_MEDIA_DOMAINS)


def load_allow_fake_ip() -> bool:
    env_val = os.environ.get("ARCHIVE_ALLOW_FAKE_IP", "").strip().lower()
    if env_val:
        return env_val in ("1", "true", "yes", "on")
    return bool(get_config_section("basic").get("allow_fake_ip", True))
