from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import socket
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger

try:
    from .config import (
        DEFAULT_ALLOWED_MEDIA_DOMAINS,
        load_allowed_media_domains,
        load_media_max_bytes,
    )
    from .serializer import cq_param_exists, read_image_dimensions
except ImportError:
    from config import (
        DEFAULT_ALLOWED_MEDIA_DOMAINS,
        load_allowed_media_domains,
        load_media_max_bytes,
    )
    from serializer import cq_param_exists, read_image_dimensions


def sniff_passive_media(prefix: bytes) -> tuple[str, str] | None:
    """Identify passive browser media from a small file-start prefix.

    This intentionally does not recognize documents, archives, SVG, XML, HTML,
    JavaScript, or arbitrary text. Callers may use it only when the upstream
    MIME type is absent or generic.
    """
    data = bytes(prefix or "")
    if not data:
        return None

    text_probe = data[:256].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text_probe.startswith(
        (
            b"<!doctype",
            b"<html",
            b"<svg",
            b"<?xml",
            b"<script",
            b"javascript:",
        )
    ):
        return None

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"

    if len(data) >= 12 and data[4:8] == b"ftyp":
        box_size = int.from_bytes(data[:4], "big")
        brand = data[8:12]
        if box_size < 12:
            return None
        if brand in {b"avif", b"avis"}:
            return "image/avif", ".avif"
        if brand == b"qt  ":
            return "video/quicktime", ".mov"
        if brand in {
            b"isom",
            b"iso2",
            b"iso3",
            b"iso4",
            b"iso5",
            b"iso6",
            b"mp41",
            b"mp42",
            b"avc1",
            b"dash",
            b"M4V ",
            b"M4A ",
            b"3gp4",
            b"3gp5",
            b"MSNV",
        }:
            return "video/mp4", ".mp4"

    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm", ".webm"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return "video/x-msvideo", ".avi"
    if data.startswith(b"OggS"):
        media_type = "video/ogg" if b"theora" in data[:256].lower() else "audio/ogg"
        return media_type, ".ogg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav", ".wav"
    if data.startswith(b"fLaC"):
        return "audio/flac", ".flac"
    if data.startswith(b"#!AMR\n"):
        return "audio/amr", ".amr"
    if data.startswith(b"ID3"):
        return "audio/mpeg", ".mp3"
    if (
        len(data) >= 4
        and data[0] == 0xFF
        and data[1] & 0xE0 == 0xE0
        and data[1] & 0x06 != 0
        and data[2] & 0xF0 not in (0x00, 0xF0)
        and data[2] & 0x0C != 0x0C
    ):
        return "audio/mpeg", ".mp3"
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 in (0xF0, 0xF4):
        return "audio/aac", ".aac"
    return None


GENERIC_MEDIA_TYPES = frozenset(
    {
        "",
        "application/binary",
        "application/octet-stream",
        "application/x-octet-stream",
        "binary/octet-stream",
    }
)


async def read_media_prefix(content, limit: int = 512) -> bytes:
    """Read a bounded file-start prefix from an aiohttp response stream."""
    prefix = bytearray()
    while len(prefix) < limit:
        chunk = await content.read(limit - len(prefix))
        if not chunk:
            break
        prefix.extend(chunk)
    return bytes(prefix)


def hostname_matches_allowlist(hostname: str, domains) -> bool:
    """Return whether a hostname is exactly in, or below, an allowed domain."""
    host = (hostname or "").lower().rstrip(".")
    return bool(host) and any(
        host == domain or host.endswith("." + domain) for domain in domains
    )


