from __future__ import annotations

import asyncio
import json
from typing import Any

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass


def _archive_json_result(data: Any) -> str:
    """Serialize archive query results for LLM tool calls."""
    return json.dumps(data, ensure_ascii=False, default=str)


def _archive_tool_error(message: str, scope: dict | None = None) -> str:
    payload = {"error": message}
    if scope:
        payload["scope"] = scope
    return _archive_json_result(payload)


def _pop_archive_scope(scoped_kwargs: dict) -> dict:
    scope = scoped_kwargs.pop("__archive_scope", {}) or {}
    if not isinstance(scope, dict):
        return {}
    return scope


def _archive_scoped_result(scope: dict, data: Any) -> str:
    return _archive_json_result({"scope": scope, "data": data})


def _allow_cross_session_schema() -> dict:
    return {
        "type": "boolean",
        "description": "可选，仅管理员跨会话查询时需要显式设置 true；普通当前会话查询不要设置。",
        "default": False,
    }


@dataclass
class ArchiveGetHistoryTool(FunctionTool[AstrAgentContext]):
    """LLM tool: query archived chat history."""

    name: str = "archive_get_history"
    description: str = (
        "查询聊天存档历史记录。可按用户、会话、关键词、时间范围分页查询，"
        "返回消息列表，适合查找某段对话、某人发言或包含特定关键词的消息。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "可选，用户 ID；传入后仅查询该用户的消息。",
                },
                "session_id": {
                    "type": "string",
                    "description": "可选，会话 ID/群聊 ID；默认当前会话。非管理员只能查询当前会话。",
                },
                "allow_cross_session": _allow_cross_session_schema(),
                "keyword": {
                    "type": "string",
                    "description": "可选，消息关键词；用于模糊搜索消息正文。",
                },
                "since_ts": {
                    "type": "integer",
                    "description": "可选，起始 Unix 时间戳（秒），只返回此时间之后的消息。",
                },
                "until_ts": {
                    "type": "integer",
                    "description": "可选，结束 Unix 时间戳（秒），只返回此时间之前的消息。",
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，最多返回条数，默认 50。",
                    "default": 50,
                },
                "offset": {
                    "type": "integer",
                    "description": "可选，分页偏移量，默认 0。",
                    "default": 0,
                },
                "asc": {
                    "type": "boolean",
                    "description": "可选，是否按时间升序返回；false 表示最新消息优先，默认 true。",
                    "default": True,
                },
                "exclude_recalled": {
                    "type": "boolean",
                    "description": "可选，是否排除已撤回消息，默认 true。",
                    "default": True,
                },
            },
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = await self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(
                scoped_kwargs["error"], scoped_kwargs.get("scope")
            )
        scope = _pop_archive_scope(scoped_kwargs)
        data = await asyncio.to_thread(self.plugin.get_history, **scoped_kwargs)
        return _archive_scoped_result(scope, data)


@dataclass
class ArchiveGetSessionsTool(FunctionTool[AstrAgentContext]):
    """LLM tool: list archived sessions."""

    name: str = "archive_get_sessions"
    description: str = (
        "获取所有存在聊天存档的会话列表。返回每个会话的 session_id、消息类型、"
        "消息数量和最后消息时间，可用于先定位要查询的群聊/私聊会话。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **_kwargs
    ) -> ToolExecResult:
        if not self.plugin._archive_tools_ready():
            return _archive_tool_error("聊天存档仍在初始化或正在关闭，请稍后重试。")
        if not self.plugin._is_admin_tool_context(context):
            return _archive_tool_error("权限不足：只有管理员可以列出所有归档会话。")
        return _archive_json_result(
            {
                "scope": {"admin": True, "cross_session": True},
                "data": await asyncio.to_thread(self.plugin.get_sessions),
            }
        )


@dataclass
class ArchiveGetMemberRankTool(FunctionTool[AstrAgentContext]):
    """LLM tool: rank active members."""

    name: str = "archive_get_member_rank"
    description: str = (
        "查询指定会话内成员活跃排行，按发言数量从高到低返回。"
        "适合回答谁最活跃、某段时间内群内发言排行等问题。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "可选，会话 ID/群聊 ID；默认当前会话。非管理员只能查询当前会话。",
                },
                "allow_cross_session": _allow_cross_session_schema(),
                "limit": {
                    "type": "integer",
                    "description": "可选，返回排行人数，默认 10。",
                    "default": 10,
                },
                "since_ts": {
                    "type": "integer",
                    "description": "可选，起始 Unix 时间戳（秒）。",
                },
                "until_ts": {
                    "type": "integer",
                    "description": "可选，结束 Unix 时间戳（秒）。",
                },
            },
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = await self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(
                scoped_kwargs["error"], scoped_kwargs.get("scope")
            )
        scope = _pop_archive_scope(scoped_kwargs)
        data = await asyncio.to_thread(self.plugin.get_member_rank, **scoped_kwargs)
        return _archive_scoped_result(scope, data)


