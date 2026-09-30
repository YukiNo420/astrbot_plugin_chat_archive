import logging
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("archive-tests")
sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
sys.modules.setdefault("astrbot.api", api)
from serializer import serialize_message_chain, serialize_onebot_message  # noqa: E402


class MediaSerializerTests(unittest.TestCase):
    def test_video_http_url_wins_over_container_file(self):
        video = type("Video", (), {})()
        video.file = "/app/data/cache/video.mp4"
        video.url = "https://example.com/video.mp4?a=1&b=2"
        self.assertEqual(
            serialize_message_chain([video]),
            "[CQ:video,url=https://example.com/video.mp4?a=1&amp;b=2]",
        )

    def test_video_file_only_fallback_is_preserved(self):
        video = type("Video", (), {})()
        video.file = "https://example.com/file-only.mp4"
        video.url = ""
        self.assertEqual(
            serialize_message_chain([video]),
            "[CQ:video,url=https://example.com/file-only.mp4]",
        )

    def test_forward_raw_media_preserves_public_urls(self):
        text = serialize_onebot_message([{
            "type": "node",
            "data": {"nickname": "synthetic", "content": [
                {"type": "image", "data": {"file": "/container/image.jpg", "url": "https://example.com/image.jpg"}},
                {"type": "video", "data": {"file": "/container/video.mp4", "url": "https://example.com/video.mp4"}},
            ]},
        }])
        self.assertIn("[CQ:image,url=https://example.com/image.jpg]", text)
        self.assertIn("[CQ:video,url=https://example.com/video.mp4]", text)
        self.assertNotIn("/container/", text)
