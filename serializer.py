from __future__ import annotations

import json
import re
import struct
from collections.abc import Iterable

from astrbot.api import logger


_INLINE_MEDIA_RE = re.compile(
    r"^(?:base64://|data:[^,\s]+;base64(?:,|&#44;))", re.IGNORECASE
)
_CQ_MEDIA_CODE_RE = re.compile(
    r"\[CQ:(image|video|record|file),([^\]]*)\]", re.IGNORECASE
)
_CQ_PARAM_SPLIT_RE = re.compile(r",(?=[A-Za-z_][\w.-]*=)")


def escape_cq_param(value) -> str:
    """Escape CQ parameter separators so stored media URLs remain parseable."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def unescape_cq_param(value) -> str:
    text = str(value or "")
    replacements = (
        ("&amp;", "&"),
        ("&#44;", ","),
        ("&#91;", "["),
        ("&#93;", "]"),
    )
    for _ in range(3):
        next_text = text
        for old, new in replacements:
            next_text = next_text.replace(old, new)
        if next_text == text:
            break
        text = next_text
    return text


def is_inline_media_source(value) -> bool:
    text = unescape_cq_param(value).strip()
    return bool(text and _INLINE_MEDIA_RE.match(text))


def clean_media_source(value) -> str:
    text = str(value or "").strip()
    return "" if is_inline_media_source(text) else text


def clean_file_name(value, default: str = "文件") -> str:
    text = str(value or "").strip()
    return default if not text or is_inline_media_source(text) else text


def _parse_cq_params(inner: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for part in _CQ_PARAM_SPLIT_RE.split(str(inner or "")):
        key, sep, raw_value = part.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        if key:
            params[key] = raw_value.strip()
    return params


def _inline_media_placeholder(cq_type: str, params: dict[str, str]) -> str:
    cq_type = cq_type.lower()
    if cq_type == "image":
        return "[CQ:image]"
    if cq_type == "video":
        return "[CQ:video]"
    if cq_type == "record":
        return "[语音]"
    if cq_type == "file":
        name = unescape_cq_param(params.get("name") or "").strip()
        return f"[文件: {name}]" if name else "[文件]"
    return ""


def sanitize_cq_media_codes(value) -> str:
    """Replace inline base64 CQ media payloads with short archive placeholders."""
    text = str(value or "")

    def replace(match: re.Match) -> str:
        cq_type = match.group(1).lower()
        params = _parse_cq_params(match.group(2))
        if any(is_inline_media_source(params.get(key)) for key in ("url", "file")):
            return _inline_media_placeholder(cq_type, params) or match.group(0)
        return match.group(0)

    return _CQ_MEDIA_CODE_RE.sub(replace, text)


def positive_int(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def valid_image_dimensions(width: int, height: int) -> bool:
    return 0 < width <= 100000 and 0 < height <= 100000


def read_image_dimensions(filepath: str) -> tuple[int, int] | None:
    """Read PNG/JPEG/GIF/WebP dimensions from headers without external deps."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(30)
            if len(header) < 10:
                return None

            if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 24:
                width, height = struct.unpack(">II", header[16:24])
                return (
                    (width, height) if valid_image_dimensions(width, height) else None
                )

            if header[:3] == b"GIF":
                width, height = struct.unpack("<HH", header[6:10])
                return (
                    (width, height) if valid_image_dimensions(width, height) else None
                )

            if header[:2] == b"\xff\xd8":
                f.seek(2)
                sof_markers = (
                    set(range(0xC0, 0xC4))
                    | set(range(0xC5, 0xC8))
                    | set(range(0xC9, 0xCC))
                    | set(range(0xCD, 0xD0))
                )
                while True:
                    marker_prefix = f.read(1)
                    if not marker_prefix:
                        break
                    if marker_prefix != b"\xff":
                        continue
                    marker_byte = f.read(1)
                    if not marker_byte:
                        break
                    marker = marker_byte[0]
                    while marker == 0xFF:
                        marker_byte = f.read(1)
                        if not marker_byte:
                            return None
                        marker = marker_byte[0]
                    if marker == 0xD9:
                        break
                    if 0xD0 <= marker <= 0xD7 or marker == 0x01:
                        continue
                    length_bytes = f.read(2)
                    if len(length_bytes) < 2:
                        break
                    length = struct.unpack(">H", length_bytes)[0]
                    if length < 2:
                        break
                    if marker in sof_markers:
                        data = f.read(5)
                        if len(data) < 5:
                            break
                        height, width = struct.unpack(">HH", data[1:5])
                        return (
                            (width, height)
                            if valid_image_dimensions(width, height)
                            else None
                        )
                    f.seek(length - 2, 1)
                return None

            if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
                chunk = header[12:16]
                if chunk == b"VP8 " and len(header) >= 30:
                    width = struct.unpack("<H", header[26:28])[0] & 0x3FFF
                    height = struct.unpack("<H", header[28:30])[0] & 0x3FFF
                    return (
                        (width, height)
                        if valid_image_dimensions(width, height)
                        else None
                    )
                if chunk == b"VP8L" and len(header) >= 25:
                    bits = struct.unpack("<I", header[21:25])[0]
                    width = (bits & 0x3FFF) + 1
                    height = ((bits >> 14) & 0x3FFF) + 1
                    return (
                        (width, height)
                        if valid_image_dimensions(width, height)
                        else None
                    )
                if chunk == b"VP8X":
                    f.seek(24)
                    canvas = f.read(6)
                    if len(canvas) == 6:
                        width = (canvas[0] | (canvas[1] << 8) | (canvas[2] << 16)) + 1
                        height = (canvas[3] | (canvas[4] << 8) | (canvas[5] << 16)) + 1
                        return (
                            (width, height)
                            if valid_image_dimensions(width, height)
                            else None
                        )
    except Exception:
        return None
    return None


