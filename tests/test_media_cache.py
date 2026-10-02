from __future__ import annotations

import hashlib
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from media_cache import (
    ArchiveMediaCache,
    PinnedPublicResolver,
    is_passive_media_file,
    sniff_passive_media,
    validate_remote_media_url,
)


class MediaCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = ArchiveMediaCache(
            config={"basic": {}},
            cache_dir=Path(self.temp_dir.name),
        )

    async def asyncTearDown(self):
        await self.cache.close()
        self.temp_dir.cleanup()

    async def test_existing_valid_cache_hit_skips_network(self):
        url = "https://gchat.qpic.cn/example.jpg"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        cached = Path(self.temp_dir.name) / f"{digest}.jpg"
        cached.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 32)

        async def fail_client():
            self.fail("HTTP client should not run for an existing cache hit")

        self.cache._get_client = fail_client
        result = await self.cache.download_media_to_cache(url)
        self.assertEqual(result, f"/static/cache/{cached.name}")

    def test_existing_cache_rejects_corrupt_symlink_and_directory(self):
        digest = "a" * 32
        corrupt = Path(self.temp_dir.name) / f"{digest}.jpg"
        corrupt.write_bytes(b"not-a-jpeg")
        self.assertEqual(self.cache._existing_cached_url(digest), "")
        self.assertFalse(is_passive_media_file(corrupt))

        corrupt.unlink()
        target = Path(self.temp_dir.name) / "target.jpg"
        target.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 32)
        corrupt.symlink_to(target)
        self.assertEqual(self.cache._existing_cached_url(digest), "")

        corrupt.unlink()
        corrupt.mkdir()
        self.assertEqual(self.cache._existing_cached_url(digest), "")

    async def test_url_credentials_and_custom_ports_are_rejected_before_network(self):
        async def fail_client():
            self.fail("invalid URLs must be rejected before creating a client")

        self.cache._get_client = fail_client
        for url in (
            "https://user:pass@gchat.qpic.cn/file.jpg",
            "https://gchat.qpic.cn:8443/file.jpg",
            "https://gchat.qpic.cn:bad/file.jpg",
        ):
            with self.subTest(url=url):
                self.assertEqual(await self.cache.download_media_to_cache(url), url)

    def test_explicit_empty_allowlist_denies_all(self):
        cache = ArchiveMediaCache(
            config={"basic": {"allowed_media_domains": []}},
            cache_dir=Path(self.temp_dir.name),
        )
        self.assertEqual(cache.get_allowed_media_domains(), set())
        with self.assertRaises(PermissionError):
            validate_remote_media_url("https://gchat.qpic.cn/a.jpg", set())

    async def test_parallel_replacements_preserve_original_order(self):
        async def fake_replace(match):
            return f"[cached:{match.group(1)}]"

        self.cache.replace_cq_media_url = fake_replace
        result = await self.cache.process_and_cache_media_in_string(
            "a[CQ:image,url=https://gchat.qpic.cn/a.jpg]"
            "b[CQ:video,url=https://gchat.qpic.cn/b.mp4]c"
        )
        self.assertEqual(result, "a[cached:image]b[cached:video]c")

    async def test_quota_evicts_oldest_managed_file_and_cleans_stale_tmp(self):
        self.cache.conf["basic"]["allow_cache_eviction"] = True
        oldest = Path(self.temp_dir.name) / f"{'a' * 32}.jpg"
        newest = Path(self.temp_dir.name) / f"{'b' * 32}.jpg"
        stale_tmp = Path(self.temp_dir.name) / f"{'c' * 32}.tmp"
        oldest.write_bytes(b"123456")
        newest.write_bytes(b"1234")
        stale_tmp.write_bytes(b"temporary")
        old_time = time.time() - 7200
        os.utime(oldest, (old_time, old_time))
        os.utime(stale_tmp, (old_time, old_time))
        self.cache.get_max_cache_bytes = lambda: 12

        self.assertTrue(await self.cache.ensure_cache_capacity(4))
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())
        self.assertFalse(stale_tmp.exists())

    async def test_default_quota_maintenance_runs(self):
        with mock.patch.dict(os.environ, {"ARCHIVE_MEDIA_CACHE_MAX_MB": ""}):
            self.assertEqual(self.cache.get_max_cache_bytes(), 10 * 1024**3)
            self.assertTrue(await self.cache.ensure_cache_capacity(0))

    async def test_fresh_parallel_temp_files_count_toward_quota(self):
        first = Path(self.temp_dir.name) / f"{'d' * 32}.tmp"
        second = Path(self.temp_dir.name) / f"{'e' * 32}.jpg.tmp"
        first.write_bytes(b"1234567")
        second.write_bytes(b"1234567")
        self.cache.get_max_cache_bytes = lambda: 10
        self.assertFalse(await self.cache.ensure_cache_capacity(0))
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    async def test_capacity_catalog_is_reused_until_forced_reconciliation(self):
        original_iterdir = Path.iterdir
        scans = 0

        def counted_iterdir(path):
            nonlocal scans
            if path == self.cache.cache_dir:
                scans += 1
            return original_iterdir(path)

        with mock.patch.object(Path, "iterdir", counted_iterdir):
            self.assertTrue(await self.cache.ensure_cache_capacity(0))
            self.assertTrue(await self.cache.ensure_cache_capacity(0))
            self.assertEqual(scans, 1)
            self.assertTrue(
                await self.cache.ensure_cache_capacity(0, force_rescan=True)
            )
            self.assertEqual(scans, 2)

    async def test_parallel_reservations_prevent_cache_quota_overcommit(self):
        self.cache.conf["basic"]["allow_cache_eviction"] = True
        first_tmp = Path(self.temp_dir.name) / f"{'a' * 32}.tmp"
        first_dest = Path(self.temp_dir.name) / f"{'a' * 32}.jpg"
        second_tmp = Path(self.temp_dir.name) / f"{'b' * 32}.tmp"
        self.cache.get_max_cache_bytes = lambda: 10

        self.assertTrue(await self.cache.reserve_cache_capacity(first_tmp, 7))
        self.assertFalse(await self.cache.reserve_cache_capacity(second_tmp, 4))
        first_tmp.write_bytes(b"1234567")
        self.assertTrue(await self.cache.finalize_cache_file(first_tmp, first_dest))
        self.assertEqual(first_dest.read_bytes(), b"1234567")
        self.assertTrue(await self.cache.reserve_cache_capacity(second_tmp, 4))
        self.assertFalse(first_dest.exists())
        await self.cache.release_cache_reservation(second_tmp)
        self.assertFalse(self.cache._cache_reservations)

    async def test_default_quota_preserves_files_and_counts_stale_temporary_files(self):
        stale = Path(self.temp_dir.name) / f"{'c' * 32}.tmp"
        existing = Path(self.temp_dir.name) / f"{'a' * 32}.jpg"
        stale.write_bytes(b"1234567")
        existing.write_bytes(b"1234")
        old_time = time.time() - 7200
        os.utime(stale, (old_time, old_time))
        self.cache.get_max_cache_bytes = lambda: 10
        self.assertFalse(await self.cache.ensure_cache_capacity(0))
        self.assertEqual(stale.read_bytes(), b"1234567")
        self.assertEqual(existing.read_bytes(), b"1234")

    async def test_undeletable_stale_temporary_file_still_counts_toward_quota(self):
        self.cache.conf["basic"]["allow_cache_eviction"] = True
        stale = Path(self.temp_dir.name) / f"{'c' * 32}.tmp"
        stale.write_bytes(b"12345678901")
        old_time = time.time() - 7200
        os.utime(stale, (old_time, old_time))
        self.cache.get_max_cache_bytes = lambda: 10

        with mock.patch.object(Path, "unlink", side_effect=PermissionError):
            self.assertFalse(await self.cache.ensure_cache_capacity(0))

        self.assertEqual(stale.read_bytes(), b"12345678901")
        self.assertEqual(self.cache._cache_total_bytes, 11)

    async def test_resolver_rejects_private_dns_answer_at_connect_time(self):
        resolver = PinnedPublicResolver(allow_fake_ip=False)
        private_result = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.2", 443),
            )
        ]
        with mock.patch.object(socket, "getaddrinfo", return_value=private_result):
            with self.assertRaises(OSError):
                await resolver.resolve("gchat.qpic.cn", 443)

    async def test_svg_response_is_not_cached(self):
        url = "https://gchat.qpic.cn/active.svg"

        class FakeContent:
            async def iter_chunked(self, _size):
                yield b"<svg></svg>"

        class FakeResponse:
            status = 200
            headers = {"content-type": "image/svg+xml"}
            content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class FakeClient:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        async def fake_client():
            return FakeClient()

        self.cache._get_client = fake_client
        result = await self.cache.download_media_to_cache(url)
        self.assertEqual(result, url)
        self.assertFalse(any(Path(self.temp_dir.name).iterdir()))

    async def test_generic_mime_caches_strictly_sniffed_media(self):
        samples = (
            (
                "png",
                b"\x89PNG\r\n\x1a\n" + b"x" * 80,
                ".png",
            ),
            (
                "jpeg",
                b"\xff\xd8\xff\xe0" + b"x" * 80,
                ".jpg",
            ),
            (
                "mp4",
                b"\x00\x00\x00\x18ftypisom" + b"x" * 80,
                ".mp4",
            ),
        )

        class FakeContent:
            def __init__(self, data):
                self.data = data
                self.offset = 0

            async def read(self, size):
                chunk = self.data[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

            async def iter_chunked(self, size):
                while self.offset < len(self.data):
                    yield await self.read(size)

        class FakeResponse:
            status = 200

            def __init__(self, data):
                self.headers = {
                    "content-type": "application/octet-stream",
                    "content-length": str(len(data)),
                }
                self.content = FakeContent(data)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class FakeClient:
            def __init__(self, data):
                self.data = data

            def get(self, *_args, **_kwargs):
                return FakeResponse(self.data)

        for label, payload, extension in samples:
            with self.subTest(label=label):

                async def fake_client(data=payload):
                    return FakeClient(data)

                self.cache._get_client = fake_client
                url = f"https://gchat.qpic.cn/{label}"
                result = await self.cache.download_media_to_cache(url)
                self.assertTrue(result.endswith(extension))
                cached_path = Path(self.temp_dir.name) / result.rsplit("/", 1)[-1]
                self.assertEqual(cached_path.read_bytes(), payload)

    async def test_generic_mime_rejects_active_and_unknown_content(self):
        payloads = (
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            b"<!doctype html><script>alert(1)</script>",
            b"%PDF-1.7\n",
            b"PK\x03\x04archive",
            b"not a recognized media file",
        )

        class FakeContent:
            def __init__(self, data):
                self.data = data
                self.offset = 0

            async def read(self, size):
                chunk = self.data[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

            async def iter_chunked(self, _size):
                return
                yield  # pragma: no cover

        class FakeResponse:
            status = 200

            def __init__(self, data):
                self.headers = {"content-type": "application/octet-stream"}
                self.content = FakeContent(data)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class FakeClient:
            def __init__(self, data):
                self.data = data

            def get(self, *_args, **_kwargs):
                return FakeResponse(self.data)

        for index, payload in enumerate(payloads):
            with self.subTest(index=index):

                async def fake_client(data=payload):
                    return FakeClient(data)

                self.cache._get_client = fake_client
                url = f"https://gchat.qpic.cn/rejected-{index}"
                self.assertEqual(
                    await self.cache.download_media_to_cache(url),
                    url,
                )
        self.assertFalse(any(Path(self.temp_dir.name).iterdir()))

    async def test_declared_image_mime_with_active_bytes_is_rejected(self):
        payload = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"

        class FakeContent:
            def __init__(self):
                self.offset = 0

            async def read(self, size):
                chunk = payload[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

            async def iter_chunked(self, _size):
                return
                yield  # pragma: no cover

        class FakeResponse:
            status = 200
            headers = {
                "content-type": "image/jpeg",
                "content-length": str(len(payload)),
            }

            def __init__(self):
                self.content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class FakeClient:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        async def fake_client():
            return FakeClient()

        self.cache._get_client = fake_client
        url = "https://gchat.qpic.cn/disguised.jpg"
        self.assertEqual(await self.cache.download_media_to_cache(url), url)
        self.assertFalse(any(Path(self.temp_dir.name).iterdir()))

    def test_sniffer_does_not_classify_active_or_document_formats(self):
        self.assertEqual(
            sniff_passive_media(b"\x89PNG\r\n\x1a\nrest"),
            ("image/png", ".png"),
        )
        for payload in (
            b"<svg></svg>",
            b"<html></html>",
            b"<?xml version='1.0'?>",
            b"%PDF-1.7",
            b"PK\x03\x04",
            b"unknown",
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(sniff_passive_media(payload))


if __name__ == "__main__":
    unittest.main()
