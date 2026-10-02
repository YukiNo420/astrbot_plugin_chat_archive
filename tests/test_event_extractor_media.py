from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from event_extractor import ArchiveEventExtractor
from media_cache import ArchiveMediaCache


class Plain:
    def __init__(self, text: str):
        self.text = text


class Reply:
    def __init__(self, message_id: str):
        self.id = message_id


class Image:
    def __init__(
        self,
        url: str,
        *,
        width: int = 0,
        height: int = 0,
    ):
        self.url = url
        self.file = url
        self.width = width
        self.height = height


class Video:
    def __init__(self, url: str):
        self.file = url
        self.url = url


class EventExtractorMediaTests(unittest.IsolatedAsyncioTestCase):
    def _event(self, chain):
        return SimpleNamespace(get_messages=lambda: chain)

    async def test_local_preprocessed_image_restores_onebot_url_then_caches(self):
        remote_url = (
            "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc,def"
        )
        event = self._event(
            [
                Plain("before"),
                Reply("42"),
                Image(
                    "/astrbot/data/temp/media_image_deadbeef.jpg",
                    width=640,
                    height=480,
                ),
                Plain("after"),
            ]
        )
        platform_raw = {
            "message": [
                {"type": "text", "data": {"text": "raw-before"}},
                {"type": "reply", "data": {"id": "42"}},
                {"type": "image", "data": {"url": remote_url}},
                {"type": "text", "data": {"text": "raw-after"}},
            ]
        }

        extracted = ArchiveEventExtractor.event_raw_message_text(
            event,
            platform_raw,
        )

        self.assertIn("before[CQ:reply,id=42][CQ:image,url=https://", extracted)
        self.assertIn("width=640,height=480", extracted)
        self.assertTrue(extracted.endswith("after"))
        self.assertNotIn("/astrbot/data/temp/", extracted)

        with tempfile.TemporaryDirectory() as temporary:
            cache = ArchiveMediaCache(
                config={},
                cache_dir=Path(temporary),
            )
            cache.download_media_to_cache = AsyncMock(
                return_value="/static/cache/0123456789abcdef0123456789abcdef.jpg"
            )

            cached = await cache.process_and_cache_media_in_string(extracted)

        self.assertIn(
            "[CQ:image,url=/static/cache/"
            "0123456789abcdef0123456789abcdef.jpg,width=640,height=480]",
            cached,
        )
        cache.download_media_to_cache.assert_awaited_once_with(remote_url)

    def test_multiple_media_restore_by_type_and_occurrence(self):
        event = self._event(
            [
                Image("/astrbot/data/temp/first.jpg"),
                Video("/astrbot/data/temp/clip.mp4"),
                Image("/astrbot/data/temp/second.jpg"),
            ]
        )
        platform_raw = {
            "message": [
                {
                    "type": "image",
                    "data": {"url": "https://gchat.qpic.cn/first.jpg"},
                },
                {
                    "type": "video",
                    "data": {"url": "https://multimedia.nt.qq.com.cn/clip.mp4"},
                },
                {
                    "type": "image",
                    "data": {"url": "https://gchat.qpic.cn/second.jpg"},
                },
            ]
        }

        extracted = ArchiveEventExtractor.event_raw_message_text(
            event,
            platform_raw,
        )

        self.assertEqual(
            extracted,
            "[CQ:image,url=https://gchat.qpic.cn/first.jpg]"
            "[CQ:video,url=https://multimedia.nt.qq.com.cn/clip.mp4]"
            "[CQ:image,url=https://gchat.qpic.cn/second.jpg]",
        )

    def test_existing_remote_and_cache_sources_are_not_overwritten(self):
        chain_message = (
            "[CQ:image,url=https://gchat.qpic.cn/already.jpg]"
            "[CQ:image,url=/static/cache/0123456789abcdef0123456789abcdef.jpg]"
        )
        platform_message = (
            "[CQ:image,url=https://gchat.qpic.cn/raw-first.jpg]"
            "[CQ:image,url=https://gchat.qpic.cn/raw-second.jpg]"
        )

        restored = ArchiveEventExtractor._restore_platform_media_urls(
            chain_message,
            platform_message,
        )

        self.assertEqual(restored, chain_message)

    def test_missing_matching_remote_segment_leaves_local_marker_unchanged(self):
        chain_message = (
            "[CQ:image,url=/astrbot/data/temp/first.jpg]"
            "[CQ:image,url=/astrbot/data/temp/second.jpg]"
        )
        platform_message = "[CQ:image,url=https://gchat.qpic.cn/first.jpg]"

        restored = ArchiveEventExtractor._restore_platform_media_urls(
            chain_message,
            platform_message,
        )

        self.assertEqual(
            restored,
            chain_message,
        )

    def test_existing_remote_slot_does_not_shift_later_local_image(self):
        chain_message = (
            "[CQ:image,url=/astrbot/data/temp/first.jpg]"
            "[CQ:image,url=https://gchat.qpic.cn/chain-second.jpg]"
            "[CQ:image,url=/astrbot/data/temp/third.jpg]"
        )
        platform_message = (
            "[CQ:image,url=https://gchat.qpic.cn/raw-first.jpg]"
            "[CQ:image,url=https://gchat.qpic.cn/raw-second.jpg]"
            "[CQ:image,url=https://gchat.qpic.cn/raw-third.jpg]"
        )

        restored = ArchiveEventExtractor._restore_platform_media_urls(
            chain_message,
            platform_message,
        )

        self.assertEqual(
            restored,
            "[CQ:image,url=https://gchat.qpic.cn/raw-first.jpg]"
            "[CQ:image,url=https://gchat.qpic.cn/chain-second.jpg]"
            "[CQ:image,url=https://gchat.qpic.cn/raw-third.jpg]",
        )

    def test_non_http_platform_candidate_is_not_used(self):
        chain_message = (
            "[CQ:image,url=/astrbot/data/temp/first.jpg]"
            "[CQ:image,url=/static/cache/0123456789abcdef0123456789abcdef.jpg]"
        )
        platform_message = (
            "[CQ:image,url=file:///astrbot/data/temp/raw.jpg]"
            "[CQ:image,url=javascript:alert(1)]"
        )

        restored = ArchiveEventExtractor._restore_platform_media_urls(
            chain_message,
            platform_message,
        )

        self.assertEqual(restored, chain_message)
