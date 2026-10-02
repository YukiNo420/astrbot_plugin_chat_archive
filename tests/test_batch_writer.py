from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from batch_writer import ArchiveBatchWriter


class RecordingBatchWriter(ArchiveBatchWriter):
    def __init__(self, *args, block_flush: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.flushed: list[list[tuple]] = []
        self.flush_started = threading.Event()
        self.flush_release = threading.Event()
        if not block_flush:
            self.flush_release.set()

    def flush_batch_sync(self, batch: list[tuple]):
        self.flush_started.set()
        self.flush_release.wait(timeout=5)
        self.flushed.append(list(batch))


class BatchWriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def _writer(self, **kwargs) -> RecordingBatchWriter:
        return RecordingBatchWriter(
            batch_size=kwargs.pop("batch_size", 10),
            flush_interval=kwargs.pop("flush_interval", 0.1),
            data_dir=Path(self.temp_dir.name),
            **kwargs,
        )

    async def _wait_for_flush_count(
        self, writer: RecordingBatchWriter, count: int
    ) -> None:
        async def wait_until_ready():
            while len(writer.flushed) < count:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_until_ready(), timeout=1)

    async def test_sparse_traffic_is_collected_until_flush_interval(self):
        writer = self._writer(batch_size=10, flush_interval=0.15)
        await writer.start()

        for index in range(5):
            self.assertTrue(await writer.enqueue((index,)))
            await asyncio.sleep(0.015)

        self.assertFalse(writer.flush_started.is_set())
        await asyncio.wait_for(asyncio.to_thread(writer.flush_started.wait), timeout=1)
        await self._wait_for_flush_count(writer, 1)
        await writer.stop()

        self.assertEqual(writer.flushed, [[(0,), (1,), (2,), (3,), (4,)]])

    async def test_stop_waits_for_inflight_thread_and_instance_restarts(self):
        writer = self._writer(
            batch_size=1,
            flush_interval=1,
            block_flush=True,
        )
        await writer.start()
        self.assertTrue(await writer.enqueue(("before-stop",)))
        await asyncio.wait_for(asyncio.to_thread(writer.flush_started.wait), timeout=1)

        stop_task = asyncio.create_task(writer.stop())
        await asyncio.sleep(0.05)
        self.assertFalse(stop_task.done())

        writer.flush_release.set()
        await asyncio.wait_for(stop_task, timeout=1)
        self.assertEqual(writer.flushed, [[("before-stop",)]])
        self.assertFalse(await writer.enqueue(("rejected",)))

        writer.flush_started.clear()
        await writer.start()
        self.assertTrue(await writer.enqueue(("after-restart",)))
        await self._wait_for_flush_count(writer, 2)
        await writer.stop()
        self.assertEqual(writer.flushed[-1], [("after-restart",)])

    async def test_full_queue_rejects_immediately_and_stop_drains_accepted_records(
        self,
    ):
        writer = self._writer(
            batch_size=1,
            flush_interval=1,
            queue_max_size=1,
            block_flush=True,
        )
        await writer.start()
        self.assertTrue(await writer.enqueue(("in-flight",)))
        await asyncio.wait_for(asyncio.to_thread(writer.flush_started.wait), timeout=1)
        self.assertTrue(await writer.enqueue(("queued",)))

        rejected = await asyncio.wait_for(
            asyncio.gather(
                *(writer.enqueue((f"too-late-{index}",)) for index in range(10))
            ),
            timeout=0.2,
        )
        self.assertEqual(rejected, [False] * 10)

        stop_task = asyncio.create_task(writer.stop())
        await asyncio.sleep(0)
        self.assertFalse(stop_task.done())

        writer.flush_release.set()
        await asyncio.wait_for(stop_task, timeout=1)

        self.assertTrue(writer._write_queue.empty())
        self.assertEqual(
            writer.flushed,
            [[("in-flight",)], [("queued",)]],
        )


if __name__ == "__main__":
    unittest.main()
