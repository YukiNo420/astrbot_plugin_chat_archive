from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType

try:
    from .forward_archive import expand_forward_payload, replace_forward_reference
except ImportError:
    from forward_archive import expand_forward_payload, replace_forward_reference

try:
    from .config import get_data_dir, get_static_cache_dir, migrate_legacy_storage
except ImportError:
    from config import get_data_dir, get_static_cache_dir, migrate_legacy_storage

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
    from .db_config import DB_PATH, DatabaseManager, get_db_connection, init_db
except ImportError:
    try:
        from db_config import DB_PATH, DatabaseManager, get_db_connection, init_db
    except Exception:
        raise RuntimeError("无法加载 db_config 模块，请确保插件包完整。")

try:
    from .llm_tools import register_archive_tools
except ImportError:
    from llm_tools import register_archive_tools

try:
    from .serializer import (
        escape_cq_param,
        serialize_message_chain,
        serialize_onebot_message,
    )
except ImportError:
    from serializer import (
        escape_cq_param,
        serialize_message_chain,
        serialize_onebot_message,
    )

# 尝试导入 web server
_ADMIN_SERVER_IMPORT_ERROR = None
try:
    from .web.server import AdminServer
except ImportError as e:
    AdminServer = None
    _ADMIN_SERVER_IMPORT_ERROR = e

DATA_DIR = get_data_dir()
STATIC_CACHE_DIR = get_static_cache_dir()


