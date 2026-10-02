from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import stat
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
    "cdn.discordapp.com",
}


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

    # Installed plugins live at <astrbot-data>/plugins/<plugin>. Deriving the
    # sibling plugin_data directory is independent of the process cwd, unlike
    # AstrBot's generic root helper used by standalone WebUI processes.
    if PLUGIN_DIR.parent.name == "plugins" and PLUGIN_DIR.parent.parent.name == "data":
        return (PLUGIN_DIR.parent.parent / "plugin_data" / PLUGIN_NAME).resolve()

    configured_root = os.environ.get("ASTRBOT_ROOT", "").strip()
    if configured_root:
        return (
            Path(configured_root).expanduser() / "data" / "plugin_data" / PLUGIN_NAME
        ).resolve()

    if not Path.cwd().resolve().is_relative_to(PLUGIN_DIR):
        try:
            from astrbot.api.star import StarTools

            # Passing the plugin name is intentional. Calling this helper from
            # config.py prevents StarTools from inferring the registered main module.
            candidate = Path(StarTools.get_data_dir(PLUGIN_NAME)).expanduser().resolve()
            if not candidate.is_relative_to(PLUGIN_DIR):
                return candidate
            logger.warning(
                "Chat Archive: AstrBot 数据根目录解析到了插件安装目录内，"
                "改用用户级持久目录；请设置 ASTRBOT_ROOT 或 ARCHIVE_DATA_DIR。"
            )
        except Exception as e:
            logger.debug(f"Chat Archive: StarTools data directory unavailable: {e}")

    # Keep a deterministic fallback for standalone imports without ever placing
    # persistent state inside the replaceable plugin installation directory.
    return (Path.home() / ".astrbot" / "data" / "plugin_data" / PLUGIN_NAME).resolve()


def get_legacy_data_dir() -> Path:
    """Return the pre-v1.5.0 data location inside the plugin installation."""
    # Do not resolve here: callers must be able to detect when the legacy data
    # directory itself is a symlink to user-managed external storage.
    return PLUGIN_DIR / "data"


def _move_file_safely(source: Path, destination: Path) -> bool:
    """Move one file without overwriting existing destination content."""
    if not source.is_file() or source.is_symlink() or destination.exists():
        return False
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        source.replace(destination)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        temporary = destination.with_name(f".{destination.name}.migrating")
        temporary.unlink(missing_ok=True)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
            source.unlink()
        finally:
            temporary.unlink(missing_ok=True)
    destination.chmod(0o600)
    return True


def _harden_storage_permissions(root: Path) -> None:
    """Keep migrated private archive state inaccessible to other users."""
    if root.is_symlink():
        return
    if root.is_file():
        root.chmod(0o600)
        return
    if not root.is_dir():
        return
    root.chmod(0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)