def cq_param_exists(inner: str, key: str) -> bool:
    return re.search(rf"(?:^|,){re.escape(key)}=", inner) is not None


def _field(obj, key: str, default=None):
    if obj is None:
        return default
    if hasattr(obj, "get") and callable(obj.get):
        value = obj.get(key)
        if value is not None:
            return value
    return getattr(obj, key, default)


def _as_component_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return list(value)
    chain = getattr(value, "chain", None)
    if isinstance(chain, list | tuple):
        return list(chain)
    if isinstance(value, Iterable) and not isinstance(value, str | bytes | dict):
        try:
            return list(value)
        except TypeError:
            return []
    return []


def _serialize_node_content(content) -> str:
    if isinstance(content, str):
        return sanitize_cq_media_codes(content)
    if isinstance(content, dict):
        return serialize_onebot_message(content)
    if isinstance(content, list | tuple) and any(
        isinstance(item, dict) for item in content
    ):
        return serialize_onebot_message(content)
    return serialize_message_chain(_as_component_list(content))


def _node_data(node):
    data = _field(node, "data", None)
    return data if isinstance(data, dict) else {}


def _node_content(node):
    data = _node_data(node)
    return (
        _field(node, "content", None)
        or data.get("content")
        or _field(node, "message", None)
        or data.get("message")
    )


def _is_forward_node_dict(item) -> bool:
    if not isinstance(item, dict):
        return False
    item_type = str(item.get("type") or "").lower()
    if item_type and item_type != "node":
        return False
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    return (
        item_type == "node"
        or "content" in item
        or "content" in data
        or "message" in item
        or "message" in data
    )


def _node_sender_label(node) -> str:
    data = _node_data(node)
    sender = _field(node, "sender", None) or data.get("sender") or {}
    name = str(
        _field(node, "name", "")
        or _field(node, "nickname", "")
        or data.get("nickname")
        or _field(sender, "nickname", "")
        or _field(sender, "card", "")
        or ""
    ).strip()
    uin = str(
        _field(node, "uin", "")
        or _field(node, "user_id", "")
        or _field(node, "sender_id", "")
        or data.get("user_id")
        or _field(sender, "user_id", "")
        or ""
    ).strip()
    if name and uin and name != uin:
        return f"{name}({uin})"
    return name or uin or "未知发送者"


def _serialize_forward_nodes(nodes) -> str:
    node_list = _as_component_list(nodes)
    lines = ["[合并转发]"]
    for index, node in enumerate(node_list, 1):
        content = _serialize_node_content(_node_content(node)).strip()
        if not content:
            data = _node_data(node)
            node_id = _field(node, "id", "") or data.get("id") or ""
            content = f"[节点消息,id={node_id}]" if node_id else "[节点消息]"
        # Indent continuation lines so nested node numbers cannot become siblings.
        content = content.replace("\n", "\n    ")
        lines.append(f"{index}. {_node_sender_label(node)}: {content}")
    if len(lines) > 1:
        lines.append("[合并转发结束]")
        return "\n".join(lines)
    return "[合并转发]\n[合并转发结束]"


def _serialize_forward_component(comp) -> str:
    cls_name = comp.__class__.__name__
    if cls_name == "Nodes":
        return _serialize_forward_nodes(getattr(comp, "nodes", []))
    if cls_name == "Node":
        return _serialize_forward_nodes([comp])

    forward_id = getattr(comp, "id", "") or getattr(comp, "res_id", "")
    if forward_id:
        return f"[合并转发,id={escape_cq_param(forward_id)}]\n[合并转发结束]"
    return "[合并转发]\n[合并转发结束]"


