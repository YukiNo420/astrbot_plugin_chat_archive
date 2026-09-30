"""Bounded, best-effort expansion of OneBot forward and file references."""
from __future__ import annotations

import asyncio
from copy import deepcopy


def response_messages(response):
    payload = response.get("data", response) if isinstance(response, dict) else response
    if isinstance(payload, dict):
        return next((payload[key] for key in ("messages", "message", "nodes", "content") if payload.get(key)), None)
    return payload


async def expand_forward_payload(messages, call_action, *, max_depth=8, max_calls=32):
    """Preserve unavailable references; never download or expose local file paths."""
    remaining = max_calls
    cache = {}

    async def request(action, **params):
        nonlocal remaining
        key = (action, tuple(params.items()))
        if key in cache:
            return cache[key]
        if remaining <= 0:
            return None
        remaining -= 1
        try:
            result = await asyncio.wait_for(call_action(action, **params), timeout=5)
        except Exception:
            result = None
        cache[key] = result
        return result

    async def walk(value, depth, ancestors):
        if depth > max_depth:
            return value
        if isinstance(value, list):
            return [await walk(item, depth, ancestors) for item in value]
        if not isinstance(value, dict):
            return value
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        kind = str(value.get("type", "")).lower()
        if kind == "forward":
            forward_id = str(data.get("id") or data.get("res_id") or "")
            content = data.get("content")
            if not content and forward_id and forward_id not in ancestors and depth < max_depth:
                for params in ({"message_id": forward_id}, {"id": forward_id}):
                    content = response_messages(await request("get_forward_msg", **params))
                    if content:
                        break
            if content and forward_id not in ancestors:
                data["content"] = await walk(deepcopy(content), depth + 1, ancestors | ({forward_id} if forward_id else set()))
        elif kind == "file" and not data.get("url"):
            file_id = data.get("file_id") or data.get("id")
            if file_id:
                group_id = data.get("group_id")
                if group_id:
                    result = await request("get_group_file_url", group_id=str(group_id), file_id=str(file_id))
                else:
                    result = await request("get_private_file_url", file_id=str(file_id))
                result = result.get("data", result) if isinstance(result, dict) else None
                if isinstance(result, dict):
                    url = result.get("url")
                    if isinstance(url, str) and url.lower().startswith(("https://", "http://")):
                        data["url"] = url
                    if not data.get("name") and result.get("file_name"):
                        data["name"] = result["file_name"]
        for key in ("content", "message", "messages", "nodes"):
            if key in data and not (kind == "forward" and key == "content"):
                data[key] = await walk(data[key], depth, ancestors)
        return value

    return await walk(deepcopy(messages), 0, set())
