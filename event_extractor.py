from __future__ import annotations

import hashlib
import re
import time
from typing import Any

try:
    from .serializer import (
        escape_cq_param,
        sanitize_cq_media_codes,
        serialize_message_chain,
        serialize_onebot_message,
        unescape_cq_param,
    )
except ImportError:
    from serializer import (
        escape_cq_param,
        sanitize_cq_media_codes,
        serialize_message_chain,
        serialize_onebot_message,
        unescape_cq_param,
    )


_CQ_CACHEABLE_MEDIA_RE = re.compile(
    r"\[CQ:(image|video)(?:,([^\]]*))?\]",
    re.IGNORECASE,
)


class ArchiveEventExtractor:
    @staticmethod
    def _cq_param_match(inner: str, key: str):
        return re.search(
            rf"(?:^|,){re.escape(key)}=([^,\]]*)",
            str(inner or ""),
            re.IGNORECASE,
        )

    @classmethod
    def _cq_media_source(cls, inner: str) -> str:
        for key in ("url", "file"):
            match = cls._cq_param_match(inner, key)
            if match:
                return unescape_cq_param(match.group(1)).strip()
        return ""

    @staticmethod
    def _is_remote_media_source(value: str) -> bool:
        return bool(re.match(r"^https?://", str(value or "").strip(), re.IGNORECASE))

    @staticmethod
    def _is_transient_local_media_source(value: str) -> bool:
        source = str(value or "").strip()
        return bool(
            source.lower().startswith("file://")
            or source.startswith("/")
            or re.match(r"^[A-Za-z]:[\\/]", source)
        )

    @classmethod
    def _restore_platform_media_urls(
        cls,
        chain_message: str,
        platform_message: str,
    ) -> str:
        """Restore remote OneBot URLs replaced by AstrBot's temporary paths.

        AstrBot normalizes incoming images to files under ``data/temp`` before
        plugin handlers run. Those files are removed after the event finishes,
        while archive media caching happens in a background queue. The original
        OneBot payload still contains the durable remote URL, so pair media
        segments by type and occurrence and replace only unusable local sources.
        This keeps richer chain serialization, dimensions and surrounding text.
        """
        platform_sources: dict[str, list[str]] = {}
        for match in _CQ_CACHEABLE_MEDIA_RE.finditer(platform_message):
            media_type = match.group(1).lower()
            platform_sources.setdefault(media_type, []).append(
                cls._cq_media_source(match.group(2))
            )

        chain_counts: dict[str, int] = {}
        for match in _CQ_CACHEABLE_MEDIA_RE.finditer(chain_message):
            media_type = match.group(1).lower()
            chain_counts[media_type] = chain_counts.get(media_type, 0) + 1
        reconciled_types = {
            media_type
            for media_type, count in chain_counts.items()
            if count == len(platform_sources.get(media_type, []))
        }

        positions: dict[str, int] = {}

        def replace(match: re.Match) -> str:
            media_type = match.group(1).lower()
            if media_type not in reconciled_types:
                return match.group(0)
            index = positions.get(media_type, 0)
            positions[media_type] = index + 1
            candidates = platform_sources.get(media_type, [])
            if index >= len(candidates):
                return match.group(0)

            remote_source = candidates[index]
            if not cls._is_remote_media_source(remote_source):
                return match.group(0)

            inner = match.group(2)
            current_source = cls._cq_media_source(inner)
            if (
                not current_source
                or cls._is_remote_media_source(current_source)
                or current_source.startswith("/static/cache/")
                or not cls._is_transient_local_media_source(current_source)
            ):
                return match.group(0)

            source_match = None
            for key in ("url", "file"):
                source_match = cls._cq_param_match(inner, key)
                if source_match:
                    break
            if source_match is None:
                return match.group(0)

            start, end = source_match.span(1)
            restored_inner = (
                inner[:start] + escape_cq_param(remote_source) + inner[end:]
            )
            return f"[CQ:{match.group(1)},{restored_inner}]"

        return _CQ_CACHEABLE_MEDIA_RE.sub(replace, chain_message)

    @staticmethod
    def get_field(obj, key, default=None):
        """Extract fields from dict-like or attribute-based platform payloads."""
        if not obj:
            return default
        if hasattr(obj, "get") and callable(obj.get):
            res = obj.get(key)
            if res is not None:
                return res
        if hasattr(obj, "__getitem__"):
            try:
                res = obj[key]
                if res is not None:
                    return res
            except (KeyError, TypeError):
                pass
        return getattr(obj, key, default)

    @staticmethod
    def safe_event_call(event, method_name: str, default=None):
        method = getattr(event, method_name, None)
        if not callable(method):
            return default
        try:
            value = method()
        except Exception:
            return default
        return default if value is None else value

    @classmethod
    def nested_field(cls, obj, *keys):
        value = obj
        for key in keys:
            value = cls.get_field(value, key, None)
            if value is None:
                return None
        return value

    @staticmethod
    def extract_from_dirty_str(s, key):
        """Extract one key from stringified event payload fallbacks."""
        if not isinstance(s, str):
            return None
        patterns = [
            rf"['\"]{key}['\"]\s*:\s*['\"]([^'\"]*)['\"]",
            rf"['\"]{key}['\"]\s*:\s*(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, s)
            if match:
                return match.group(1)
        return None

    @classmethod
    def coerce_timestamp(cls, value, default: int | None = None) -> int:
        if default is None:
            default = int(time.time())
        if value is None or value == "":
            return int(default)
        try:
            if hasattr(value, "timestamp") and callable(value.timestamp):
                return int(value.timestamp())
        except Exception:
            pass
        try:
            number = float(value)
            # Telegram/Discord wrappers can expose millisecond timestamps; DB stores seconds.
            if number > 10_000_000_000:
                number = number / 1000
            return int(number)
        except (TypeError, ValueError):
            pass
        extracted = cls.extract_from_dirty_str(str(value), "time")
        if extracted:
            return cls.coerce_timestamp(extracted, default)
        return int(default)

    @classmethod
    def message_type_value(cls, event) -> str:
        msg_type = cls.safe_event_call(event, "get_message_type", "")
        value = getattr(msg_type, "value", msg_type)
        return str(value or "")

    @classmethod
    def event_session_id(cls, event) -> str:
        if not event:
            return ""
        for attr in ("unified_msg_origin", "session_id"):
            value = getattr(event, attr, "")
            if value:
                return str(value)
        return ""

    @classmethod
    def event_group_id(cls, event) -> str:
        group_id = cls.safe_event_call(event, "get_group_id", "")
        if group_id:
            return str(group_id)
        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", "")
        if group_id:
            return str(group_id)
        return ""

    @classmethod
    def event_sender_id(cls, event) -> str:
        if not event:
            return ""
        sender_id = cls.safe_event_call(event, "get_sender_id", "")
        if sender_id:
            return str(sender_id)
        return str(getattr(event, "sender_id", "") or "")

    @classmethod
    def event_raw_message_text(cls, event, platform_raw) -> str:
        raw_message = ""
        try:
            chain = event.get_messages()
            if chain:
                raw_message = serialize_message_chain(chain)
        except Exception:
            pass

        platform_message = serialize_onebot_message(
            cls.get_field(platform_raw, "message", "")
        )
        if raw_message and platform_message:
            raw_message = cls._restore_platform_media_urls(
                raw_message,
                platform_message,
            )
        if platform_message and (
            not raw_message
            or raw_message == "[合并转发]"
            or raw_message.startswith("[合并转发,id=")
        ):
            raw_message = platform_message

        if not raw_message:
            raw_message = cls.get_field(platform_raw, "raw_message", "")
            if isinstance(raw_message, str) and (
                raw_message.startswith("<Event") or raw_message.startswith("{")
            ):
                extracted = cls.extract_from_dirty_str(raw_message, "raw_message")
                if extracted:
                    raw_message = extracted

        if (
            not isinstance(raw_message, str)
            or not raw_message
            or raw_message.startswith("<Event")
        ):
            raw_message = (
                cls.safe_event_call(event, "get_message_str", "")
                or getattr(event, "message_str", "")
                or getattr(getattr(event, "message_obj", None), "message_str", "")
                or cls.safe_event_call(event, "get_message_outline", "")
            )
            if isinstance(raw_message, str) and raw_message.startswith("<Event"):
                extracted = cls.extract_from_dirty_str(raw_message, "raw_message")
                raw_message = extracted if extracted else "[无法解析的消息]"

        return sanitize_cq_media_codes(str(raw_message or ""))

    @classmethod
    def telegram_raw_message_obj(cls, platform_raw):
        if not platform_raw:
            return None
        return (
            getattr(platform_raw, "message", None)
            or cls.get_field(platform_raw, "message", None)
            or platform_raw
        )

    @classmethod
    def event_timestamp(cls, event, platform_raw) -> int:
        message_obj = getattr(event, "message_obj", None)
        default = int(time.time())
        candidates = [
            cls.nested_field(platform_raw, "message", "date"),
            cls.nested_field(platform_raw, "effective_message", "date"),
            cls.nested_field(platform_raw, "message", "created_at"),
            cls.get_field(platform_raw, "created_at", None),
            cls.get_field(platform_raw, "date", None),
            cls.get_field(platform_raw, "time", None),
            getattr(message_obj, "timestamp", None),
        ]
        for candidate in candidates:
            if candidate not in (None, ""):
                return cls.coerce_timestamp(candidate, default)
        return default

    @classmethod
    def event_session_name(cls, event, platform_raw, message_type: str) -> str:
        message_obj = getattr(event, "message_obj", None)
        platform_name = str(
            cls.safe_event_call(event, "get_platform_name", "") or ""
        ).lower()
        is_group = "group" in str(message_type).lower()
        telegram_message = cls.telegram_raw_message_obj(platform_raw)

        candidates = [
            cls.safe_event_call(event, "get_group_name", ""),
            getattr(getattr(message_obj, "group", None), "group_name", ""),
            cls.nested_field(platform_raw, "effective_chat", "title"),
            cls.nested_field(platform_raw, "message", "chat", "title"),
            cls.nested_field(platform_raw, "chat", "title"),
        ]

        if platform_name == "telegram":
            candidates.extend(
                [
                    cls.nested_field(telegram_message, "chat", "title"),
                    cls.nested_field(telegram_message, "chat", "full_name"),
                    cls.nested_field(telegram_message, "sender_chat", "title"),
                ]
            )

        if platform_name == "discord":
            guild_name = cls.get_field(
                cls.get_field(platform_raw, "guild", None), "name", ""
            )
            channel_name = cls.get_field(
                cls.get_field(platform_raw, "channel", None), "name", ""
            )
            if guild_name and channel_name:
                candidates.append(f"{guild_name} / #{channel_name}")
            candidates.append(channel_name)

        if not is_group:
            candidates.extend(
                [
                    cls.safe_event_call(event, "get_sender_name", ""),
                    cls.nested_field(platform_raw, "message", "chat", "username"),
                    cls.nested_field(platform_raw, "message", "from_user", "username"),
                ]
            )

        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return ""

    @classmethod
    def event_message_id(
        cls, event, platform_raw, user_id: str, timestamp: int, raw_message: str
    ) -> str:
        message_obj = getattr(event, "message_obj", None)
        candidates = [
            getattr(message_obj, "message_id", ""),
            cls.nested_field(platform_raw, "message", "message_id"),
            cls.get_field(platform_raw, "message_id", ""),
            cls.get_field(platform_raw, "id", ""),
            cls.nested_field(platform_raw, "interaction", "id"),
        ]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text

        platform_id = str(cls.safe_event_call(event, "get_platform_id", "") or "")
        digest = hashlib.sha1(raw_message.encode("utf-8", errors="ignore")).hexdigest()[
            :12
        ]
        return f"{platform_id}:{user_id}:{timestamp}:{digest}"

    @classmethod
    def archive_record_from_event(
        cls, event, *, raw_message: str | None = None
    ) -> dict[str, Any]:
        platform_raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        platform_id = str(cls.safe_event_call(event, "get_platform_id", "") or "")
        platform_name = str(cls.safe_event_call(event, "get_platform_name", "") or "")
        user_id = str(cls.safe_event_call(event, "get_sender_id", "") or "")
        nickname = str(cls.safe_event_call(event, "get_sender_name", "") or "")
        message_type = cls.message_type_value(event)
        session_id = cls.event_session_id(event)
        if not session_id:
            raw_session_id = (
                cls.event_group_id(event)
                or str(cls.safe_event_call(event, "get_session_id", "") or "")
                or str(
                    getattr(getattr(event, "message_obj", None), "session_id", "") or ""
                )
            )
            if raw_session_id and platform_id and message_type:
                session_id = f"{platform_id}:{message_type}:{raw_session_id}"
            else:
                session_id = raw_session_id

        if isinstance(user_id, str) and (
            user_id.startswith("<Event") or user_id.startswith("{")
        ):
            extracted = cls.extract_from_dirty_str(user_id, "user_id")
            if extracted:
                user_id = extracted

        if raw_message is None:
            raw_message = cls.event_raw_message_text(event, platform_raw)

        timestamp = cls.event_timestamp(event, platform_raw)
        session_name = cls.event_session_name(event, platform_raw, message_type)
        msg_id = cls.event_message_id(
            event, platform_raw, user_id, timestamp, raw_message
        )

        user_avatar = ""
        guild_avatar = ""
        if platform_name.lower() == "discord":
            # Extract user avatar
            author = getattr(platform_raw, "author", None)
            if author:
                for attr in ("display_avatar", "avatar"):
                    val = getattr(author, attr, None)
                    if val:
                        url = getattr(val, "url", None)
                        if url:
                            user_avatar = str(url)
                            break
                        elif isinstance(val, str) and val.startswith("http"):
                            user_avatar = val
                            break
                        elif hasattr(val, "__str__"):
                            s_val = str(val)
                            if s_val.startswith("http"):
                                user_avatar = s_val
                                break
            if not user_avatar and isinstance(platform_raw, dict):
                author_dict = platform_raw.get("author")
                if isinstance(author_dict, dict):
                    a_hash = author_dict.get("avatar")
                    if a_hash and user_id:
                        user_avatar = (
                            f"https://cdn.discordapp.com/avatars/{user_id}/{a_hash}.png"
                        )

            # Extract guild avatar (server's icon)
            guild = getattr(platform_raw, "guild", None)
            if guild:
                icon = getattr(guild, "icon", None)
                if icon:
                    url = getattr(icon, "url", None)
                    if url:
                        guild_avatar = str(url)
                    elif isinstance(icon, str) and icon.startswith("http"):
                        guild_avatar = icon
                    elif hasattr(icon, "__str__"):
                        s_icon = str(icon)
                        if s_icon.startswith("http"):
                            guild_avatar = s_icon
            if not guild_avatar and isinstance(platform_raw, dict):
                guild_dict = platform_raw.get("guild")
                if isinstance(guild_dict, dict):
                    g_id = guild_dict.get("id")
                    g_icon = guild_dict.get("icon")
                    if g_id and g_icon:
                        guild_avatar = (
                            f"https://cdn.discordapp.com/icons/{g_id}/{g_icon}.png"
                        )

        return {
            "user_id": str(user_id or ""),
            "sender_name": str(nickname or ""),
            "message": sanitize_cq_media_codes(str(raw_message or "")),
            "timestamp": int(timestamp),
            "session_id": str(session_id or ""),
            "message_type": str(message_type or ""),
            "session_name": str(session_name or ""),
            "msg_id": str(msg_id or ""),
            "platform_id": str(platform_id or ""),
            "platform_name": str(platform_name or ""),
            "avatar_url": user_avatar,
            "guild_avatar_url": guild_avatar,
        }

    @staticmethod
    def archive_record_tuple(record: dict[str, Any]) -> tuple:
        return (
            str(record.get("user_id", "")),
            str(record.get("sender_name", "")),
            str(record.get("message", "")),
            int(record.get("timestamp") or time.time()),
            str(record.get("session_id", "")),
            str(record.get("message_type", "")),
            str(record.get("session_name", "")),
            str(record.get("msg_id", "")),
            str(record.get("platform_id", "")),
            str(record.get("platform_name", "")),
            str(record.get("avatar_url", "")),
            str(record.get("guild_avatar_url", "")),
        )