def serialize_onebot_message(message) -> str:
    """Serialize raw OneBot/NapCat message segments into archive text."""
    if isinstance(message, str):
        return sanitize_cq_media_codes(message)
    if isinstance(message, list | tuple):
        if message and all(_is_forward_node_dict(item) for item in message):
            return _serialize_forward_nodes(
                [
                    item.get("data") if isinstance(item.get("data"), dict) else item
                    for item in message
                ]
            )
        return "".join(serialize_onebot_message(item) for item in message)
    if not isinstance(message, dict):
        return ""

    seg_type = str(message.get("type") or "").lower()
    data = message.get("data") if isinstance(message.get("data"), dict) else {}

    if seg_type == "text":
        return sanitize_cq_media_codes(str(data.get("text") or ""))
    if seg_type == "image":
        url = clean_media_source(data.get("url") or data.get("file") or "")
        return f"[CQ:image,url={escape_cq_param(url)}]" if url else "[CQ:image]"
    if seg_type == "video":
        url = clean_media_source(data.get("url") or data.get("file") or "")
        return f"[CQ:video,url={escape_cq_param(url)}]" if url else "[CQ:video]"
    if seg_type == "record":
        url = clean_media_source(data.get("url") or data.get("file") or "")
        return f"[CQ:record,url={escape_cq_param(url)}]" if url else "[语音]"
    if seg_type == "face":
        return f"[CQ:face,id={escape_cq_param(data.get('id', ''))}]"
    if seg_type == "at":
        return f"[CQ:at,qq={escape_cq_param(data.get('qq', ''))}]"
    if seg_type == "reply":
        return f"[CQ:reply,id={escape_cq_param(data.get('id', ''))}]"
    if seg_type == "json":
        payload = data.get("data", "")
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False, default=str)
        return f"[CQ:json,data={escape_cq_param(payload)}]"
    if seg_type == "file":
        name = clean_file_name(data.get("name") or data.get("file") or "文件")
        url = clean_media_source(data.get("url") or "")
        if url:
            return f"[CQ:file,name={escape_cq_param(name)},url={escape_cq_param(url)}]"
        return f"[文件: {name}]"
    if seg_type == "forward":
        forward_id = data.get("id") or data.get("res_id") or message.get("id") or ""
        content = data.get("content") or message.get("content")
        if content:
            text = _serialize_forward_nodes(content)
            if forward_id and text.startswith("[合并转发]"):
                return text.replace(
                    "[合并转发]", f"[合并转发,id={escape_cq_param(forward_id)}]", 1
                )
            return text
        if forward_id:
            return f"[合并转发,id={escape_cq_param(forward_id)}]\n[合并转发结束]"
        return "[合并转发]\n[合并转发结束]"
    if seg_type == "node":
        return _serialize_forward_nodes([data])

    return sanitize_cq_media_codes(str(data.get("text") or ""))


def serialize_message_chain(chain) -> str:
    """Serialize an AstrBot message chain as text with CQ media markers."""
    parts = []
    for comp in chain:
        cls_name = comp.__class__.__name__
        try:
            if cls_name == "Plain":
                parts.append(getattr(comp, "text", ""))
            elif cls_name == "Image":
                url = clean_media_source(
                    getattr(comp, "url", "") or getattr(comp, "file", "")
                )
                if url:
                    url = escape_cq_param(url)
                    width = positive_int(getattr(comp, "width", 0))
                    height = positive_int(getattr(comp, "height", 0))
                    dim_str = (
                        f",width={width},height={height}" if width and height else ""
                    )
                    parts.append(f"[CQ:image,url={url}{dim_str}]")
                else:
                    parts.append("[CQ:image]")
            elif cls_name == "Video":
                url = clean_media_source(
                    getattr(comp, "url", "") or getattr(comp, "file", "")
                )
                if url:
                    url = escape_cq_param(url)
                    parts.append(f"[CQ:video,url={url}]")
                else:
                    parts.append("[CQ:video]")
            elif cls_name == "Record":
                url = clean_media_source(
                    getattr(comp, "url", "") or getattr(comp, "file", "")
                )
                if url:
                    url = escape_cq_param(url)
                    parts.append(f"[CQ:record,url={url}]")
                else:
                    parts.append("[语音]")
            elif cls_name == "Face":
                face_id = getattr(comp, "id", "")
                parts.append(f"[CQ:face,id={face_id}]")
            elif cls_name == "At":
                qq = getattr(comp, "qq", "")
                parts.append(f"[CQ:at,qq={qq}]")
            elif cls_name == "Reply":
                rid = getattr(comp, "id", "")
                parts.append(f"[CQ:reply,id={rid}]")
            elif cls_name.lower() == "json":
                data = getattr(comp, "data", "") or getattr(comp, "text", "")
                if not isinstance(data, str):
                    data = json.dumps(data, ensure_ascii=False, default=str)
                escaped_data = escape_cq_param(data)
                parts.append(f"[CQ:json,data={escaped_data}]")
            elif cls_name in ("Forward", "Node", "Nodes"):
                parts.append(_serialize_forward_component(comp))
            elif cls_name == "File":
                name = clean_file_name(getattr(comp, "name", "文件"))
                url = clean_media_source(
                    getattr(comp, "url", "") or getattr(comp, "file", "")
                )
                if url:
                    name = escape_cq_param(name)
                    url = escape_cq_param(url)
                    parts.append(f"[CQ:file,name={name},url={url}]")
                else:
                    parts.append(f"[文件: {name}]")
            elif cls_name == "Poke":
                parts.append("[戳一戳]")
            else:
                text = getattr(comp, "text", None)
                if text:
                    parts.append(str(text))
        except Exception as e:
            logger.debug(f"Chat Archive: failed to serialize {cls_name}: {e}")
    return sanitize_cq_media_codes("".join(parts))