@dataclass
class ArchiveGetUserSummaryTool(FunctionTool[AstrAgentContext]):
    """LLM tool: summarize one user."""

    name: str = "archive_get_user_summary"
    description: str = (
        "查询指定用户的存档统计概览，包括总消息数、首次出现时间、最后发言时间、"
        "最近昵称等；可选限定在某个会话内统计。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "必填，用户 ID。",
                },
                "session_id": {
                    "type": "string",
                    "description": "可选，会话 ID/群聊 ID；默认当前会话。非管理员只能查询当前会话。",
                },
                "allow_cross_session": _allow_cross_session_schema(),
            },
            "required": ["user_id"],
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = await self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(
                scoped_kwargs["error"], scoped_kwargs.get("scope")
            )
        scope = _pop_archive_scope(scoped_kwargs)
        data = await asyncio.to_thread(self.plugin.get_user_summary, **scoped_kwargs)
        return _archive_scoped_result(scope, data)


@dataclass
class ArchiveGetMessageCountTool(FunctionTool[AstrAgentContext]):
    """LLM tool: count messages."""

    name: str = "archive_get_message_count"
    description: str = (
        "轻量统计聊天存档消息数量。可按用户、会话、时间范围筛选，"
        "适合回答总共有多少条消息、某人/某群某段时间发了多少条等问题。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "可选，用户 ID；传入后只统计该用户消息。",
                },
                "session_id": {
                    "type": "string",
                    "description": "可选，会话 ID/群聊 ID；默认当前会话。非管理员只能查询当前会话。",
                },
                "allow_cross_session": _allow_cross_session_schema(),
                "since_ts": {
                    "type": "integer",
                    "description": "可选，起始 Unix 时间戳（秒）。",
                },
                "until_ts": {
                    "type": "integer",
                    "description": "可选，结束 Unix 时间戳（秒）。",
                },
                "exclude_recalled": {
                    "type": "boolean",
                    "description": "可选，是否排除已撤回消息，默认 true。",
                    "default": True,
                },
            },
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = await self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(
                scoped_kwargs["error"], scoped_kwargs.get("scope")
            )
        scope = _pop_archive_scope(scoped_kwargs)
        data = await asyncio.to_thread(self.plugin.get_message_count, **scoped_kwargs)
        return _archive_scoped_result(scope, data)


@dataclass
class ArchiveGetContextMessagesTool(FunctionTool[AstrAgentContext]):
    """LLM tool: get formatted context messages."""

    name: str = "archive_get_context_messages"
    description: str = (
        "获取适合 LLM 阅读的上下文消息列表。返回格式为 "
        "[时间字符串, 发送者昵称, 消息内容]，适合在需要回顾某个会话近期上下文时调用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "可选，会话 ID/群聊 ID；默认当前会话。非管理员只能查询当前会话。",
                },
                "allow_cross_session": _allow_cross_session_schema(),
                "user_id": {
                    "type": "string",
                    "description": "可选，用户 ID；传入后只返回该用户的上下文消息。",
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，返回消息条数，默认 50。",
                    "default": 50,
                },
                "exclude_recalled": {
                    "type": "boolean",
                    "description": "可选，是否排除已撤回消息，默认 true。",
                    "default": True,
                },
            },
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = await self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(
                scoped_kwargs["error"], scoped_kwargs.get("scope")
            )
        scope = _pop_archive_scope(scoped_kwargs)
        data = await asyncio.to_thread(
            self.plugin.get_context_messages, **scoped_kwargs
        )
        return _archive_scoped_result(scope, data)


def register_archive_tools(context: Any, plugin: Any) -> int:
    context.add_llm_tools(
        ArchiveGetHistoryTool(plugin=plugin),
        ArchiveGetSessionsTool(plugin=plugin),
        ArchiveGetMemberRankTool(plugin=plugin),
        ArchiveGetUserSummaryTool(plugin=plugin),
        ArchiveGetMessageCountTool(plugin=plugin),
        ArchiveGetContextMessagesTool(plugin=plugin),
    )
    return 6
