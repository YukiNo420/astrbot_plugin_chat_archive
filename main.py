from __future__ import annotations

import asyncio
import datetime
import json
import time
import os
from functools import lru_cache
from pathlib import Path
from contextlib import contextmanager
from typing import Any

try:
    from .forward_archive import expand_forward_payload
except ImportError:
    from forward_archive import expand_forward_payload

try:
    from .config import get_data_dir, get_static_cache_dir
except ImportError:
    from config import get_data_dir, get_static_cache_dir

try:
    from .batch_writer import ArchiveBatchWriter
except ImportError:
    from batch_writer import ArchiveBatchWriter

try:
    from .media_cache import ArchiveMediaCache
except ImportError:
    from media_cache import ArchiveMediaCache

try:
    from .event_extractor import ArchiveEventExtractor
except ImportError:
    from event_extractor import ArchiveEventExtractor

try:
    from .telegram_channel_capture import TelegramChannelCapture
except ImportError:
    from telegram_channel_capture import TelegramChannelCapture

try:
    from .db_config import get_db_connection, init_db, DatabaseManager
except ImportError:
    try:
        from db_config import get_db_connection, init_db, DatabaseManager
    except Exception:
        raise RuntimeError("无法加载 db_config 模块，请确保插件包完整。")

from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType

try:
    from .llm_tools import register_archive_tools
except ImportError:
    from llm_tools import register_archive_tools

try:
    from .serializer import (
        cq_param_exists,
        escape_cq_param,
        positive_int,
        read_image_dimensions,
        serialize_message_chain,
        serialize_onebot_message,
        valid_image_dimensions,
    )
except ImportError:
    from serializer import (
        cq_param_exists,
        escape_cq_param,
        positive_int,
        read_image_dimensions,
        serialize_message_chain,
        serialize_onebot_message,
        valid_image_dimensions,
    )

# 尝试导入 web server
try:
    from .web.server import AdminServer
except ImportError:
    AdminServer = None

from astrbot.api import logger

DATA_DIR = get_data_dir()
STATIC_CACHE_DIR = get_static_cache_dir()