def ip_is_public(
    ip: ipaddress._BaseAddress,
    *,
    allow_fake_ip: bool = False,
) -> bool:
    """Reject local/special addresses, with an opt-in for proxy fake-IP space."""
    if (
        allow_fake_ip
        and isinstance(ip, ipaddress.IPv4Address)
        and ip in ipaddress.IPv4Network("198.18.0.0/15")
    ):
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def resolve_public_targets(
    hostname: str,
    port: int,
    *,
    family: int = socket.AF_UNSPEC,
    allow_fake_ip: bool = False,
) -> list[dict]:
    """Resolve once and return only numeric addresses safe to connect to."""
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            family,
            socket.SOCK_STREAM,
        )
    else:
        literal_family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        infos = [
            (
                literal_family,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (str(literal), port),
            )
        ]

    targets: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for addr_family, _socktype, proto, _canonname, sockaddr in infos:
        address = str(sockaddr[0]).split("%", 1)[0]
        ip = ipaddress.ip_address(address)
        if not ip_is_public(ip, allow_fake_ip=allow_fake_ip):
            raise OSError(f"non-public DNS target: {hostname} -> {ip}")
        key = (addr_family, str(ip))
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            {
                "hostname": hostname,
                "host": str(ip),
                "port": port,
                "family": addr_family,
                "proto": proto or socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
        )
    if not targets:
        raise OSError(f"no DNS targets for {hostname}")
    return targets


class PinnedPublicResolver(aiohttp.abc.AbstractResolver):
    """Resolve and pin a connection to the same validated numeric address."""

    def __init__(self, *, allow_fake_ip: bool):
        self.allow_fake_ip = allow_fake_ip

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict]:
        return await resolve_public_targets(
            host,
            port,
            family=family,
            allow_fake_ip=self.allow_fake_ip,
        )

    async def close(self) -> None:
        return None


