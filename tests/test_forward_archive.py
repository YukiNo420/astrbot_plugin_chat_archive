import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("archive-tests")
sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
sys.modules.setdefault("astrbot.api", api)
from forward_archive import expand_forward_payload  # noqa: E402
from serializer import serialize_onebot_message  # noqa: E402


def node(content, name="sender"):
    return {"type": "node", "data": {"nickname": name, "content": content}}


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_reference_and_file_url(self):
        call = AsyncMock(side_effect=[
            {"data": {"messages": [node([{"type": "file", "data": {"name": "notes.txt", "file_id": "f"}}], "inner")]}},
            {"data": {"url": "https://example.com/notes.txt?a=1&b=2"}},
        ])
        raw = [node([{"type": "forward", "data": {"id": "child"}}], "outer")]
        expanded = await expand_forward_payload(raw, call)
        text = serialize_onebot_message(expanded)
        self.assertIn("    1. inner:", text)
        self.assertEqual(text.count("[合并转发结束]"), 2)
        self.assertIn("[CQ:file,name=notes.txt,url=https://", text)
        self.assertNotIn("content", raw[0]["data"]["content"][0]["data"])
        self.assertEqual(call.await_args_list[1].args, ("get_private_file_url",))

    async def test_cycle_and_budget_are_bounded(self):
        ref = {"type": "forward", "data": {"id": "same"}}
        call = AsyncMock(return_value={"messages": [node([ref])]})
        expanded = await expand_forward_payload([ref], call, max_calls=2)
        self.assertEqual(call.await_count, 1)
        self.assertIn("id=same", serialize_onebot_message(expanded))
        call.reset_mock()
        await expand_forward_payload([ref], call, max_calls=0)
        call.assert_not_awaited()

    async def test_unavailable_file_remains_named_and_local_path_is_rejected(self):
        file = {"type": "file", "data": {"name": "notes.txt", "file_id": "f"}}
        for result in ({"data": {"url": "C:/private/notes.txt"}}, RuntimeError("unsupported")):
            call = AsyncMock(side_effect=result if isinstance(result, Exception) else None, return_value=result)
            text = serialize_onebot_message(await expand_forward_payload([node([file])], call))
            self.assertIn("[文件: notes.txt]", text)
            self.assertNotIn("C:/private", text)

    async def test_group_file_uses_only_supplied_group(self):
        call = AsyncMock(return_value={"url": "https://example.com/f"})
        file = {"type": "file", "data": {"name": "f", "file_id": "file", "group_id": "group"}}
        await expand_forward_payload([node([file])], call)
        call.assert_awaited_once_with("get_group_file_url", group_id="group", file_id="file")

    async def test_inline_nested_content_and_numbered_plain_text(self):
        call = AsyncMock()
        raw = [node([node([{"type": "text", "data": {"text": "hello\n2. ordinary: text"}}], "inner")], "outer")]
        text = serialize_onebot_message(await expand_forward_payload(raw, call))
        call.assert_not_awaited()
        self.assertIn("        2. ordinary: text", text)


if __name__ == "__main__":
    unittest.main()
