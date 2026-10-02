from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger

try:
    from .event_extractor import ArchiveEventExtractor
    from .serializer import escape_cq_param
except ImportError:
    from event_extractor import ArchiveEventExtractor
    from serializer import escape_cq_param


class TelegramChannelCapture:
    """Capture Telegram channel_post updates from the running PTB application."""

    _HANDLER_GROUP = 90
    _POLL_INTERVAL = 0.5
    _AVATAR_MISS_TTL = 300
    _AVATAR_HIT_TTL = 21_600
    _AVATAR_CACHE_MAX = 512

    def __init__(self, plugin):
        self.plugin = plugin
        self._registrations: dict[int, tuple[Any, Any, int]] = {}
        self._register_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._avatar_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._avatar_miss_until: OrderedDict[str, float] = OrderedDict()
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._file_lock_refs: dict[str, int] = {}
        self._file_locks_guard = asyncio.Lock()

    async def run(self) -> None:
        self._stop_event.clear()
        retry_delay = self._POLL_INTERVAL
        while not getattr(self.plugin, "_shutting_down", False):
            try:
                await self.ensure_registered()
                retry_delay = self._POLL_INTERVAL
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Chat Archive: Telegram 捕获器注册检查失败，将重试: {e}")
                retry_delay = min(max(retry_delay * 2, 1.0), 10.0)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=retry_delay,
                )
                return
            except asyncio.TimeoutError:
                continue

    async def ensure_registered(self) -> int:
        try:
            from telegram import Update
            from telegram.ext import TypeHandler
        except Exception as e:
            logger.debug(f"Chat Archive: Telegram channel capture unavailable: {e}")
            return 0

        async with self._register_lock:
            count = 0
            adapters = self._iter_telegram_adapters()
            active_keys = {id(adapter) for adapter in adapters}
            for key, registration in list(self._registrations.items()):
                if key not in active_keys:
                    self._remove_handler(*registration)
                    self._registrations.pop(key, None)

            for adapter in adapters:
                application = getattr(adapter, "application", None)
                if application is None:
                    continue

                key = id(adapter)
                registered = self._registrations.get(key)
                if registered and registered[0] is application:
                    count += 1
                    continue
                if registered:
                    self._remove_handler(*registered)

                try:

                    async def channel_post_handler(update, context, adapter=adapter):
                        await self.handle_update(update, context, adapter)

                    handler = TypeHandler(Update, channel_post_handler)
                    application.add_handler(handler, group=self._HANDLER_GROUP)
                    self._registrations[key] = (
                        application,
                        handler,
                        self._HANDLER_GROUP,
                    )
                    count += 1

                    platform_id, _ = self._adapter_platform(adapter)
                    logger.info(
                        f"Chat Archive: 已为 Telegram 平台 {platform_id} 注册频道消息捕获器。"
                    )
                except Exception as e:
                    logger.error(
                        "Chat Archive: Telegram Application 捕获器注册失败，"
                        f"下次监控周期将重试: {e}"
                    )

            return count

    async def stop(self) -> None:
        self._stop_event.set()
        for registration in list(self._registrations.values()):
            self._remove_handler(*registration)
        self._registrations.clear()

    async def handle_update(self, update, context, adapter) -> None:
        if getattr(self.plugin, "_shutting_down", False):
            return

        message = getattr(update, "channel_post", None)
        if not message:
            return

        basic_conf = self.plugin.conf.get("basic", {}) if self.plugin.conf else {}
        if not basic_conf.get("enable_archive", True):
            return

        try:
            platform_id, platform_name = self._adapter_platform(adapter)
            bot = getattr(context, "bot", None) or getattr(adapter, "client", None)
            cache_media = bool(basic_conf.get("cache_media", False))
            # Build placeholders only. Telegram file/avatar network work is
            # deferred to the plugin's ordered media worker.
            raw_message = await self._message_to_archive_text(
                message,
                bot=bot,
                cache_media=False,
            )
            record = self._record_from_channel_message(
                message,
                platform_id=platform_id,
                platform_name=platform_name,
                raw_message=raw_message,
            )

            if record["user_id"] in getattr(self.plugin, "_ignored_users", set()):
                return
            if cache_media:
                record["_telegram_channel_message"] = message
                record["_telegram_bot"] = bot
                record["_telegram_platform_id"] = platform_id

            await self.plugin._enqueue_record_dict(
                record,
                cache_media=cache_media,
            )
        except Exception as e:
            logger.error(
                f"Chat Archive: 记录 Telegram 频道消息异常: {e}", exc_info=True
            )

    async def enrich_record_media(self, record: dict[str, Any]) -> None:
        """Populate Telegram media/avatar fields inside the background worker."""
        message = record.pop("_telegram_channel_message", None)
        bot = record.pop("_telegram_bot", None)
        platform_id = str(
            record.pop("_telegram_platform_id", "")
            or record.get("platform_id")
            or "telegram"
        )
        if message is not None:
            record["message"] = await self._message_to_archive_text(
                message,
                bot=bot,
                cache_media=True,
            )
            avatar_url = await self.resolve_channel_avatar(
                message,
                bot=bot,
                platform_id=platform_id,
            )
            if avatar_url:
                record["avatar_url"] = avatar_url
                record["guild_avatar_url"] = avatar_url
            return

        event = record.pop("_telegram_event", None)
        avatar_kind = str(record.pop("_telegram_avatar_kind", "") or "")
        if event is None:
            return
        if avatar_kind == "bot":
            avatar_url = await self.resolve_bot_avatar(event)
        else:
            avatar_url = await self.resolve_event_avatar(event)
        if avatar_url:
            record["avatar_url"] = avatar_url
        if record.get("message_type") in {
            "group",
            "GroupMessage",
            "channel",
            "ChannelMessage",
        }:
            guild_avatar_url = await self.resolve_chat_avatar(event)
            if guild_avatar_url:
                record["guild_avatar_url"] = guild_avatar_url

        message_text = str(record.get("message") or "")
        if "[CQ:" in message_text:
            record["message"] = await self.plugin._process_and_cache_media_in_string(
                message_text
            )

    def _iter_telegram_adapters(self):
        context = getattr(self.plugin, "context", None)
        platform_manager = getattr(context, "platform_manager", None)
        if platform_manager is None:
            return []

        get_insts = getattr(platform_manager, "get_insts", None)
        if callable(get_insts):
            with suppress(Exception):
                adapters = list(get_insts())
                return [
                    adapter
                    for adapter in adapters
                    if self._adapter_platform(adapter)[1].lower() == "telegram"
                ]

        adapters = getattr(platform_manager, "platform_insts", []) or []
        return [
            adapter
            for adapter in adapters
            if self._adapter_platform(adapter)[1].lower() == "telegram"
        ]

    @staticmethod
    def _adapter_platform(adapter) -> tuple[str, str]:
        platform_id = ""
        platform_name = ""
        try:
            meta = adapter.meta()
            platform_id = str(getattr(meta, "id", "") or "")
            platform_name = str(getattr(meta, "name", "") or "")
        except Exception:
            pass

        config = getattr(adapter, "config", {}) or {}
        if not platform_id and hasattr(config, "get"):
            platform_id = str(config.get("id") or "")
        if not platform_name and hasattr(config, "get"):
            platform_name = str(config.get("type") or "")
        return platform_id or "telegram", platform_name or "telegram"

    @classmethod
    def _record_from_channel_message(
        cls,
        message,
        *,
        platform_id: str,
        platform_name: str,
        raw_message: str,
    ) -> dict[str, Any]:
        chat = getattr(message, "chat", None)
        chat_id = str(getattr(chat, "id", "") or "")
        thread_id = str(getattr(message, "message_thread_id", "") or "")
        session_key = chat_id
        if getattr(message, "is_topic_message", False) and thread_id:
            session_key = f"{chat_id}#{thread_id}"

        sender_chat = getattr(message, "sender_chat", None) or chat
        user_id = str(getattr(sender_chat, "id", "") or chat_id)
        sender_name = (
            getattr(sender_chat, "title", "")
            or getattr(sender_chat, "username", "")
            or getattr(chat, "title", "")
            or "Telegram Channel"
        )
        session_name = (
            getattr(chat, "title", "")
            or getattr(chat, "full_name", "")
            or getattr(chat, "username", "")
            or chat_id
        )
        timestamp = ArchiveEventExtractor.coerce_timestamp(
            getattr(message, "date", None),
            int(time.time()),
        )
        msg_id = str(getattr(message, "message_id", "") or "")

        return {
            "user_id": user_id,
            "sender_name": str(sender_name or ""),
            "message": str(raw_message or "[Telegram 频道消息]"),
            "timestamp": int(timestamp),
            "session_id": (
                f"{platform_id}:ChannelMessage:{session_key}"
                if platform_id and session_key
                else session_key
            ),
            "message_type": "ChannelMessage",
            "session_name": str(session_name or ""),
            "msg_id": msg_id or f"{platform_id}:{user_id}:{timestamp}",
            "platform_id": str(platform_id or ""),
            "platform_name": str(platform_name or "telegram"),
            "avatar_url": "",
        }

    async def resolve_event_avatar(self, event) -> str:
        platform_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""

        platform_raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        message = (
            getattr(platform_raw, "effective_message", None)
            or getattr(platform_raw, "message", None)
            or getattr(platform_raw, "channel_post", None)
            or getattr(platform_raw, "edited_channel_post", None)
            or platform_raw
        )
        if message is None:
            return ""

        user = getattr(platform_raw, "effective_user", None)
        chat = getattr(platform_raw, "effective_chat", None)
        bot = self._bot_for_platform(platform_id)
        return await self.resolve_message_avatar(
            message,
            bot=bot,
            platform_id=platform_id or "telegram",
            user=user,
            chat=chat,
        )

    async def resolve_message_avatar(
        self,
        message,
        *,
        bot=None,
        platform_id: str = "telegram",
        user=None,
        chat=None,
    ) -> str:
        from_user = user or getattr(message, "from_user", None)
        user_id = getattr(from_user, "id", None)
        if user_id:
            cache_key = f"{platform_id}:user:{user_id}"
            cached = self._get_cached_avatar(cache_key)
            if cached is not None:
                return cached
            avatar_url = await self._fetch_user_avatar(
                bot,
                str(user_id),
                cache_key,
            )
            if not avatar_url:
                avatar_url = await self._fetch_chat_avatar(
                    bot,
                    str(user_id),
                    chat or getattr(message, "chat", None),
                    cache_key,
                )
            return self._remember_avatar(cache_key, avatar_url)

        chat = (
            chat
            or getattr(message, "sender_chat", None)
            or getattr(message, "chat", None)
        )
        chat_id = getattr(chat, "id", None)
        if chat_id:
            cache_key = f"{platform_id}:chat:{chat_id}"
            cached = self._get_cached_avatar(cache_key)
            if cached is not None:
                return cached
            avatar_url = await self._fetch_chat_avatar(
                bot,
                str(chat_id),
                chat,
                cache_key,
            )
            return self._remember_avatar(cache_key, avatar_url)

        return ""

    async def resolve_channel_avatar(
        self,
        message,
        *,
        bot=None,
        platform_id: str = "telegram",
    ) -> str:
        chat = getattr(message, "sender_chat", None) or getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if not chat_id:
            return await self.resolve_message_avatar(
                message,
                bot=bot,
                platform_id=platform_id,
            )

        cache_key = f"{platform_id}:chat:{chat_id}"
        cached = self._get_cached_avatar(cache_key)
        if cached is not None:
            return cached

        avatar_url = await self._fetch_chat_avatar(bot, str(chat_id), chat, cache_key)
        return self._remember_avatar(cache_key, avatar_url)

    async def resolve_chat_avatar(self, event) -> str:
        platform_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""

        platform_raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        message = (
            getattr(platform_raw, "effective_message", None)
            or getattr(platform_raw, "message", None)
            or getattr(platform_raw, "channel_post", None)
            or getattr(platform_raw, "edited_channel_post", None)
            or platform_raw
        )
        chat = getattr(platform_raw, "effective_chat", None) or (
            getattr(message, "chat", None) if message else None
        )
        if chat is None:
            return ""

        chat_id = getattr(chat, "id", None)
        if not chat_id:
            return ""

        bot = self._bot_for_platform(platform_id)
        cache_key = f"{platform_id}:chat:{chat_id}"
        cached = self._get_cached_avatar(cache_key)
        if cached is not None:
            return cached
        avatar_url = await self._fetch_chat_avatar(
            bot,
            str(chat_id),
            chat,
            cache_key,
        )
        return self._remember_avatar(cache_key, avatar_url)

    async def resolve_bot_avatar(self, event) -> str:
        platform_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""
        platform_id = platform_id or "telegram"

        bot = self._bot_for_platform(platform_id)
        if bot is None:
            return ""

        bot_id = None
        try:
            bot_id = getattr(bot, "id", None)
        except Exception:
            bot_id = None
        if not bot_id:
            token = str(getattr(bot, "token", "") or "")
            token_id = token.split(":", 1)[0]
            if token_id.isdigit():
                bot_id = token_id
        if not bot_id:
            get_me = getattr(bot, "get_me", None)
            if callable(get_me):
                try:
                    me = get_me()
                    me = await me if inspect.isawaitable(me) else me
                    bot_id = getattr(me, "id", None)
                except Exception as e:
                    logger.debug(f"Chat Archive: 获取 Telegram bot 信息失败: {e}")
        if not bot_id:
            return ""

        cache_key = f"{platform_id}:bot:{bot_id}"
        cached = self._get_cached_avatar(cache_key)
        if cached is not None:
            return cached
        avatar_url = await self._fetch_user_avatar(bot, str(bot_id), cache_key)
        if not avatar_url:
            avatar_url = await self._fetch_chat_avatar(
                bot, str(bot_id), None, cache_key
            )
        return self._remember_avatar(cache_key, avatar_url)

    def _get_cached_avatar(self, cache_key: str) -> str | None:
        if cache_key in self._avatar_cache:
            avatar_url, expires_at = self._avatar_cache[cache_key]
            if expires_at > time.time():
                self._avatar_cache.move_to_end(cache_key)
                return avatar_url
            self._avatar_cache.pop(cache_key, None)
        miss_until = self._avatar_miss_until.get(cache_key, 0)
        if miss_until > time.time():
            self._avatar_miss_until.move_to_end(cache_key)
            return ""
        if miss_until:
            self._avatar_miss_until.pop(cache_key, None)
        return None

    def _remember_avatar(self, cache_key: str, avatar_url: str) -> str:
        if avatar_url:
            self._avatar_cache[cache_key] = (
                avatar_url,
                time.time() + self._AVATAR_HIT_TTL,
            )
            self._avatar_cache.move_to_end(cache_key)
            self._avatar_miss_until.pop(cache_key, None)
            while len(self._avatar_cache) > self._AVATAR_CACHE_MAX:
                self._avatar_cache.popitem(last=False)
            return avatar_url
        self._avatar_miss_until[cache_key] = time.time() + self._AVATAR_MISS_TTL
        self._avatar_miss_until.move_to_end(cache_key)
        while len(self._avatar_miss_until) > self._AVATAR_CACHE_MAX:
            self._avatar_miss_until.popitem(last=False)
        return ""

    def _bot_for_platform(self, platform_id: str):
        fallback_bot = None
        for adapter in self._iter_telegram_adapters():
            adapter_platform_id, _ = self._adapter_platform(adapter)
            bot = getattr(adapter, "client", None)
            if fallback_bot is None and bot is not None:
                fallback_bot = bot
            if platform_id and adapter_platform_id != platform_id:
                continue
            if bot is not None:
                return bot
        return fallback_bot

    async def _fetch_user_avatar(self, bot, user_id: str, cache_key: str) -> str:
        if bot is None:
            return ""
        try:
            photos = await bot.get_user_profile_photos(user_id=int(user_id), limit=1)
            photo_rows = getattr(photos, "photos", None) or []
            if not photo_rows or not photo_rows[0]:
                return ""
            photo = photo_rows[0][-1]
            file_obj = await self._photo_to_file(photo, bot)
            return await self._cache_telegram_file(file_obj, cache_key)
        except Exception as e:
            logger.debug(f"Chat Archive: 获取 Telegram 用户头像失败 {user_id}: {e}")
            return ""

    async def _fetch_chat_avatar(self, bot, chat_id: str, chat, cache_key: str) -> str:
        if bot is None:
            return ""
        try:
            chat_obj = chat
            get_chat = getattr(bot, "get_chat", None)
            if callable(get_chat):
                maybe_chat = get_chat(chat_id=int(chat_id))
                chat_obj = (
                    await maybe_chat if inspect.isawaitable(maybe_chat) else maybe_chat
                )
            photo = getattr(chat_obj, "photo", None) or getattr(chat, "photo", None)
            file_id = (
                getattr(photo, "big_file_id", "")
                or getattr(photo, "small_file_id", "")
                or getattr(photo, "file_id", "")
            )
            if not file_id:
                return ""
            get_file = getattr(bot, "get_file", None)
            if not callable(get_file):
                return ""
            file_obj = get_file(file_id)
            if inspect.isawaitable(file_obj):
                file_obj = await file_obj
            return await self._cache_telegram_file(file_obj, cache_key)
        except Exception as e:
            logger.debug(f"Chat Archive: 获取 Telegram 聊天头像失败 {chat_id}: {e}")
            return ""

    @staticmethod
    async def _photo_to_file(photo, bot):
        get_file = getattr(photo, "get_file", None)
        if callable(get_file):
            file_obj = get_file()
            return await file_obj if inspect.isawaitable(file_obj) else file_obj

        file_id = getattr(photo, "file_id", "")
        get_file = getattr(bot, "get_file", None)
        if file_id and callable(get_file):
            file_obj = get_file(file_id)
            return await file_obj if inspect.isawaitable(file_obj) else file_obj
        return None

    async def _cache_telegram_file(
        self,
        file_obj,
        cache_key: str,
        *,
        prefix: str = "telegram_avatar",
        default_suffix: str = ".jpg",
        allowed_suffixes: set[str] | None = None,
    ) -> str:
        async with self._file_locks_guard:
            lock = self._file_locks.get(cache_key)
            if lock is None:
                lock = asyncio.Lock()
                self._file_locks[cache_key] = lock
                self._file_lock_refs[cache_key] = 0
            self._file_lock_refs[cache_key] += 1
        try:
            async with lock:
                return await self._cache_telegram_file_unlocked(
                    file_obj,
                    cache_key,
                    prefix=prefix,
                    default_suffix=default_suffix,
                    allowed_suffixes=allowed_suffixes,
                )
        finally:
            async with self._file_locks_guard:
                refs = self._file_lock_refs.get(cache_key, 1) - 1
                if refs <= 0:
                    self._file_lock_refs.pop(cache_key, None)
                    self._file_locks.pop(cache_key, None)
                else:
                    self._file_lock_refs[cache_key] = refs

    async def _cache_telegram_file_unlocked(
        self,
        file_obj,
        cache_key: str,
        *,
        prefix: str = "telegram_avatar",
        default_suffix: str = ".jpg",
        allowed_suffixes: set[str] | None = None,
    ) -> str:
        if file_obj is None:
            return ""
        media_cache = getattr(self.plugin, "_media_cache", None)
        cache_dir = getattr(media_cache, "cache_dir", None)
        if cache_dir is None:
            return ""

        allowed_suffixes = allowed_suffixes or {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif",
        }
        file_path = str(getattr(file_obj, "file_path", "") or "")
        suffix = Path(urlparse(file_path).path).suffix.lower()
        if suffix not in allowed_suffixes:
            suffix = default_suffix
        if suffix == ".jpeg":
            suffix = ".jpg"

        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:32]
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(Exception):
            cache_dir.chmod(0o700)
        dest_path = cache_dir / f"{prefix}_{digest}{suffix}"
        relative_url = f"/static/cache/{dest_path.name}"
        is_avatar = prefix == "telegram_avatar"
        stale_avatar_exists = False
        if dest_path.exists() and not is_avatar:
            return relative_url
        if dest_path.exists() and is_avatar:
            try:
                age = max(0.0, time.time() - dest_path.stat().st_mtime)
            except OSError:
                age = self._AVATAR_HIT_TTL
            if age < self._AVATAR_HIT_TTL:
                return relative_url
            stale_avatar_exists = True

        file_size = int(getattr(file_obj, "file_size", 0) or 0)
        if prefix != "telegram_avatar":
            max_bytes = 0
            get_max_media_bytes = getattr(media_cache, "get_max_media_bytes", None)
            if callable(get_max_media_bytes):
                with suppress(Exception):
                    max_bytes = int(get_max_media_bytes() or 0)
            if max_bytes and file_size > max_bytes:
                logger.warning(
                    f"Chat Archive: 拒绝缓存超大 Telegram 媒体 size={file_size}"
                )
                return ""

        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
        tmp_path.unlink(missing_ok=True)
        reserve_capacity = getattr(media_cache, "reserve_cache_capacity", None)
        finalize_cache_file = getattr(media_cache, "finalize_cache_file", None)
        release_reservation = getattr(media_cache, "release_cache_reservation", None)
        use_reservation = all(
            callable(method)
            for method in (
                reserve_capacity,
                finalize_cache_file,
                release_reservation,
            )
        )
        ensure_capacity = getattr(media_cache, "ensure_cache_capacity", None)
        reservation_active = False
        if use_reservation:
            capacity_ok = reserve_capacity(tmp_path, file_size)
            if inspect.isawaitable(capacity_ok):
                capacity_ok = await capacity_ok
            reservation_active = bool(capacity_ok)
        elif callable(ensure_capacity):
            capacity_ok = ensure_capacity(file_size)
            if inspect.isawaitable(capacity_ok):
                capacity_ok = await capacity_ok
        else:
            capacity_ok = True
        if not capacity_ok:
            logger.warning("Chat Archive: Telegram 媒体缓存总容量不足，拒绝下载")
            return relative_url if stale_avatar_exists and dest_path.exists() else ""

        try:
            download = getattr(file_obj, "download_to_drive", None)
            if callable(download):
                with suppress(Exception):
                    tmp_path.touch(mode=0o600, exist_ok=True)
                result = download(custom_path=str(tmp_path))
                if inspect.isawaitable(result):
                    await result
            else:
                download_bytes = getattr(file_obj, "download_as_bytearray", None)
                if not callable(download_bytes):
                    return ""
                data = download_bytes()
                if inspect.isawaitable(data):
                    data = await data
                max_bytes = 0
                if prefix != "telegram_avatar":
                    get_max_media_bytes = getattr(
                        media_cache, "get_max_media_bytes", None
                    )
                    if callable(get_max_media_bytes):
                        with suppress(Exception):
                            max_bytes = int(get_max_media_bytes() or 0)
                if max_bytes and len(data) > max_bytes:
                    logger.warning(
                        f"Chat Archive: 拒绝缓存超大 Telegram 媒体 size={len(data)}"
                    )
                    return ""
                with suppress(Exception):
                    tmp_path.touch(mode=0o600, exist_ok=True)
                await asyncio.to_thread(tmp_path.write_bytes, bytes(data))
            if prefix != "telegram_avatar":
                max_bytes = 0
                get_max_media_bytes = getattr(media_cache, "get_max_media_bytes", None)
                if callable(get_max_media_bytes):
                    with suppress(Exception):
                        max_bytes = int(get_max_media_bytes() or 0)
                if max_bytes and tmp_path.stat().st_size > max_bytes:
                    actual_size = tmp_path.stat().st_size
                    tmp_path.unlink(missing_ok=True)
                    logger.warning(
                        f"Chat Archive: 拒绝缓存超大 Telegram 媒体 size={actual_size}"
                    )
                    return ""
            if use_reservation:
                capacity_ok = finalize_cache_file(tmp_path, dest_path)
                if inspect.isawaitable(capacity_ok):
                    capacity_ok = await capacity_ok
                reservation_active = False
            elif callable(ensure_capacity):
                # Fresh .tmp bytes are already counted by the shared quota scan.
                capacity_ok = ensure_capacity(0)
                if inspect.isawaitable(capacity_ok):
                    capacity_ok = await capacity_ok
                if not capacity_ok:
                    tmp_path.unlink(missing_ok=True)
                    logger.warning(
                        "Chat Archive: Telegram 媒体缓存超过总容量，拒绝落盘"
                    )
                    return (
                        relative_url
                        if stale_avatar_exists and dest_path.exists()
                        else ""
                    )
            if not use_reservation:
                tmp_path.replace(dest_path)
            with suppress(Exception):
                dest_path.chmod(0o600)
            return relative_url
        except asyncio.CancelledError:
            with suppress(Exception):
                tmp_path.unlink(missing_ok=True)
            raise
        except Exception as e:
            with suppress(Exception):
                tmp_path.unlink(missing_ok=True)
            logger.debug(f"Chat Archive: 缓存 Telegram 文件失败: {e}")
            return relative_url if stale_avatar_exists and dest_path.exists() else ""
        finally:
            if reservation_active:
                released = release_reservation(tmp_path)
                if inspect.isawaitable(released):
                    await released

    async def _message_to_archive_text(
        self,
        message,
        bot=None,
        *,
        cache_media: bool = True,
    ) -> str:
        parts = []
        text = getattr(message, "text", None) or getattr(message, "caption", None)
        if text:
            parts.append(str(text))

        await self._append_media_parts(
            message,
            parts,
            bot=bot,
            cache_media=cache_media,
        )
        return "".join(parts) or "[Telegram 频道消息]"

    async def _append_media_parts(
        self,
        message,
        parts: list[str],
        *,
        bot=None,
        cache_media: bool,
    ) -> None:
        photos = getattr(message, "photo", None)
        if photos:
            photo = photos[-1]
            await self._append_media_cq(
                parts,
                "image",
                photo,
                "[CQ:image]",
                bot=bot,
                cache_media=cache_media,
            )

        video = getattr(message, "video", None) or getattr(message, "animation", None)
        if video:
            await self._append_media_cq(
                parts,
                "video",
                video,
                "[CQ:video]",
                bot=bot,
                cache_media=cache_media,
            )

        voice = getattr(message, "voice", None) or getattr(message, "audio", None)
        if voice:
            await self._append_media_cq(
                parts,
                "record",
                voice,
                "[语音]",
                bot=bot,
                cache_media=cache_media,
            )

        document = getattr(message, "document", None)
        if document:
            name = getattr(document, "file_name", "") or "文件"
            await self._append_file_cq(
                parts,
                document,
                name,
                bot=bot,
                cache_media=cache_media,
            )

        sticker = getattr(message, "sticker", None)
        if sticker:
            await self._append_media_cq(
                parts,
                "image",
                sticker,
                "[CQ:image]",
                bot=bot,
                cache_media=cache_media,
            )
            emoji = getattr(sticker, "emoji", "")
            if emoji:
                parts.append(f"Sticker: {emoji}")

    async def _append_media_cq(
        self,
        parts: list[str],
        cq_type: str,
        media,
        fallback: str,
        *,
        bot=None,
        cache_media: bool,
    ) -> None:
        url = (
            await self._cache_telegram_media(media, cq_type, bot=bot)
            if cache_media
            else ""
        )
        if url:
            parts.append(f"[CQ:{cq_type},url={escape_cq_param(url)}]")
        else:
            parts.append(fallback)

    async def _append_file_cq(
        self,
        parts: list[str],
        document,
        name: str,
        *,
        bot=None,
        cache_media: bool,
    ) -> None:
        url = (
            await self._cache_telegram_media(document, "file", bot=bot)
            if cache_media
            else ""
        )
        if url:
            parts.append(
                f"[CQ:file,name={escape_cq_param(name)},url={escape_cq_param(url)}]"
            )
        else:
            parts.append(f"[文件: {name}]")

    async def _cache_telegram_media(self, media, cq_type: str, *, bot=None) -> str:
        file_obj = await self._media_to_file(media, bot)
        if file_obj is None:
            return ""
        cache_key = self._media_cache_key(media, file_obj, cq_type)
        default_suffix = ".mp4" if cq_type == "video" else ".jpg"
        if cq_type in {"record", "file"}:
            default_suffix = ".dat"
        return await self._cache_telegram_file(
            file_obj,
            cache_key,
            prefix="telegram_media",
            default_suffix=default_suffix,
            allowed_suffixes={
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".gif",
                ".mp4",
                ".mov",
                ".m4v",
                ".webm",
                ".ogg",
                ".oga",
                ".mp3",
                ".wav",
                ".dat",
                ".pdf",
                ".zip",
            },
        )

    @staticmethod
    def _media_cache_key(media, file_obj, cq_type: str) -> str:
        identifiers = [
            cq_type,
            str(getattr(media, "file_unique_id", "") or ""),
            str(getattr(media, "file_id", "") or ""),
            str(getattr(file_obj, "file_unique_id", "") or ""),
            str(getattr(file_obj, "file_id", "") or ""),
            str(getattr(file_obj, "file_path", "") or ""),
        ]
        identifiers = [part for part in identifiers if part]
        if not identifiers:
            identifiers = [str(time.time_ns())]
        return "telegram:media:" + ":".join(identifiers)

    @staticmethod
    async def _media_to_file(media, bot=None):
        get_file = getattr(media, "get_file", None)
        if callable(get_file):
            try:
                file_obj = get_file()
                if inspect.isawaitable(file_obj):
                    file_obj = await file_obj
                return file_obj
            except Exception as e:
                logger.debug(f"Chat Archive: 获取 Telegram 频道媒体文件失败: {e}")

        file_id = getattr(media, "file_id", "")
        get_file = getattr(bot, "get_file", None)
        if file_id and callable(get_file):
            try:
                file_obj = get_file(file_id)
                return await file_obj if inspect.isawaitable(file_obj) else file_obj
            except Exception as e:
                logger.debug(f"Chat Archive: 通过 bot 获取 Telegram 媒体文件失败: {e}")
        return None

    @staticmethod
    def _remove_handler(application, handler, group: int) -> None:
        remove_handler = getattr(application, "remove_handler", None)
        if callable(remove_handler):
            with suppress(Exception):
                remove_handler(handler, group=group)