def validate_remote_media_url(url: str, allowed_domains):
    """Validate a remote media URL and return its parsed form and port.

    ``ValueError`` represents malformed input; ``PermissionError`` represents
    a syntactically valid destination outside the configured policy.
    """
    try:
        raw_url = str(url or "")
        if raw_url != raw_url.strip() or any(ord(char) < 32 for char in raw_url):
            raise ValueError("invalid URL")
        parsed = urlparse(raw_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("invalid URL scheme")
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if not hostname or parsed.username or parsed.password:
            raise ValueError("invalid URL")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("invalid URL") from exc
    if port not in (80, 443):
        raise PermissionError("forbidden port")
    if not hostname_matches_allowlist(hostname, allowed_domains):
        raise PermissionError("forbidden domain")
    return parsed, hostname, port


def is_passive_media_file(path: Path) -> bool:
    """Validate a cached file by type signature and extension."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as file_handle:
            sniffed = sniff_passive_media(file_handle.read(512))
    except OSError:
        return False
    if sniffed is None:
        return False
    _media_type, detected_extension = sniffed
    suffix = path.suffix.lower()
    compatible_extensions = {
        ".jpg": {".jpg", ".jpeg"},
        ".mp4": {".mp4", ".m4v"},
        ".ogg": {".ogg", ".oga"},
        ".webm": {".webm", ".mkv"},
    }
    return suffix in compatible_extensions.get(
        detected_extension,
        {detected_extension},
    )


class ArchiveMediaCache:
    _CACHE_STATE_MAX_AGE = 300.0
    _URL_EXTENSIONS = {
        ".png",
        ".gif",
        ".webp",
        ".jpg",
        ".jpeg",
        ".avif",
        ".aac",
        ".amr",
        ".flac",
        ".mp3",
        ".ogg",
        ".wav",
        ".mp4",
        ".m4v",
        ".mov",
        ".webm",
        ".avi",
        ".mkv",
    }
    _CONTENT_TYPE_EXTENSIONS = {
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "video/x-m4v": ".m4v",
        "video/x-matroska": ".mkv",
        "video/x-msvideo": ".avi",
        "video/ogg": ".ogg",
        "audio/aac": ".aac",
        "audio/amr": ".amr",
        "audio/flac": ".flac",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
    }

    def __init__(self, *, config, cache_dir: Path):
        self.conf = config
        self.cache_dir = cache_dir
        self._download_locks: dict[str, asyncio.Lock] = {}
        self._download_lock_refs: dict[str, int] = {}
        self._download_locks_guard = asyncio.Lock()
        self._client: aiohttp.ClientSession | None = None
        self._client_guard = asyncio.Lock()
        self._download_slots = asyncio.Semaphore(4)
        self._maintenance_guard = asyncio.Lock()
        self._cache_total_bytes: int | None = None
        self._cache_evictable: dict[str, tuple[float, int, Path]] = {}
        self._cache_state_checked_at = 0.0
        self._cache_reservations: dict[str, tuple[int, Path]] = {}

    async def _get_client(self) -> aiohttp.ClientSession:
        async with self._client_guard:
            if self._client is None or self._client.closed:
                resolver = PinnedPublicResolver(
                    allow_fake_ip=self.is_allow_fake_ip(),
                )
                connector = aiohttp.TCPConnector(
                    resolver=resolver,
                    use_dns_cache=False,
                    ttl_dns_cache=0,
                    limit=8,
                    limit_per_host=4,
                )
                self._client = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(
                        total=30,
                        connect=10,
                        sock_read=20,
                    ),
                    auto_decompress=False,
                    trust_env=False,
                )
            return self._client

    async def close(self) -> None:
        async with self._client_guard:
            client = self._client
            self._client = None
        if client is not None:
            await client.close()

    def _existing_cached_url(self, url_hash: str) -> str:
        extensions = self._URL_EXTENSIONS | set(self._CONTENT_TYPE_EXTENSIONS.values())
        for ext in extensions:
            candidate = self.cache_dir / f"{url_hash}{ext}"
            if is_passive_media_file(candidate):
                return f"/static/cache/{candidate.name}"
        return ""

    def get_allowed_media_domains(self) -> set[str]:
        """Return configured media-cache domain allowlist."""
        if "ARCHIVE_ALLOWED_MEDIA_DOMAINS" in os.environ:
            return set(load_allowed_media_domains())
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        domains = (
            basic_conf.get("allowed_media_domains")
            if "allowed_media_domains" in basic_conf
            else DEFAULT_ALLOWED_MEDIA_DOMAINS
        )
        if isinstance(domains, str):
            domains = domains.split(",")
        if domains is None:
            domains = ()
        return {str(d).strip().lower().rstrip(".") for d in domains if str(d).strip()}

    def get_max_media_bytes(self) -> int:
        if os.environ.get("ARCHIVE_MEDIA_MAX_MB", "").strip():
            return load_media_max_bytes()
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        try:
            mb = int(basic_conf.get("media_max_mb", 50))
        except (TypeError, ValueError):
            mb = 50
        mb = max(1, min(mb, 200))
        return mb * 1024 * 1024

    def get_max_cache_bytes(self) -> int:
        """Return the total active cache quota (default 10 GiB).

        A conservative default keeps existing installations below the current
        live cache size from being unexpectedly evicted during a data-directory
        migration. Operators can override it with ARCHIVE_MEDIA_CACHE_MAX_MB.
        """
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        raw_value = os.environ.get(
            "ARCHIVE_MEDIA_CACHE_MAX_MB",
            basic_conf.get("media_cache_max_mb", 10240),
        )
        try:
            mb = int(raw_value)
        except (TypeError, ValueError):
            mb = 10240
        mb = max(256, min(mb, 102400))
        return mb * 1024 * 1024

    @staticmethod
    def _is_managed_cache_filename(name: str) -> bool:
        return bool(
            re.fullmatch(
                r"(?:[0-9a-f]{32}|telegram_(?:avatar|chat_avatar|media)_[0-9a-f]{32})"
                r"\.[A-Za-z0-9]{1,8}",
                name,
                flags=re.IGNORECASE,
            )
        )

    def _maintain_cache_sync(
        self,
        required_bytes: int,
        *,
        force_rescan: bool = False,
        exclude_reservation: str = "",
        replacement_path: Path | None = None,
    ) -> bool:
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(Exception):
            self.cache_dir.chmod(0o700)

        if (
            force_rescan
            or self._cache_total_bytes is None
            or time.monotonic() - self._cache_state_checked_at
            >= self._CACHE_STATE_MAX_AGE
        ):
            now = time.time()
            total_bytes = 0
            evictable: dict[str, tuple[float, int, Path]] = {}
            active_temp_paths = {
                str(path) for _reserved, path in self._cache_reservations.values()
            }
            for path in self.cache_dir.iterdir():
                if not path.is_file():
                    continue
                try:
                    path_stat = path.stat()
                except OSError:
                    continue
                if path.name.endswith(".tmp"):
                    if str(path) in active_temp_paths:
                        continue
                    if now - path_stat.st_mtime >= 3600 and (self.conf or {}).get(
                        "basic", {}
                    ).get("allow_cache_eviction", False):
                        try:
                            path.unlink()
                            continue
                        except OSError:
                            pass
                    # Retained or undeletable temporary files still use space.
                    total_bytes += path_stat.st_size
                    continue
                total_bytes += path_stat.st_size
                if self._is_managed_cache_filename(path.name):
                    evictable[path.name] = (
                        path_stat.st_mtime,
                        path_stat.st_size,
                        path,
                    )
            self._cache_total_bytes = total_bytes
            self._cache_evictable = evictable
            self._cache_state_checked_at = time.monotonic()

        quota = self.get_max_cache_bytes()
        required_bytes = max(0, int(required_bytes or 0))
        if required_bytes > quota:
            return False

        active_bytes = 0
        for key, (reserved_bytes, temp_path) in self._cache_reservations.items():
            if key == exclude_reservation:
                continue
            try:
                temp_size = temp_path.stat().st_size
            except OSError:
                temp_size = 0
            active_bytes += max(reserved_bytes, temp_size)

        replacement_size = 0
        protected_name = ""
        if replacement_path is not None:
            protected_name = replacement_path.name
            existing = self._cache_evictable.get(protected_name)
            if existing is not None:
                replacement_size = existing[1]

        target = quota - active_bytes - required_bytes + replacement_size
        if self._cache_total_bytes <= target:
            return True

        if not (self.conf or {}).get("basic", {}).get("allow_cache_eviction", False):
            # Admission remains bounded; existing referenced files stay intact.
            return False

        for _mtime, size, path in sorted(
            self._cache_evictable.values(), key=lambda item: item[0]
        ):
            if path.name == protected_name:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            self._cache_evictable.pop(path.name, None)
            self._cache_total_bytes -= size
            if self._cache_total_bytes <= target:
                return True
        return self._cache_total_bytes <= target

    async def ensure_cache_capacity(
        self, required_bytes: int = 0, *, force_rescan: bool = False
    ) -> bool:
        """Verify quota using a cached catalog, optionally forcing reconciliation."""
        async with self._maintenance_guard:
            try:
                return await asyncio.to_thread(
                    self._maintain_cache_sync,
                    required_bytes,
                    force_rescan=force_rescan,
                )
            except Exception as exc:
                logger.warning(f"Chat Archive: 媒体缓存容量维护失败: {exc}")
                return False

    async def reserve_cache_capacity(
        self, temp_path: Path, required_bytes: int = 0
    ) -> bool:
        """Reserve quota for one in-flight download without rescanning the directory."""
        key = str(temp_path)
        async with self._maintenance_guard:
            if key in self._cache_reservations:
                return False
            try:
                capacity_ok = await asyncio.to_thread(
                    self._maintain_cache_sync,
                    required_bytes,
                )
            except Exception as exc:
                logger.warning(f"Chat Archive: 媒体缓存容量预留失败: {exc}")
                return False
            if not capacity_ok:
                return False
            self._cache_reservations[key] = (
                max(0, int(required_bytes or 0)),
                temp_path,
            )
            return True

    async def finalize_cache_file(self, temp_path: Path, dest_path: Path) -> bool:
        """Atomically commit one reserved temp file and update the cache catalog."""
        key = str(temp_path)
        async with self._maintenance_guard:
            if key not in self._cache_reservations:
                return False
            try:
                actual_size = temp_path.stat().st_size
                capacity_ok = await asyncio.to_thread(
                    self._maintain_cache_sync,
                    actual_size,
                    exclude_reservation=key,
                    replacement_path=dest_path,
                )
                if not capacity_ok:
                    self._cache_reservations.pop(key, None)
                    return False

                existing = self._cache_evictable.get(dest_path.name)
                existing_size = existing[1] if existing is not None else 0
                temp_path.replace(dest_path)
                path_stat = dest_path.stat()
                self._cache_total_bytes = (
                    int(self._cache_total_bytes or 0)
                    - existing_size
                    + path_stat.st_size
                )
                self._cache_evictable[dest_path.name] = (
                    path_stat.st_mtime,
                    path_stat.st_size,
                    dest_path,
                )
                self._cache_reservations.pop(key, None)
                return True
            except Exception:
                self._cache_reservations.pop(key, None)
                self._cache_total_bytes = None
                raise

    async def release_cache_reservation(self, temp_path: Path) -> None:
        """Release quota held by a cancelled or failed download."""
        async with self._maintenance_guard:
            self._cache_reservations.pop(str(temp_path), None)

    def is_allow_fake_ip(self) -> bool:
        env_val = os.environ.get("ARCHIVE_ALLOW_FAKE_IP", "").strip().lower()
        if env_val:
            return env_val in ("1", "true", "yes", "on")
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        return bool(basic_conf.get("allow_fake_ip", True))

    async def download_media_to_cache(self, url: str) -> str:
        """Download media into the local web cache and return its /static/cache path."""
        if not url:
            return ""

        if url.startswith("/static/cache/"):
            return url

        allowed_domains = self.get_allowed_media_domains()
        try:
            _parsed_url, _hostname, _upstream_port = validate_remote_media_url(
                url,
                allowed_domains,
            )
        except ValueError as exc:
            logger.warning(f"Chat Archive: 拒绝下载无效媒体 URL {url}: {exc}")
            return url
        except PermissionError as exc:
            logger.warning(f"Chat Archive: 拒绝缓存媒体 URL {url}: {exc}")
            return url

        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        cached_url = self._existing_cached_url(url_hash)
        if cached_url:
            return cached_url

        max_media_bytes = self.get_max_media_bytes()

        try:
            self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with suppress(Exception):
                self.cache_dir.chmod(0o700)
        except Exception as e:
            logger.error(f"Chat Archive: 创建缓存目录失败: {e}")
            return url
        async with self._download_locks_guard:
            download_lock = self._download_locks.get(url_hash)
            if download_lock is None:
                download_lock = asyncio.Lock()
                self._download_locks[url_hash] = download_lock
            self._download_lock_refs[url_hash] = (
                self._download_lock_refs.get(url_hash, 0) + 1
            )

        try:
            async with download_lock:
                cached_url = self._existing_cached_url(url_hash)
                if cached_url:
                    return cached_url

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
                    "Referer": "https://q.qq.com/",
                }

                try:
                    client = await self._get_client()
                    async with self._download_slots:
                        async with client.get(
                            url,
                            headers=headers,
                            allow_redirects=False,
                        ) as response:
                            if response.status != 200:
                                logger.warning(
                                    f"Chat Archive: 下载媒体失败 {url}, 状态码 {response.status}"
                                )
                                return url

                            content_length = response.headers.get("content-length")
                            expected_size = 0
                            if content_length:
                                try:
                                    expected_size = int(content_length)
                                    if expected_size > max_media_bytes:
                                        logger.warning(
                                            f"Chat Archive: 拒绝缓存超大媒体 {url}, size={content_length}"
                                        )
                                        return url
                                except ValueError:
                                    expected_size = 0

                            declared_type = (
                                response.headers.get("content-type", "")
                                .split(";", 1)[0]
                                .strip()
                                .lower()
                            )
                            if (
                                declared_type not in GENERIC_MEDIA_TYPES
                                and declared_type not in self._CONTENT_TYPE_EXTENSIONS
                            ):
                                logger.warning(
                                    "Chat Archive: 拒绝缓存活动/不支持的媒体响应 "
                                    f"{url}, content-type={declared_type or 'unknown'}"
                                )
                                return url

                            sniffed_prefix = await read_media_prefix(response.content)
                            sniffed = sniff_passive_media(sniffed_prefix)
                            if sniffed is None:
                                logger.warning(
                                    "Chat Archive: 拒绝缓存无法识别的媒体响应 "
                                    f"{url}, content-type={declared_type or 'unknown'}"
                                )
                                return url
                            _content_type, new_ext = sniffed
                            filename = f"{url_hash}{new_ext}"
                            dest_path = self.cache_dir / filename
                            relative_url = f"/static/cache/{filename}"
                            if is_passive_media_file(dest_path):
                                return relative_url

                            temp_path = dest_path.with_suffix(".tmp")
                            temp_path.unlink(missing_ok=True)
                            if not await self.reserve_cache_capacity(
                                temp_path,
                                expected_size,
                            ):
                                logger.warning(
                                    "Chat Archive: 媒体缓存总容量不足，拒绝下载"
                                )
                                return url
                            reservation_active = True
                            downloaded = len(sniffed_prefix)
                            try:
                                if downloaded > max_media_bytes:
                                    raise ValueError(
                                        f"media too large: {downloaded} bytes"
                                    )
                                with suppress(Exception):
                                    temp_path.touch(mode=0o600, exist_ok=True)
                                with open(temp_path, "wb") as f:
                                    if sniffed_prefix:
                                        f.write(sniffed_prefix)
                                    async for chunk in response.content.iter_chunked(
                                        64 * 1024
                                    ):
                                        downloaded += len(chunk)
                                        if downloaded > max_media_bytes:
                                            raise ValueError(
                                                f"media too large: {downloaded} bytes"
                                            )
                                        f.write(chunk)
                                finalized = await self.finalize_cache_file(
                                    temp_path,
                                    dest_path,
                                )
                                reservation_active = False
                                if not finalized:
                                    raise ValueError("media cache quota exceeded")
                            except asyncio.CancelledError:
                                if reservation_active:
                                    await self.release_cache_reservation(temp_path)
                                temp_path.unlink(missing_ok=True)
                                raise
                            except Exception:
                                if reservation_active:
                                    await self.release_cache_reservation(temp_path)
                                temp_path.unlink(missing_ok=True)
                                raise
                            logger.info(f"Chat Archive: 成功缓存媒体到 {dest_path}")
                            with suppress(Exception):
                                dest_path.chmod(0o600)
                            return relative_url
                except Exception as e:
                    logger.error(f"Chat Archive: 下载媒体异常 {url}: {e}")

                return url
        finally:
            async with self._download_locks_guard:
                refs = self._download_lock_refs.get(url_hash, 0) - 1
                if refs <= 0:
                    self._download_lock_refs.pop(url_hash, None)
                    self._download_locks.pop(url_hash, None)
                else:
                    self._download_lock_refs[url_hash] = refs

    async def replace_cq_media_url(self, match) -> str:
        cq_type = match.group(1)
        inner = match.group(2)
        url_match = re.search(r"url=(https?://[^,\]]+)", inner)
        if url_match:
            original_url = url_match.group(1)
            url = (
                original_url.replace("&amp;", "&")
                .replace("&#44;", ",")
                .replace("&#91;", "[")
                .replace("&#93;", "]")
            )
            cached_url = await self.download_media_to_cache(url)
            new_inner = inner.replace(original_url, cached_url)
            if (
                cq_type == "image"
                and not cq_param_exists(new_inner, "width")
                and not cq_param_exists(new_inner, "height")
                and cached_url.startswith("/static/cache/")
            ):
                cached_path = self.cache_dir / cached_url.rsplit("/", 1)[-1]
                dims = read_image_dimensions(str(cached_path))
                if dims:
                    new_inner += f",width={dims[0]},height={dims[1]}"
            return f"[CQ:{cq_type},{new_inner}]"
        return match.group(0)

    async def process_and_cache_media_in_string(self, text: str) -> str:
        """Replace CQ image/video URLs in text with local cache URLs when possible."""
        if not text:
            return text

        pattern = r"\[CQ:(image|video),([^\]]+)\]"
        matches = list(re.finditer(pattern, text))
        if not matches:
            return text

        replacements = await asyncio.gather(
            *(self.replace_cq_media_url(match) for match in matches),
            return_exceptions=True,
        )
        for match, result in reversed(list(zip(matches, replacements))):
            if isinstance(result, BaseException):
                logger.error(f"Chat Archive: 缓存媒体片段失败: {result}")
                replaced = match.group(0)
            else:
                replaced = result
            start, end = match.span()
            text = text[:start] + replaced + text[end:]

        return text