@register("astrbot_plugin_chat_archive", "YukiNo420", "高性能聊天消息存档插件", "v1.5.2")
class ChatArchivePlugin(Star):
    # Batch writer configuration
    _BATCH_SIZE = 50
    _FLUSH_INTERVAL = 2.0  # seconds
    _MEDIA_PROCESS_TIMEOUT = 30.0
    _FORWARD_EXPAND_TIMEOUT = 5.0

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.conf = config

        self._shutting_down = True
        self._lifecycle_lock = asyncio.Lock()
        self._initialized = False

        # Track active background tasks to avoid garbage collection and 'Task was destroyed but it is pending' errors
        self._background_tasks = set()

        # Write buffer: queue + background writer
        self._writer = ArchiveBatchWriter(
            batch_size=self._BATCH_SIZE,
            flush_interval=self._FLUSH_INTERVAL,
            data_dir=DATA_DIR,
        )
        self._media_cache = ArchiveMediaCache(
            config=self.conf, cache_dir=STATIC_CACHE_DIR
        )
        self._media_queue: asyncio.Queue[tuple[dict[str, Any], bool]] = asyncio.Queue(
            maxsize=500
        )
        self._media_accept_lock = asyncio.Lock()
        self._media_generation = 0
        self._media_worker_task: asyncio.Task | None = None
        self._pending_recalls: dict[tuple[str, str], float] = {}
        self._pending_recall_lock = asyncio.Lock()
        self._pending_recall_event = asyncio.Event()
        self._recall_reconcile_task: asyncio.Task | None = None
        self._telegram_channel_capture = TelegramChannelCapture(self)
        self._telegram_channel_task = None

        self._register_llm_tools()

        # Cache blacklist as set for O(1) lookup
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        self._ignored_users: set[str] = {
            str(u) for u in basic_conf.get("ignored_users", [])
        }

        self.web_server = None
        web_conf = self.conf.get("web_server", {}) if self.conf else {}
        if AdminServer:
            if web_conf.get("enable", True):
                host = os.environ.get("ARCHIVE_HOST", "").strip() or web_conf.get(
                    "host", "127.0.0.1"
                )
                try:
                    port = int(
                        os.environ.get("ARCHIVE_PORT", "").strip()
                        or web_conf.get("port", 8090)
                    )
                except (TypeError, ValueError):
                    port = 8090
                api_key = (
                    os.environ.get("ARCHIVE_API_KEY", "").strip()
                    or str(web_conf.get("api_key", "") or "").strip()
                )

                if not api_key:
                    import secrets

                    api_key = secrets.token_urlsafe(16)
                    logger.warning(
                        "\n"
                        + "=" * 60
                        + f"\n[Chat Archive 安全警告] 您未在配置中指定访问验证 Key (api_key)！"
                        f"\n为了保障您的聊天记录隐私，系统已自动生成一个强随机密码："
                        f"\n👉👉 {api_key} 👈👈"
                        f"\n请使用上述密码登录 Web 仪表盘。您随时可以在插件的配置选项中设置自定义的 api_key。"
                        "\n" + "=" * 60 + "\n"
                    )

                self.web_server = AdminServer(
                    plugin_instance=self,
                    host=host,
                    port=port,
                    api_key=api_key,
                    cache_dir=STATIC_CACHE_DIR,
                )
            else:
                logger.info(
                    "Chat Archive: 内置 Web 面板已禁用（已通过外部或 systemd 解耦运行）。"
                )
        elif web_conf.get("enable", True):
            logger.warning(
                "Chat Archive: WebUI 依赖未安装，内置 Web 面板未启动。"
                f"请在插件目录执行 python3 -m pip install -r requirements.txt。原因: {_ADMIN_SERVER_IMPORT_ERROR}"
            )

        # Start clean-up background task
        self._clean_task = None

    async def initialize(self):
        """Initialize storage and start plugin services without blocking AstrBot."""
        async with self._lifecycle_lock:
            if self._initialized:
                return

            try:
                migration = await asyncio.to_thread(
                    migrate_legacy_storage, DATA_DIR, database_path=DB_PATH
                )
                if migration["cache_files"] or migration["state_files"]:
                    logger.info(
                        "Chat Archive: 旧版插件目录数据迁移完成，历史媒体 URL 保持不变。"
                    )
                await asyncio.to_thread(init_db)
                logger.info("Chat Archive: 数据库与表结构初始化成功。")

                await self._writer.start()
                await self._telegram_channel_capture.ensure_registered()
                loop = asyncio.get_running_loop()
                self._media_worker_task = loop.create_task(self._media_cache_worker())
                self._clean_task = loop.create_task(self._periodic_clean_loop())
                self._recall_reconcile_task = loop.create_task(
                    self._pending_recall_reconcile_loop()
                )

                # Start the externally reachable WebUI last. Marking the schema
                # ready before its lifespan starts avoids a duplicate init_db.
                if self.web_server:
                    self._set_web_schema_ready(True)
                    await self.web_server.start()

                # Event handlers only become usable after all fallible
                # initialization steps above have succeeded.
                async with self._media_accept_lock:
                    self._media_generation += 1
                    self._shutting_down = False
                    self._initialized = True
                self._start_telegram_channel_capture_task(loop)
            except Exception as e:
                await self._rollback_failed_initialize()
                logger.error(f"Chat Archive: 插件初始化失败: {e}")
                raise

    def _set_web_schema_ready(self, ready: bool) -> None:
        if not self.web_server:
            return
        self.web_server.schema_ready = bool(ready)
        app = getattr(getattr(self.web_server, "config", None), "app", None)
        state = getattr(app, "state", None)
        if state is not None:
            state.schema_ready = bool(ready)

    async def _rollback_failed_initialize(self) -> None:
        """Best-effort rollback for every service an incomplete init may start."""
        self._shutting_down = True
        self._initialized = False
        async with self._media_accept_lock:
            self._media_generation += 1
        self._set_web_schema_ready(False)

        for attribute in (
            "_telegram_channel_task",
            "_clean_task",
            "_recall_reconcile_task",
        ):
            task = getattr(self, attribute, None)
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            setattr(self, attribute, None)

        try:
            await self._telegram_channel_capture.stop()
        except Exception as cleanup_error:
            logger.error(
                f"Chat Archive: 初始化回滚 Telegram 捕获器失败: {cleanup_error}"
            )
        try:
            await self._stop_media_cache_worker()
        except Exception as cleanup_error:
            logger.error(f"Chat Archive: 初始化回滚媒体 worker 失败: {cleanup_error}")
        try:
            await self._writer.stop()
        except Exception as cleanup_error:
            logger.error(f"Chat Archive: 初始化回滚写入器失败: {cleanup_error}")
        try:
            await self._media_cache.close()
        except Exception as cleanup_error:
            logger.error(f"Chat Archive: 初始化回滚媒体客户端失败: {cleanup_error}")
        if self.web_server:
            try:
                await self.web_server.stop()
            except Exception as cleanup_error:
                logger.error(f"Chat Archive: 初始化回滚 WebUI 失败: {cleanup_error}")

    def _start_telegram_channel_capture_task(self, loop=None):
        if self._shutting_down:
            return
        if self._telegram_channel_task and not self._telegram_channel_task.done():
            return
        try:
            loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._telegram_channel_capture.run())
        self._telegram_channel_task = task
        task.add_done_callback(self._telegram_channel_task_done)

    def _telegram_channel_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled() or self._shutting_down:
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error:
            logger.error(
                f"Chat Archive: Telegram 捕获监控异常退出，将自动重启: {error}"
            )
        else:
            logger.warning("Chat Archive: Telegram 捕获监控意外退出，将自动重启。")
        self._telegram_channel_task = None
        try:
            asyncio.get_running_loop().call_later(
                1.0,
                self._start_telegram_channel_capture_task,
            )
        except RuntimeError:
            pass

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
    def _coerce_onebot_id(value: str):
        value = str(value or "").strip()
        if value.isdigit():
            try:
                return int(value)
            except Exception:
                return value
        return value

    def _group_admin_only_enabled(self) -> bool:
        conf = getattr(self, "conf", None)
        basic_conf = conf.get("basic", {}) if conf else {}
        return bool(basic_conf.get("group_admin_only", True))

    async def _is_group_owner_or_admin_event(self, event) -> bool:
        """Check current sender's OneBot group role for group-scoped archive tools."""
        group_id = ArchiveEventExtractor.event_group_id(event)
        user_id = ArchiveEventExtractor.event_sender_id(event)
        if not group_id or not user_id:
            return False

        call_action = self._resolve_onebot_call_action(event)
        if not callable(call_action):
            logger.debug(
                "Chat Archive: 当前事件不支持 OneBot call_action，无法校验群管理权限"
            )
            return False

        try:
            result = await call_action(
                "get_group_member_info",
                group_id=self._coerce_onebot_id(group_id),
                user_id=self._coerce_onebot_id(user_id),
                no_cache=True,
            )
        except TypeError:
            try:
                result = await call_action(
                    "get_group_member_info",
                    group_id=self._coerce_onebot_id(group_id),
                    user_id=self._coerce_onebot_id(user_id),
                )
            except Exception as e:
                logger.debug(
                    f"Chat Archive: get_group_member_info 群管理权限校验失败: {e}"
                )
                return False
        except Exception as e:
            logger.debug(f"Chat Archive: get_group_member_info 群管理权限校验失败: {e}")
            return False

        data = result.get("data") if isinstance(result, dict) else None
        role = ""
        if isinstance(data, dict):
            role = str(data.get("role") or "").lower()
        if not role and isinstance(result, dict):
            role = str(result.get("role") or "").lower()
        return role in {"owner", "admin"}

    def _is_admin_tool_context(self, context) -> bool:
        """Trust only AstrBot's current-profile role decision for this event."""
        event = self._extract_tool_event(context)
        if not event:
            return False

        try:
            is_admin = getattr(event, "is_admin", None)
            return bool(callable(is_admin) and is_admin())
        except Exception:
            return False

    def _archive_tools_ready(self) -> bool:
        return bool(self._initialized and not self._shutting_down)

    @staticmethod
    def _truthy_tool_arg(value) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
            "允许",
            "是",
        }

    async def _prepare_archive_tool_query(
        self, context, kwargs: dict, require_session: bool = True
    ) -> tuple[bool, dict]:
        """Validate and scope LLM archive-tool queries to the current chat session.

        Non-admin tool calls are always forced to the current event session. Admin
        cross-session reads require an explicit allow_cross_session=true flag so a
        stale model-supplied session_id from prior turns cannot silently leak data
        from another group into the current conversation.
        """
        if not self._archive_tools_ready():
            return False, {
                "error": "聊天存档仍在初始化或正在关闭，请稍后重试。",
                "scope": {"ready": False},
            }
        scoped_kwargs = dict(kwargs)
        event = self._extract_tool_event(context)
        current_session_id = ArchiveEventExtractor.event_session_id(event)
        current_group_id = ArchiveEventExtractor.event_group_id(event)
        sender_id = ArchiveEventExtractor.event_sender_id(event)
        is_admin = self._is_admin_tool_context(context)
        requested_session_id = str(scoped_kwargs.get("session_id") or "").strip()
        allow_cross_session = self._truthy_tool_arg(
            scoped_kwargs.pop("allow_cross_session", False)
        )

        if self._group_admin_only_enabled() and current_group_id and not is_admin:
            if not await self._is_group_owner_or_admin_event(event):
                return False, {
                    "error": "权限不足：当前已启用 group_admin_only，只有本群群主/管理员可以调用本群存档工具。",
                    "scope": {
                        "current_session_id": current_session_id,
                        "requested_session_id": requested_session_id,
                        "cross_session": bool(
                            requested_session_id
                            and requested_session_id != current_session_id
                        ),
                        "admin": False,
                        "group_admin_only": True,
                        "group_id": current_group_id,
                        "sender_id": sender_id,
                        "group_role": "member",
                    },
                }

        def attach_scope(effective_session_id: str, *, cross_session: bool) -> dict:
            scoped_kwargs["session_id"] = effective_session_id
            scoped_kwargs["__archive_scope"] = {
                "session_id": effective_session_id,
                "current_session_id": current_session_id,
                "requested_session_id": requested_session_id,
                "cross_session": bool(cross_session),
                "admin": bool(is_admin),
                "group_admin_only": self._group_admin_only_enabled(),
                "group_id": current_group_id,
                "sender_id": sender_id,
            }
            return scoped_kwargs

        if requested_session_id:
            if current_session_id and requested_session_id == current_session_id:
                return True, attach_scope(requested_session_id, cross_session=False)
            if is_admin and allow_cross_session:
                return True, attach_scope(requested_session_id, cross_session=True)
            if is_admin:
                return False, {
                    "error": (
                        "检测到跨会话归档查询。为避免把其他群聊数据误当成当前上下文，"
                        "请显式设置 allow_cross_session=true 后重试。"
                    ),
                    "scope": {
                        "current_session_id": current_session_id,
                        "requested_session_id": requested_session_id,
                        "cross_session": True,
                        "admin": True,
                    },
                }
            return False, {
                "error": "权限不足：只能查询当前会话的归档，跨会话查询需要管理员权限。",
                "scope": {
                    "current_session_id": current_session_id,
                    "requested_session_id": requested_session_id,
                    "cross_session": True,
                    "admin": False,
                },
            }

        if current_session_id:
            return True, attach_scope(current_session_id, cross_session=False)

        if is_admin and not require_session and allow_cross_session:
            scoped_kwargs["__archive_scope"] = {
                "session_id": "",
                "current_session_id": current_session_id,
                "requested_session_id": requested_session_id,
                "cross_session": False,
                "admin": True,
            }
            return True, scoped_kwargs

        return False, {
            "error": "无法确认当前会话，已拒绝归档查询以保护聊天隐私。",
            "scope": {
                "current_session_id": current_session_id,
                "requested_session_id": requested_session_id,
                "cross_session": False,
                "admin": bool(is_admin),
            },
        }

    def get_bot_ids(self) -> list[str]:
        bot_ids = ["bot", "99999", "astrbot"]
        try:
            context = getattr(self, "context", None)
            platform_manager = getattr(context, "platform_manager", None)
            if platform_manager:
                get_insts = getattr(platform_manager, "get_insts", None)
                insts = (
                    get_insts()
                    if callable(get_insts)
                    else getattr(platform_manager, "platform_insts", []) or []
                )
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
        """Stop plugin services and release the WebUI port before reload."""
        self._shutting_down = True
        async with self._lifecycle_lock:
            async with self._media_accept_lock:
                self._media_generation += 1
            self._set_web_schema_ready(False)
            web_stop_error = None
            if self.web_server:
                try:
                    await self.web_server.stop()
                except Exception as e:
                    web_stop_error = e
                    logger.error(f"Chat Archive: WebUI 停止失败: {e}")

            if self._telegram_channel_task and not self._telegram_channel_task.done():
                self._telegram_channel_task.cancel()
                try:
                    await self._telegram_channel_task
                except asyncio.CancelledError:
                    pass
            self._telegram_channel_task = None
            await self._telegram_channel_capture.stop()

            # Cancel the clean-up task
            if self._clean_task and not self._clean_task.done():
                self._clean_task.cancel()
                try:
                    await self._clean_task
                except asyncio.CancelledError:
                    pass
            self._clean_task = None

            await self._stop_media_cache_worker()

            if self._recall_reconcile_task and not self._recall_reconcile_task.done():
                self._recall_reconcile_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._recall_reconcile_task
            self._recall_reconcile_task = None

            # Stop and flush the writer before the final recall reconciliation.
            await self._writer.stop()
            await self._reconcile_pending_recalls_once()

            # Wait for delivery-confirmation tasks to observe shutdown.
            if self._background_tasks:
                pending_tasks = [t for t in self._background_tasks if not t.done()]
                if pending_tasks:
                    logger.info(
                        f"Chat Archive: 等待 {len(pending_tasks)} 个后台任务执行完毕..."
                    )
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                self._background_tasks.clear()

            await self._media_cache.close()
            self._initialized = False

            if web_stop_error:
                raise web_stop_error

    async def _enqueue_record_dict(
        self,
        record: dict[str, Any],
        *,
        cache_media: bool,
    ) -> bool:
        """Queue every record through one worker to preserve archive order."""
        generation = self._media_generation
        async with self._media_accept_lock:
            if (
                self._shutting_down
                or not self._initialized
                or generation != self._media_generation
            ):
                return False
            try:
                self._media_queue.put_nowait((dict(record), bool(cache_media)))
            except asyncio.QueueFull:
                logger.error(
                    "Chat Archive: 有序媒体队列已满，丢弃一条归档记录以保护主进程内存。"
                )
                return False
            return True

    async def _media_cache_worker(self) -> None:
        while True:
            current, cache_media = await self._media_queue.get()
            write_task: asyncio.Task | None = None
            try:
                if cache_media:
                    try:
                        if any(
                            key in current
                            for key in (
                                "_telegram_channel_message",
                                "_telegram_event",
                            )
                        ):
                            await asyncio.wait_for(
                                self._telegram_channel_capture.enrich_record_media(
                                    current
                                ),
                                timeout=self._MEDIA_PROCESS_TIMEOUT,
                            )
                        else:
                            current["message"] = await asyncio.wait_for(
                                self._process_and_cache_media_in_string(
                                    str(current.get("message") or "")
                                ),
                                timeout=self._MEDIA_PROCESS_TIMEOUT,
                            )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Chat Archive: 后台媒体缓存超时，"
                            "本条消息将使用原始媒体地址或占位符归档。"
                        )
                    except Exception as e:
                        logger.error(f"Chat Archive: 后台缓存媒体文件失败: {e}")
                await self._writer.start()
                write_task = asyncio.create_task(
                    self._writer.enqueue(
                        ArchiveEventExtractor.archive_record_tuple(current)
                    )
                )
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                # Await the same enqueue operation if cancellation raced with a
                # successful queue.put; starting a second enqueue can duplicate.
                if write_task is None:
                    write_task = asyncio.create_task(
                        self._writer.enqueue(
                            ArchiveEventExtractor.archive_record_tuple(current)
                        )
                    )
                await asyncio.shield(write_task)
                raise
            except Exception as e:
                logger.error(
                    f"Chat Archive: 单条有序归档处理失败，继续处理后续消息: {e}",
                    exc_info=True,
                )
                safe_record = {
                    key: value
                    for key, value in current.items()
                    if not str(key).startswith("_")
                }
                await asyncio.to_thread(
                    self._writer.write_failed_batch,
                    [safe_record],
                    str(e),
                )
            finally:
                self._media_queue.task_done()

    async def _stop_media_cache_worker(self) -> None:
        task = self._media_worker_task
        if task is None:
            return
        try:
            await asyncio.wait_for(self._media_queue.join(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Chat Archive: 媒体缓存队列关闭超时，剩余消息将使用原始地址归档。"
            )
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._media_worker_task = None
        while not self._media_queue.empty():
            try:
                record, _cache_media = self._media_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._writer.enqueue(
                ArchiveEventExtractor.archive_record_tuple(record)
            )
            self._media_queue.task_done()

    async def _remember_pending_recall(
        self,
        session_id: str,
        message_id: str,
    ) -> None:
        key = (str(session_id or ""), str(message_id or ""))
        if not all(key):
            return
        async with self._pending_recall_lock:
            # Keep the compensation set bounded even under malformed notice spam.
            while len(self._pending_recalls) >= 10_000:
                self._pending_recalls.pop(next(iter(self._pending_recalls)))
            self._pending_recalls[key] = time.monotonic() + 86_400
            self._pending_recall_event.set()

    @staticmethod
    def _mark_recalled_sync(keys: list[tuple[str, str]]) -> set[tuple[str, str]]:
        if not keys:
            return set()
        connection = get_db_connection()
        matched: set[tuple[str, str]] = set()
        try:
            for session_id, message_id in keys:
                cursor = connection.execute(
                    "UPDATE chat_history SET is_recalled = 1 "
                    "WHERE msg_id = ? AND session_id = ? "
                    "AND user_id != '0' AND COALESCE(is_recalled, 0) = 0",
                    (message_id, session_id),
                )
                if cursor.rowcount:
                    matched.add((session_id, message_id))
                    continue
                existing = connection.execute(
                    "SELECT 1 FROM chat_history "
                    "WHERE msg_id = ? AND session_id = ? "
                    "AND user_id != '0' AND is_recalled = 1 LIMIT 1",
                    (message_id, session_id),
                ).fetchone()
                if existing:
                    matched.add((session_id, message_id))
            connection.commit()
            return matched
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def _reconcile_pending_recalls_once(self) -> int:
        async with self._pending_recall_lock:
            now = time.monotonic()
            expired = [
                key
                for key, deadline in self._pending_recalls.items()
                if deadline <= now
            ]
            for key in expired:
                self._pending_recalls.pop(key, None)
            keys = list(self._pending_recalls)
        if not keys:
            return 0
        try:
            matched = await asyncio.to_thread(self._mark_recalled_sync, keys)
        except Exception as e:
            logger.error(f"Chat Archive: 撤回补偿写入失败: {e}")
            return 0
        if matched:
            async with self._pending_recall_lock:
                for key in matched:
                    self._pending_recalls.pop(key, None)
        return len(matched)

    async def _pending_recall_reconcile_loop(self) -> None:
        while True:
            await self._pending_recall_event.wait()
            self._pending_recall_event.clear()
            retry_delay = 0.5
            while True:
                await self._reconcile_pending_recalls_once()
                async with self._pending_recall_lock:
                    has_pending = bool(self._pending_recalls)
                if not has_pending:
                    break
                try:
                    await asyncio.wait_for(
                        self._pending_recall_event.wait(),
                        timeout=retry_delay,
                    )
                    self._pending_recall_event.clear()
                    retry_delay = 0.5
                except asyncio.TimeoutError:
                    retry_delay = min(retry_delay * 2, 60.0)

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
        ids.extend(
            cls._extract_forward_ids_from_message(
                ArchiveEventExtractor.get_field(platform_raw, "message", "")
            )
        )
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
            logger.warning(
                f"Chat Archive: 无法获取 OneBot call_action，跳过合并转发展开 {forward_id}"
            )
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
                logger.warning(
                    "Chat Archive: nested forward expansion timed out; preserving available content"
                )
            text = serialize_onebot_message(messages).strip()
            if text:
                if text.startswith("[合并转发]\n"):
                    return text.replace(
                        "[合并转发]", f"[合并转发,id={escape_cq_param(forward_id)}]", 1
                    )
                return f"[合并转发,id={escape_cq_param(forward_id)}]\n{text}\n[合并转发结束]"
            logger.warning(
                f"Chat Archive: get_forward_msg 返回空内容 {forward_id} {params}"
            )
        return ""

    def _resolve_onebot_call_action(self, event):
        bot = getattr(event, "bot", None)
        for candidate in (bot, getattr(bot, "api", None)):
            call_action = getattr(candidate, "call_action", None)
            if callable(call_action):
                return call_action

        platform_id = str(
            ArchiveEventExtractor.safe_event_call(
                event,
                "get_platform_id",
                "",
            )
            or ""
        ).lower()
        platform_name = str(
            ArchiveEventExtractor.safe_event_call(
                event,
                "get_platform_name",
                "",
            )
            or ""
        ).lower()
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
            for candidate in (
                client,
                getattr(client, "api", None),
                get_client() if callable(get_client) else None,
            ):
                inst_call_action = getattr(candidate, "call_action", None)
                if callable(inst_call_action):
                    return inst_call_action
        return None

    async def _expand_forward_message_if_needed(
        self, event, record: dict[str, Any], platform_raw
    ) -> None:
        message = str(record.get("message") or "")
        if "[合并转发" not in message:
            return

        forward_ids = self._extract_forward_ids(event, platform_raw)
        deadline = asyncio.get_running_loop().time() + self._FORWARD_EXPAND_TIMEOUT
        for forward_id in forward_ids:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.warning("Chat Archive: 合并转发展开超时，保留未展开内容。")
                break
            try:
                expanded = await asyncio.wait_for(
                    self._fetch_forward_archive_text(event, forward_id),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                logger.warning("Chat Archive: 合并转发展开超时，保留未展开内容。")
                break
            if not expanded:
                continue
            markers = (
                f"[合并转发,id={forward_id}]",
                f"[合并转发,id={escape_cq_param(forward_id)}]",
                "[合并转发]",
            )
            message, replaced = replace_forward_reference(message, markers, expanded)
            if not replaced:
                message = f"{message}\n{expanded}" if message else expanded
        record["message"] = message

    async def _process_and_cache_media_in_string(self, text: str) -> str:
        return await self._media_cache.process_and_cache_media_in_string(text)

    @staticmethod
    def _is_non_message_platform_event(platform_raw) -> bool:
        post_type = str(
            ArchiveEventExtractor.get_field(
                platform_raw,
                "post_type",
                "",
            )
            or ""
        ).lower()
        if post_type in {"notice", "request", "meta_event"}:
            return True
        return bool(
            ArchiveEventExtractor.get_field(
                platform_raw,
                "notice_type",
                "",
            )
            or ArchiveEventExtractor.get_field(
                platform_raw,
                "request_type",
                "",
            )
        )

    @filter.event_message_type(EventMessageType.ALL, priority=20)
    async def record_message(self, event: AstrMessageEvent):
        """
        拦截所有的消息事件，并存档至数据库。
        """
        if self._shutting_down or not self._initialized:
            return

        # 读取配置
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        if not basic_conf.get("enable_archive", True):
            return

        cache_media = bool(basic_conf.get("cache_media", False))
        platform_raw = getattr(
            getattr(event, "message_obj", None),
            "raw_message",
            None,
        )
        record = ArchiveEventExtractor.archive_record_from_event(event)
        notice_type = str(
            ArchiveEventExtractor.get_field(
                platform_raw,
                "notice_type",
                "",
            )
            or ""
        ).lower()
        if notice_type in {"group_recall", "friend_recall"}:
            recalled_msg_id = str(
                ArchiveEventExtractor.get_field(
                    platform_raw,
                    "message_id",
                    "",
                )
                or ""
            )
            if not recalled_msg_id:
                return
            await self._remember_pending_recall(
                record["session_id"],
                recalled_msg_id,
            )
            record["message"] = f"🛡️ [撤回了一条消息 (ID: {recalled_msg_id})]"
            record["user_id"] = "0"
            record["sender_name"] = "系统通知"
            record["msg_id"] = f"recall_{recalled_msg_id}"
            await self._enqueue_record_dict(record, cache_media=False)
            return

        # Notice/request wrappers are not chat messages. Keeping only recall
        # notices prevents UUID-shaped empty rows from polluting statistics.
        if self._is_non_message_platform_event(platform_raw):
            return

        if not record["user_id"]:
            return
        if record["user_id"] in self._ignored_users:
            return

        if cache_media and str(record.get("platform_name", "")).lower() == "telegram":
            record["_telegram_event"] = event
            record["_telegram_avatar_kind"] = "event"
        await self._expand_forward_message_if_needed(event, record, platform_raw)
        await self._enqueue_record_dict(record, cache_media=cache_media)

    @filter.on_decorating_result()
    async def snapshot_bot_reply(self, event: AstrMessageEvent):
        """Preserve components such as Record before RespondStage extracts them."""
        if self._shutting_down or not self._initialized:
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        raw_message = serialize_message_chain(result.chain)
        if raw_message:
            event.set_extra("_chat_archive_reply_snapshot", raw_message)

    @filter.on_llm_response()
    async def capture_streaming_bot_reply(self, event: AstrMessageEvent, response):
        """Archive streaming output only after AstrBot confirms stream completion."""
        if self._shutting_down or not self._initialized or response is None:
            return
        result = event.get_result()
        content_type = getattr(
            getattr(result, "result_content_type", None),
            "name",
            "",
        )
        if content_type != "STREAMING_RESULT":
            return

        response_chain = getattr(response, "result_chain", None)
        chain = getattr(response_chain, "chain", None)
        raw_message = (
            serialize_message_chain(chain)
            if chain
            else str(getattr(response, "completion_text", "") or "")
        )
        if not raw_message:
            return
        task = asyncio.create_task(
            self._archive_streaming_reply_after_delivery(event, raw_message)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _archive_streaming_reply_after_delivery(
        self,
        event: AstrMessageEvent,
        raw_message: str,
    ) -> None:
        for _ in range(1800):
            if self._shutting_down:
                return
            if event.get_extra("_streaming_finished", False):
                captured = event.get_extra(
                    "_chat_archive_bot_messages",
                    set(),
                )
                if raw_message not in captured:
                    await self._archive_bot_reply(event, raw_message)
                return
            await asyncio.sleep(0.1)

    @filter.after_message_sent()
    async def handle_bot_reply(self, event: AstrMessageEvent):
        """
        在机器人成功发送消息后，捕获并存档机器人自己的回复。
        """
        if self._shutting_down or not self._initialized:
            return

        raw_message = str(event.get_extra("_chat_archive_reply_snapshot", "") or "")
        result = event.get_result()
        if not raw_message and result and result.chain:
            raw_message = serialize_message_chain(result.chain)
        if not raw_message:
            return

        try:
            await self._archive_bot_reply(event, raw_message)
        except Exception as e:
            logger.error(f"Chat Archive: 记录机器人回复消息异常: {e}", exc_info=True)

    async def _archive_bot_reply(
        self,
        event: AstrMessageEvent,
        raw_message: str,
    ) -> None:
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        if not basic_conf.get("enable_archive", True):
            return
        user_id = str(
            ArchiveEventExtractor.safe_event_call(
                event,
                "get_self_id",
                "",
            )
            or "bot"
        )
        if user_id in self._ignored_users:
            return

        cache_media = bool(basic_conf.get("cache_media", False))
        record = ArchiveEventExtractor.archive_record_from_event(
            event,
            raw_message=raw_message,
        )
        record["user_id"] = user_id

        nickname = "Bot"
        try:
            if self.context and hasattr(self.context, "get_self_nickname"):
                nickname = self.context.get_self_nickname() or nickname
            elif self.context and hasattr(self.context, "get_bot_name"):
                nickname = self.context.get_bot_name() or nickname
        except Exception:
            pass
        record["sender_name"] = str(nickname)

        if cache_media and str(record.get("platform_name", "")).lower() == "telegram":
            record["_telegram_event"] = event
            record["_telegram_avatar_kind"] = "bot"

        timestamp = int(time.time())
        record["timestamp"] = timestamp
        record["msg_id"] = f"bot_{timestamp}_{time.time_ns()}"
        captured = event.get_extra("_chat_archive_bot_messages", set())
        if not isinstance(captured, set):
            captured = set()
        captured.add(raw_message)
        event.set_extra("_chat_archive_bot_messages", captured)
        await self._enqueue_record_dict(record, cache_media=cache_media)

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

    async def _periodic_clean_loop(self):
        """定期清理过期缓存文件的循环。每一天运行一次。"""
        # Wait a small duration initially to let startup finish
        await asyncio.sleep(5)
        while True:
            try:
                await self._maintain_media_cache_once()
                basic_conf = self.conf.get("basic", {}) if self.conf else {}
                enable_clean = basic_conf.get("enable_clean", False)
                clean_days = basic_conf.get("clean_days", 30)

                try:
                    from .archive_management import ArchiveManager

                    manager = ArchiveManager(get_db_connection, STATIC_CACHE_DIR)
                    retained = await asyncio.to_thread(
                        manager.apply_retention,
                        basic_conf.get("message_retention_days", 0),
                        basic_conf.get("message_retention_sessions", []),
                        global_scope=basic_conf.get("message_retention_global", False),
                        excluded_session_ids=basic_conf.get(
                            "message_retention_excluded_sessions", []
                        ),
                        should_stop=lambda: self._shutting_down,
                    )
                    if retained and self.web_server:
                        from .web.server import invalidate_statistics

                        invalidate_statistics()
                except Exception as exc:
                    logger.error(f"Chat Archive: recoverable retention failed: {exc}")

            except Exception as e:
                logger.error(f"Chat Archive: 定期清理执行异常: {e}")

            # Sleep for 24 hours (86400 seconds)
            await asyncio.sleep(86400)

    async def _maintain_media_cache_once(self) -> None:
        """Apply optional age cleanup, stale tmp cleanup, and the total quota."""
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        enable_clean = basic_conf.get("enable_clean", False)
        clean_days = basic_conf.get("clean_days", 30)
        if (
            enable_clean
            and clean_days > 0
            and basic_conf.get("allow_cache_eviction", False)
        ):
            await self._clean_expired_cache(clean_days)
        if not await self._media_cache.ensure_cache_capacity(0, force_rescan=True):
            logger.warning(
                "Chat Archive: 媒体缓存仍超过总容量上限，请检查不可管理文件或调高容量。"
            )

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
                logger.info(
                    f"Chat Archive: 清理了 {cleaned_count} 个过期媒体文件，释放空间 {cleaned_bytes / (1024 * 1024):.2f} MB。"
                )
        except Exception as e:
            logger.error(f"Chat Archive: 清理过期缓存文件失败: {e}")