def _merge_directory_safely(source: Path, destination: Path) -> int:
    """Move a directory tree while preserving any destination conflicts."""
    if not source.is_dir() or source.is_symlink():
        return 0
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not destination.exists():
        try:
            source.replace(destination)
            _harden_storage_permissions(destination)
            return sum(
                1
                for path in destination.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise

    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    moved = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(source)
        if _move_file_safely(path, destination / relative):
            moved += 1

    # Remove only directories made empty by the migration. Unknown/conflicting
    # files stay in the legacy tree and are never overwritten or deleted.
    for path in sorted(
        (item for item in source.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        with contextlib.suppress(OSError):
            path.rmdir()
    with contextlib.suppress(OSError):
        source.rmdir()
    _harden_storage_permissions(destination)
    return moved


def migrate_legacy_storage(
    data_dir: Path | None = None,
    legacy_dir: Path | None = None,
    *,
    database_path: str | Path | None = None,
) -> dict[str, int]:
    """Move known legacy state into AstrBot's persistent plugin-data directory.

    The migration runs before the database or WebUI starts. It only moves known
    archive files, never overwrites destination content, and leaves unrelated
    files in the old plugin directory untouched.

    Args:
        data_dir: Destination directory for persistent plugin state.
        legacy_dir: Previous data directory inside the plugin installation.
        database_path: Active database path. A custom path prevents migration
            of the unused default database while still moving cache state.

    Returns:
        Counts of migrated cache and state files.

    Raises:
        RuntimeError: Both legacy and destination default databases exist.
    """
    destination_root = (data_dir or get_data_dir()).resolve()
    source_path = Path(legacy_dir or get_legacy_data_dir()).expanduser()
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    result = {"cache_files": 0, "state_files": 0}
    try:
        source_lstat = source_path.lstat()
    except FileNotFoundError:
        return result
    if stat.S_ISLNK(source_lstat.st_mode):
        logger.warning(
            "Chat Archive: 旧数据目录是符号链接，已跳过自动迁移以避免"
            "移动用户管理的外部存储；如需继续使用，请设置 "
            "ARCHIVE_DATA_DIR 指向该目标目录。"
        )
        return result

    source_root = source_path.resolve()
    if destination_root == source_root:
        return result

    legacy_db = source_root / "chat_history.db"
    destination_db = destination_root / "chat_history.db"
    active_db = (
        Path(database_path).expanduser().resolve()
        if database_path is not None
        else destination_db
    )
    migrate_default_db = active_db == destination_db
    if migrate_default_db and legacy_db.exists() and destination_db.exists():
        raise RuntimeError(
            "Chat Archive database migration conflict: both legacy and "
            f"destination databases exist ({legacy_db}, {destination_db}). "
            "Neither database was overwritten; resolve the conflict explicitly."
        )

    destination_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        destination_root.chmod(0o700)

    legacy_cache = source_root / "web_cache"
    if legacy_cache.exists():
        result["cache_files"] = _merge_directory_safely(
            legacy_cache,
            destination_root / "web_cache",
        )

    # Move SQLite sidecars before the main DB so a failed migration cannot make
    # an incomplete database appear ready at the new path.
    if migrate_default_db and legacy_db.exists():
        for suffix in ("-wal", "-shm", ""):
            source = source_root / f"chat_history.db{suffix}"
            destination = destination_root / f"chat_history.db{suffix}"
            if _move_file_safely(source, destination):
                result["state_files"] += 1

    failed_writes = source_root / "chat_archive_failed_writes.jsonl"
    if _move_file_safely(
        failed_writes,
        destination_root / failed_writes.name,
    ):
        result["state_files"] += 1

    if result["cache_files"] or result["state_files"]:
        logger.info(
            "Chat Archive: 已迁移旧数据目录中的 "
            f"{result['cache_files']} 个缓存文件和 "
            f"{result['state_files']} 个状态文件到 {destination_root}"
        )
    return result


def get_static_cache_dir() -> Path:
    return get_data_dir() / "web_cache"


def get_config_path() -> Path:
    env_config_path = os.environ.get("ARCHIVE_CONFIG_PATH", "").strip()
    if env_config_path:
        return expand_path(env_config_path, PLUGIN_DIR)

    data_dir = get_data_dir()
    config_path = (
        data_dir.parent.parent / "config" / "astrbot_plugin_chat_archive_config.json"
    )
    if not config_path.exists():
        config_path = (
            PLUGIN_DIR.parent.parent
            / "config"
            / "astrbot_plugin_chat_archive_config.json"
        )
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
        mode = (
            str(get_config_section("basic").get("sqlite_journal_mode", "WAL"))
            .strip()
            .upper()
        )
    mode = mode or "WAL"
    if mode not in allowed_modes:
        logger.warning(f"Unsupported SQLite journal mode '{mode}', fallback to WAL.")
        mode = "WAL"
    return mode


def load_sqlite_pool_size() -> int:
    value = os.environ.get("ARCHIVE_SQLITE_MAX_CONNECTIONS", "").strip()
    if not value:
        value = str(
            get_config_section("basic").get("sqlite_max_connections", "")
        ).strip()
    try:
        pool_size = int(value or 10)
    except (TypeError, ValueError):
        pool_size = 10
    return max(2, min(pool_size, 64))


def load_api_key() -> str:
    env_key = os.environ.get("ARCHIVE_API_KEY", "").strip()
    if env_key:
        return env_key
    return str(get_config_section("web_server").get("api_key", "") or "").strip()


def load_auth_failure_limit() -> int:
    value = os.environ.get("ARCHIVE_AUTH_FAILURE_LIMIT", "").strip()
    if not value:
        value = get_config_section("web_server").get("auth_failure_limit", 8)
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 8
    return max(3, min(limit, 60))


def load_auth_lockout_seconds() -> int:
    value = os.environ.get("ARCHIVE_AUTH_LOCKOUT_SECONDS", "").strip()
    if not value:
        value = get_config_section("web_server").get("auth_lockout_seconds", 300)
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = 300
    return max(30, min(seconds, 3600))


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
    if "ARCHIVE_ALLOWED_MEDIA_DOMAINS" in os.environ:
        env_domains = os.environ.get("ARCHIVE_ALLOWED_MEDIA_DOMAINS", "")
        domains: Any = [part.strip() for part in env_domains.split(",")]
    else:
        basic_conf = get_config_section("basic")
        domains = (
            basic_conf.get("allowed_media_domains")
            if "allowed_media_domains" in basic_conf
            else DEFAULT_ALLOWED_MEDIA_DOMAINS
        )

    if isinstance(domains, str):
        domains = [part.strip() for part in domains.split(",")]
    if domains is None:
        domains = ()
    cleaned = {
        str(domain).strip().lower().rstrip(".")
        for domain in domains
        if str(domain).strip()
    }
    return frozenset(cleaned)


def load_allow_fake_ip() -> bool:
    env_val = os.environ.get("ARCHIVE_ALLOW_FAKE_IP", "").strip().lower()
    if env_val:
        return env_val in ("1", "true", "yes", "on")
    return bool(get_config_section("basic").get("allow_fake_ip", True))