@register("astrbot_plugin_chat_archive", "yukino42", "高性能聊天消息存档插件", "v1.4.1")
class ChatArchivePlugin(Star):
    # Batch writer configuration
    _BATCH_SIZE = 50
    _FLUSH_INTERVAL = 2.0  # seconds

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.conf = config

        self._shutting_down = False

        # Track active background tasks to avoid garbage collection and 'Task was destroyed but it is pending' errors
        self._background_tasks = set()

        # Write buffer: queue + background writer
        self._writer = ArchiveBatchWriter(
            batch_size=self._BATCH_SIZE,
            flush_interval=self._FLUSH_INTERVAL,
            data_dir=DATA_DIR,
        )
        self._write_queue = self._writer.queue
        self._media_cache = ArchiveMediaCache(config=self.conf, cache_dir=STATIC_CACHE_DIR)
        self._event_extractor = ArchiveEventExtractor()
        self._telegram_channel_capture = TelegramChannelCapture(self)
        self._telegram_channel_task = None

        # 插件启动时，显式调用建表逻辑，确保数据库表结构就绪
        try:
            init_db()
            logger.info("Chat Archive: 数据库与表结构初始化成功。")
        except Exception as e:
            logger.error(f"Chat Archive: 初始化数据库失败: {e}")

        self._register_llm_tools()

        # Cache blacklist as set for O(1) lookup
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        self._ignored_users: set[str] = {
            str(u) for u in basic_conf.get("ignored_users", [])
        }

        self.web_server = None
        if AdminServer:
            web_conf = self.conf.get("web_server", {}) if self.conf else {}
            if web_conf.get("enable", True):
                host = os.environ.get("ARCHIVE_HOST", "").strip() or web_conf.get("host", "127.0.0.1")
                try:
                    port = int(os.environ.get("ARCHIVE_PORT", "").strip() or web_conf.get("port", 8090))
                except (TypeError, ValueError):
                    port = 8090
                api_key = os.environ.get("ARCHIVE_API_KEY", "").strip() or web_conf.get("api_key", "")

                if not api_key:
                    import secrets
                    api_key = secrets.token_urlsafe(16)
                    logger.warning("\n" + "=" * 60 +
                                   f"\n[Chat Archive 安全警告] 您未在配置中指定访问验证 Key (api_key)！"
                                   f"\n为了保障您的聊天记录隐私，系统已自动生成一个强随机密码："
                                   f"\n👉👉 {api_key} 👈👈"
                                   f"\n请使用上述密码登录 Web 仪表盘。您随时可以在插件的配置选项中设置自定义的 api_key。"
                                   "\n" + "=" * 60 + "\n")

                self.web_server = AdminServer(
                    plugin_instance=self, host=host, port=port, api_key=api_key, cache_dir=STATIC_CACHE_DIR
                )
                self.web_server.run_in_thread()
            else:
                logger.info("Chat Archive: 内置 Web 面板已禁用（已通过外部或 systemd 解耦运行）。")

        # Start clean-up background task
        self._clean_task = None
        try:
            loop = asyncio.get_running_loop()
            self._clean_task = loop.create_task(self._periodic_clean_loop())
            self._start_telegram_channel_capture_task(loop)
        except RuntimeError:
            pass

    async def initialize(self):
        self._start_telegram_channel_capture_task()
        await self._telegram_channel_capture.ensure_registered()

    def _start_telegram_channel_capture_task(self, loop=None):
        if self._telegram_channel_task and not self._telegram_channel_task.done():
            return
        try:
            loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            return
        self._telegram_channel_task = loop.create_task(
            self._telegram_channel_capture.run()
        )

    def _register_llm_tools(self):
        """注册聊天存档查询 LLM 工具到 AstrBot Context。"""
        try:
            tool_count = register_archive_tools(self.context, self)
            logger.info(f"Chat Archive: 已注册 {tool_count} 个 LLM 查询工具。")
        except Exception as e:
            logger.warning(f"Chat Archive: 注册 LLM 查询工具失败: {e}")

    @staticmethod
    def _extract_tool_event(context):
        """Best-effort extraction of the AstrBot event from an LLM tool context."""
        queue = [context]
        seen = set()
        event_attrs = ("event", "message_event", "astr_message_event")
        nested_attrs = ("context", "ctx", "data", "value", "payload", "run_context")

        while queue:
            obj = queue.pop(0)
            if obj is None:
                continue
            obj_id = id(obj)
            if obj_id in seen:
                continue
            seen.add(obj_id)

            if hasattr(obj, "unified_msg_origin") or hasattr(obj, "get_sender_id"):
                return obj

            for attr in event_attrs:
                try:
                    candidate = getattr(obj, attr, None)
                except Exception:
                    candidate = None
                if candidate is not None and (
                    hasattr(candidate, "unified_msg_origin")
                    or hasattr(candidate, "get_sender_id")
                ):
                    return candidate
                if candidate is not None and len(queue) < 32:
                    queue.append(candidate)

            for attr in nested_attrs:
                try:
                    candidate = getattr(obj, attr, None)
                except Exception:
                    candidate = None
                if candidate is not None and len(queue) < 32:
                    queue.append(candidate)

        return None

    @staticmethod
    def _event_session_id(event) -> str:
        return ArchiveEventExtractor.event_session_id(event)

    @staticmethod
    def _event_sender_id(event) -> str:
        return ArchiveEventExtractor.event_sender_id(event)

    @staticmethod
    @lru_cache(maxsize=1)
    def _load_admin_ids() -> frozenset[str]:
        admin_ids = set()
        try:
            config_dir = DATA_DIR.parent.parent / "config"
            if not config_dir.exists():
                config_dir = Path(__file__).resolve().parent.parent / "config"
            if config_dir.exists():
                for f in config_dir.glob("abconf_*.json"):
                    with open(f, "r", encoding="utf-8-sig") as fh:
                        data = json.load(fh)
                    for admin in data.get("admins_id", []):
                        admin_str = str(admin).strip()
                        if not admin_str:
                            continue
                        admin_ids.add(admin_str)
                        if admin_str.startswith("UID: "):
                            admin_ids.add(admin_str.replace("UID: ", "").strip())
        except Exception as e:
            logger.debug(f"Chat Archive: 加载管理员 ID 失败: {e}")
        return frozenset(admin_ids)

    def _is_admin_tool_context(self, context) -> bool:
        event = self._extract_tool_event(context)
        if not event:
            return False

        try:
            is_admin = getattr(event, "is_admin", None)
            if callable(is_admin) and is_admin():
                return True
        except Exception:
            pass

        sender_id = self._event_sender_id(event)
        return bool(sender_id and sender_id in self._load_admin_ids())

    def _prepare_archive_tool_query(
        self, context, kwargs: dict, require_session: bool = True
    ) -> tuple[bool, dict]:
        scoped_kwargs = dict(kwargs)
        event = self._extract_tool_event(context)
        current_session_id = self._event_session_id(event)
        is_admin = self._is_admin_tool_context(context)
        requested_session_id = str(scoped_kwargs.get("session_id") or "").strip()

        if requested_session_id:
            if current_session_id and requested_session_id == current_session_id:
                return True, scoped_kwargs
            if is_admin:
                return True, scoped_kwargs
            return False, {
                "error": "权限不足：只能查询当前会话的归档，跨会话查询需要管理员权限。"
            }

        if current_session_id:
            scoped_kwargs["session_id"] = current_session_id
            return True, scoped_kwargs

        if is_admin and not require_session:
            return True, scoped_kwargs

        return False, {"error": "无法确认当前会话，已拒绝归档查询以保护聊天隐私。"}

    def get_bot_ids(self) -> list[str]:
        bot_ids = ["bot", "99999", "astrbot"]
        try:
            context = getattr(self, "context", None)
            platform_manager = getattr(context, "platform_manager", None)
            if platform_manager:
                get_insts = getattr(platform_manager, "get_insts", None)
                insts = get_insts() if callable(get_insts) else getattr(platform_manager, "platform_insts", []) or []
                for inst in insts:
                    client = getattr(inst, "client", None)
                    if client:
                        bot_id = getattr(client, "id", None)
                        if bot_id:
                            bot_ids.append(str(bot_id))
                        self_id = getattr(client, "self_id", None)
                        if self_id:
                            bot_ids.append(str(self_id))
        except Exception as e:
            logger.debug(f"Chat Archive: Failed to get bot IDs: {e}")
        return list(set(bot_ids))

    async def terminate(self):
        self._shutting_down = True
        if self._telegram_channel_task and not self._telegram_channel_task.done():
            self._telegram_channel_task.cancel()
            try:
                await self._telegram_channel_task
            except asyncio.CancelledError:
                pass
        await self._telegram_channel_capture.stop()

        # Cancel the clean-up task
        if self._clean_task and not self._clean_task.done():
            self._clean_task.cancel()
            try:
                await self._clean_task
            except asyncio.CancelledError:
                pass

        # Wait for all background tasks to finish
        if self._background_tasks:
            pending_tasks = [t for t in self._background_tasks if not t.done()]
            if pending_tasks:
                logger.info(f"Chat Archive: 等待 {len(pending_tasks)} 个后台任务执行完毕...")
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # Cancel the batch writer and flush remaining messages
        await self._writer.stop()

        if self.web_server:
            await self.web_server.stop()

    async def _ensure_writer_started(self):
        await self._writer.start()

    async def _enqueue_record(self, record: tuple):
        if self._shutting_down:
            return
        await self._writer.enqueue(record)

    async def _batch_writer(self):
        await self._writer.run()

    def _write_failed_batch(self, batch: list[tuple], reason: str):
        self._writer.write_failed_batch(batch, reason)

    def _flush_batches_sync(self, records: list[tuple]):
        self._writer.flush_batches_sync(records)

    def _flush_batch_sync(self, batch: list[tuple]):
        self._writer.flush_batch_sync(batch)

    @staticmethod
    def _serialize_message_chain(chain) -> str:
        return serialize_message_chain(chain)

    @staticmethod
    def _escape_cq_param(value) -> str:
        return escape_cq_param(value)

    @staticmethod
    def _positive_int(value) -> int:
        return positive_int(value)

    @staticmethod
    def _valid_image_dimensions(width: int, height: int) -> bool:
        return valid_image_dimensions(width, height)

    @staticmethod
    def _read_image_dimensions(filepath: str) -> tuple[int, int] | None:
        return read_image_dimensions(filepath)

    @staticmethod
    def _cq_param_exists(inner: str, key: str) -> bool:
        return cq_param_exists(inner, key)

    @staticmethod
    def _extract_forward_ids_from_message(value) -> list[str]:
        ids: list[str] = []

        def add_id(candidate):
            text = str(candidate or "").strip()
            if text and text not in ids:
                ids.append(text)

        def walk(obj):
            if obj is None:
                return
            if isinstance(obj, list | tuple):
                for item in obj:
                    walk(item)
                return
            if isinstance(obj, dict):
                data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
                if str(obj.get("type") or "").lower() == "forward":
                    add_id(data.get("id") or data.get("res_id") or obj.get("id"))
                for key in ("message", "messages", "content", "nodes"):
                    walk(obj.get(key) or data.get(key))
                return

            cls_name = obj.__class__.__name__
            if cls_name == "Forward":
                add_id(getattr(obj, "id", "") or getattr(obj, "res_id", ""))
                return
            if cls_name == "Nodes":
                walk(getattr(obj, "nodes", []))
                return
            if cls_name == "Node":
                walk(getattr(obj, "content", []))

        walk(value)
        return ids

    @classmethod
    def _extract_forward_ids(cls, event, platform_raw) -> list[str]:
        ids: list[str] = []
        try:
            ids.extend(cls._extract_forward_ids_from_message(event.get_messages()))
        except Exception:
            pass
        ids.extend(cls._extract_forward_ids_from_message(cls._get_field(platform_raw, "message", "")))
        return list(dict.fromkeys(ids))

    @staticmethod
    def _forward_response_messages(response):
        if not response:
            return None
        payload = response
        if isinstance(payload, dict) and "data" in payload:
            payload = payload.get("data")
        if isinstance(payload, dict):
            return (
                payload.get("messages")
                or payload.get("message")
                or payload.get("nodes")
                or payload.get("content")
            )
        return payload

    async def _fetch_forward_archive_text(self, event, forward_id: str) -> str:
        call_action = self._resolve_onebot_call_action(event)
        if not callable(call_action):
            logger.warning(f"Chat Archive: 无法获取 OneBot call_action，跳过合并转发展开 {forward_id}")
            return ""

        param_candidates = [{"message_id": forward_id}, {"id": forward_id}]
        if str(forward_id).isdigit():
            numeric_id = int(forward_id)
            param_candidates.extend([{"message_id": numeric_id}, {"id": numeric_id}])

        for params in param_candidates:
            try:
                response = await call_action("get_forward_msg", **params)
            except Exception as e:
                logger.warning(
                    f"Chat Archive: 获取合并转发内容失败 {forward_id} {params}: {e}"
                )
                continue
            messages = self._forward_response_messages(response)
            try:
                messages = await asyncio.wait_for(
                    expand_forward_payload(messages, call_action), timeout=15
                )
            except asyncio.TimeoutError:
                logger.warning("Chat Archive: nested forward expansion timed out; preserving available content")
            text = serialize_onebot_message(messages).strip()
            if text:
                if text.startswith("[合并转发]\n"):
                    return text.replace("[合并转发]", f"[合并转发,id={escape_cq_param(forward_id)}]", 1)
                return f"[合并转发,id={escape_cq_param(forward_id)}]\n{text}\n[合并转发结束]"
            logger.warning(f"Chat Archive: get_forward_msg 返回空内容 {forward_id} {params}")
        return ""

    def _resolve_onebot_call_action(self, event):
        bot = getattr(event, "bot", None)
        for candidate in (bot, getattr(bot, "api", None)):
            call_action = getattr(candidate, "call_action", None)
            if callable(call_action):
                return call_action

        platform_id = str(self._safe_event_call(event, "get_platform_id", "") or "").lower()
        platform_name = str(self._safe_event_call(event, "get_platform_name", "") or "").lower()
        manager = getattr(self.context, "platform_manager", None)
        get_insts = getattr(manager, "get_insts", None)
        if not callable(get_insts):
            return None

        for inst in get_insts() or []:
            meta_fn = getattr(inst, "meta", None)
            meta = meta_fn() if callable(meta_fn) else None
            meta_id = str(getattr(meta, "id", "") or "").lower()
            meta_name = str(getattr(meta, "name", "") or "").lower()
            if not any((meta_id, meta_name)):
                continue
            # Prefer exact platform match; keep onebot-compatible aliases as fallback.
            matched = (
                (platform_id and platform_id in (meta_id, meta_name))
                or (platform_name and platform_name in (meta_id, meta_name))
                or meta_id in {"aiocqhttp", "onebot", "napcat", "qq"}
                or meta_name in {"aiocqhttp", "onebot", "napcat", "qq"}
            )
            if not matched:
                continue
            client = getattr(inst, "client", None)
            get_client = getattr(inst, "get_client", None)
            for candidate in (client, getattr(client, "api", None), get_client() if callable(get_client) else None):
                inst_call_action = getattr(candidate, "call_action", None)
                if callable(inst_call_action):
                    return inst_call_action
        return None

    async def _expand_forward_message_if_needed(self, event, record: dict[str, Any], platform_raw) -> None:
        message = str(record.get("message") or "")
        if "[合并转发" not in message:
            return

        forward_ids = self._extract_forward_ids(event, platform_raw)
        for forward_id in forward_ids:
            expanded = await self._fetch_forward_archive_text(event, forward_id)
            if expanded:
                record["message"] = expanded
                return

    @staticmethod
    @contextmanager
    def _db_conn():
        """Context manager for database connections."""
        conn = None
        try:
            conn = get_db_connection()
            yield conn
        finally:
            if conn:
                conn.close()

    def _get_allowed_media_domains(self) -> set[str]:
        return self._media_cache.get_allowed_media_domains()

    def _get_max_media_bytes(self) -> int:
        return self._media_cache.get_max_media_bytes()

    @staticmethod
    def _hostname_matches_allowlist(hostname: str, domains: set[str]) -> bool:
        return ArchiveMediaCache.hostname_matches_allowlist(hostname, domains)

    @staticmethod
    def _ip_is_public(ip) -> bool:
        return ArchiveMediaCache.ip_is_public(ip)

    async def _hostname_resolves_to_public_ips(self, hostname: str) -> bool:
        return await self._media_cache.hostname_resolves_to_public_ips(hostname)

    async def _download_media_to_cache(self, url: str) -> str:
        return await self._media_cache.download_media_to_cache(url)

    async def _replace_cq_media_url(self, match) -> str:
        return await self._media_cache.replace_cq_media_url(match)

    async def _process_and_cache_media_in_string(self, text: str) -> str:
        return await self._media_cache.process_and_cache_media_in_string(text)

    @staticmethod
    def _get_field(obj, key, default=None):
        return ArchiveEventExtractor.get_field(obj, key, default)

    @staticmethod
    def _normalize_db_record_tuple(record: tuple) -> tuple:
        return ArchiveBatchWriter.normalize_record_tuple(record)

    @staticmethod
    def _safe_event_call(event, method_name: str, default=None):
        return ArchiveEventExtractor.safe_event_call(event, method_name, default)

    @staticmethod
    def _nested_field(obj, *keys):
        return ArchiveEventExtractor.nested_field(obj, *keys)

    @staticmethod
    def _coerce_timestamp(value, default: int | None = None) -> int:
        return ArchiveEventExtractor.coerce_timestamp(value, default)

    @staticmethod
    def _message_type_value(event) -> str:
        return ArchiveEventExtractor.message_type_value(event)

    def _event_raw_message_text(self, event, platform_raw) -> str:
        return self._event_extractor.event_raw_message_text(event, platform_raw)

    def _event_timestamp(self, event, platform_raw) -> int:
        return self._event_extractor.event_timestamp(event, platform_raw)

    def _event_session_name(self, event, platform_raw, message_type: str) -> str:
        return self._event_extractor.event_session_name(event, platform_raw, message_type)

    def _event_message_id(self, event, platform_raw, user_id: str, timestamp: int, raw_message: str) -> str:
        return self._event_extractor.event_message_id(
            event, platform_raw, user_id, timestamp, raw_message
        )

    def _archive_record_from_event(self, event, *, raw_message: str | None = None) -> dict[str, Any]:
        return self._event_extractor.archive_record_from_event(
            event, raw_message=raw_message
        )

    @staticmethod
    def _archive_record_tuple(record: dict[str, Any]) -> tuple:
        return ArchiveEventExtractor.archive_record_tuple(record)

    @staticmethod
    def _extract_from_dirty_str(s, key):
        return ArchiveEventExtractor.extract_from_dirty_str(s, key)

    @filter.event_message_type(EventMessageType.ALL)
    async def record_message(self, event: AstrMessageEvent):
        """
        拦截所有的消息事件，并存档至数据库。
        """
        # 读取配置
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        if not basic_conf.get("enable_archive", True):
            return

        cache_media = basic_conf.get("cache_media", False)

        record = self._archive_record_from_event(event)
        user_id = record["user_id"]
        if str(record.get("platform_name", "")).lower() == "telegram":
            avatar_url = await self._telegram_channel_capture.resolve_event_avatar(event)
            if avatar_url:
                record["avatar_url"] = avatar_url
            if record.get("message_type") in ("group", "GroupMessage", "channel", "ChannelMessage"):
                guild_avatar_url = await self._telegram_channel_capture.resolve_chat_avatar(event)
                if guild_avatar_url:
                    record["guild_avatar_url"] = guild_avatar_url

        # O(1) blacklist lookup via cached set
        if user_id in self._ignored_users:
            return

        platform_raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        await self._expand_forward_message_if_needed(event, record, platform_raw)

        # 6. 处理消息回撤事件 (Notice Event in OneBot 11)
        try:
            notice_type = self._get_field(platform_raw, "notice_type", "")
            if notice_type in ["group_recall", "friend_recall"]:
                recalled_msg_id = str(self._get_field(platform_raw, "message_id", ""))
                if recalled_msg_id:
                    # 将对应的原始消息标记为已撤回
                    def mark_recalled(m_id, sess_id):
                        for attempt in range(5):
                            c = None
                            try:
                                c = get_db_connection()
                                c.execute(
                                    "UPDATE chat_history SET is_recalled = 1 "
                                    "WHERE msg_id = ? AND session_id = ? AND user_id != '0'",
                                    (m_id, str(sess_id)),
                                )
                                c.commit()
                                row = c.execute(
                                    "SELECT id FROM chat_history "
                                    "WHERE msg_id = ? AND session_id = ? AND user_id != '0' "
                                    "AND is_recalled = 1 LIMIT 1",
                                    (m_id, str(sess_id)),
                                ).fetchone()
                                if row:
                                    return
                            except Exception as e:
                                logger.error(
                                    f"Chat Archive: 标记撤回失败 {m_id}: {e}"
                                )
                            finally:
                                if c:
                                    c.close()
                            time.sleep(0.5 * (attempt + 1))
                        logger.warning(f"Chat Archive: 未找到可标记撤回的消息 {m_id}")

                    if not self._shutting_down:
                        task = asyncio.create_task(
                            asyncio.to_thread(mark_recalled, recalled_msg_id, record["session_id"])
                        )
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)

                    # 同时记录一条系统消息
                    record["message"] = f"🛡️ [撤回了一条消息 (ID: {recalled_msg_id})]"
                    # 让系统消息的记录也进入队列，但标记为系统类型
                    record["user_id"] = "0"
                    record["sender_name"] = "系统通知"
                    record["msg_id"] = f"recall_{recalled_msg_id}"
        except Exception as e:
            logger.error(f"Chat Archive: 撤回检测异常: {e}")

        # 6.5. 缓存媒体原文件 (如果配置开启)
        if cache_media and record["message"]:
            try:
                record["message"] = await self._process_and_cache_media_in_string(record["message"])
            except Exception as e:
                logger.error(f"Chat Archive: 缓存媒体文件失败: {e}")

        if not record["user_id"]:
            return

        # Enqueue for batch write
        await self._ensure_writer_started()
        await self._enqueue_record(self._archive_record_tuple(record))

    @filter.after_message_sent()
    async def handle_bot_reply(self, event: AstrMessageEvent):
        """
        在机器人成功发送消息后，捕获并存档机器人自己的回复。
        """
        # 读取配置，检查是否开启存档
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        if not basic_conf.get("enable_archive", True):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        try:
            # 1. 序列化消息链
            raw_message = self._serialize_message_chain(result.chain)
            if not raw_message:
                return

            # 2. 检查是否开启缓存媒体文件并执行缓存
            cache_media = basic_conf.get("cache_media", False)
            if cache_media:
                try:
                    raw_message = await self._process_and_cache_media_in_string(raw_message)
                except Exception as e:
                    logger.error(f"Chat Archive: 缓存机器人媒体回复失败: {e}")

            record = self._archive_record_from_event(event, raw_message=raw_message)

            # 3. 构造机器人自己的基本信息
            user_id = str(self._safe_event_call(event, "get_self_id", "") or "bot")
            record["user_id"] = user_id
            
            # 获取机器人自身的昵称，如果获取不到则使用配置或默认名称
            nickname = "Bot"
            try:
                if self.context and hasattr(self.context, "get_self_nickname"):
                    nickname = self.context.get_self_nickname() or nickname
                elif self.context and hasattr(self.context, "get_bot_name"):
                    nickname = self.context.get_bot_name() or nickname
            except Exception:
                pass
            record["sender_name"] = str(nickname)
            if str(record.get("platform_name", "")).lower() == "telegram":
                avatar_url = await self._telegram_channel_capture.resolve_bot_avatar(event)
                if avatar_url:
                    record["avatar_url"] = avatar_url
                if record.get("message_type") in ("group", "GroupMessage", "channel", "ChannelMessage"):
                    guild_avatar_url = await self._telegram_channel_capture.resolve_chat_avatar(event)
                    if guild_avatar_url:
                        record["guild_avatar_url"] = guild_avatar_url

            # 消息 ID，对于机器人回复，尽量从 platform 侧结果获取，没有则生成临时 ID
            timestamp = int(time.time())
            msg_id = f"bot_{timestamp}_{int(time.time() * 1000) % 1000}"
            record["timestamp"] = timestamp
            record["msg_id"] = msg_id

            # 4. Enqueue for batch write
            await self._ensure_writer_started()
            await self._enqueue_record(self._archive_record_tuple(record))
            
        except Exception as e:
            logger.error(f"Chat Archive: 记录机器人回复消息异常: {e}", exc_info=True)

    # ==========================
    # Third-party Plugin API (Python API)
    # Access via: self.context.get_registered_star("astrbot_plugin_chat_archive")
    # ==========================

    @classmethod
    def get_history(cls, *args, **kwargs) -> list[dict]:
        """Advanced mixed query interface for chat history."""
        return DatabaseManager.get_history(*args, **kwargs)

    @classmethod
    def get_sessions(cls) -> list[dict]:
        """Get all sessions that have archived chat records."""
        return DatabaseManager.get_sessions()

    @classmethod
    def get_member_rank(cls, *args, **kwargs) -> list[dict]:
        """Get the top active members in a session by message count."""
        return DatabaseManager.get_member_rank(*args, **kwargs)

    @classmethod
    def get_user_summary(cls, *args, **kwargs) -> dict:
        """Get a statistical overview of a specific user."""
        return DatabaseManager.get_user_summary(*args, **kwargs)

    @classmethod
    def get_message_count(cls, *args, **kwargs) -> int:
        """Lightweight count query without fetching message data."""
        return DatabaseManager.get_message_count(*args, **kwargs)

    @classmethod
    def get_context_messages(cls, *args, **kwargs) -> list[tuple[str, str, str]]:
        """Convenience method for LLM context: returns formatted messages."""
        return DatabaseManager.get_context_messages(*args, **kwargs)

    @classmethod
    def _execute_query(cls, *args, **kwargs) -> dict:
        """Execute the actual database query (runs in thread)."""
        return DatabaseManager.execute_query(*args, **kwargs)

    async def _periodic_clean_loop(self):
        """定期清理过期缓存文件的循环。每一天运行一次。"""
        # Wait a small duration initially to let startup finish
        await asyncio.sleep(5)
        while True:
            try:
                basic_conf = self.conf.get("basic", {}) if self.conf else {}
                enable_clean = basic_conf.get("enable_clean", False)
                clean_days = basic_conf.get("clean_days", 30)

                if enable_clean and clean_days > 0:
                    await self._clean_expired_cache(clean_days)
            except Exception as e:
                logger.error(f"Chat Archive: 定期清理执行异常: {e}")
            
            # Sleep for 24 hours (86400 seconds)
            await asyncio.sleep(86400)

    async def _clean_expired_cache(self, days: int):
        """物理清理几天前的缓存文件"""
        if not STATIC_CACHE_DIR.exists():
            return

        now = time.time()
        threshold = now - (days * 86400)

        try:
            def _scan_and_delete():
                """在线程池中执行文件扫描与删除，避免阻塞事件循环。"""
                cleaned_count = 0
                cleaned_bytes = 0
                for p in STATIC_CACHE_DIR.glob("*"):
                    if p.is_file() and p.suffix != ".tmp":
                        try:
                            st = p.stat()
                            if st.st_mtime < threshold:
                                cleaned_bytes += st.st_size
                                p.unlink()
                                cleaned_count += 1
                        except Exception as e:
                            logger.debug(f"Chat Archive: 无法删除文件 {p}: {e}")
                return cleaned_count, cleaned_bytes

            cleaned_count, cleaned_bytes = await asyncio.to_thread(_scan_and_delete)

            if cleaned_count > 0:
                logger.info(f"Chat Archive: 清理了 {cleaned_count} 个过期媒体文件，释放空间 {cleaned_bytes / (1024 * 1024):.2f} MB。")
        except Exception as e:
            logger.error(f"Chat Archive: 清理过期缓存文件失败: {e}")
