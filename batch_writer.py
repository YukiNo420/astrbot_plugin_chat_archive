from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import suppress
from pathlib import Path

from astrbot.api import logger

try:
    from .db_config import get_db_connection
except ImportError:
    from db_config import get_db_connection


class ArchiveBatchWriter:
    def __init__(
        self,
        *,
        batch_size: int,
        flush_interval: float,
        data_dir: Path,
        queue_max_size: int = 5000,
    ):
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.data_dir = data_dir
        self._write_queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max_size)
        self._writer_task: asyncio.Task | None = None
        self._writer_lock = asyncio.Lock()
        self._shutting_down = True
        self._generation = 0

    async def start(self):
        """Start (or safely restart) the background batch writer task."""
        async with self._writer_lock:
            if self._writer_task is not None and not self._writer_task.done():
                return
            self._generation += 1
            self._shutting_down = False
            self._writer_task = asyncio.create_task(self.run())

    async def enqueue(self, record: tuple) -> bool:
        """Enqueue a record without allowing a producer to cross a stop boundary."""
        generation = self._generation
        async with self._writer_lock:
            if (
                self._shutting_down
                or generation != self._generation
                or self._writer_task is None
                or self._writer_task.done()
            ):
                return False
            try:
                self._write_queue.put_nowait(record)
                return True
            except asyncio.QueueFull:
                logger.error(
                    "Chat Archive: 写入队列已满，丢弃一条归档记录以保护主进程内存。"
                )
                return False

    async def stop(self):
        """Stop accepting writes and wait until every accepted record is durable."""
        async with self._writer_lock:
            self._shutting_down = True
            self._generation += 1
            task = self._writer_task

            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

            # A task that failed before stop() may have left records behind.
            remaining = self._drain_queue()
            if remaining:
                await asyncio.to_thread(self.flush_batches_sync, remaining)
            self._writer_task = None

    async def run(self):
        """Collect records until batch_size or flush_interval, then persist them."""
        buffer: list[tuple] = []
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    buffer.append(await self._write_queue.get())
                    deadline = loop.time() + self.flush_interval

                    while len(buffer) < self.batch_size:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            break
                        try:
                            item = await asyncio.wait_for(
                                self._write_queue.get(), timeout=remaining
                            )
                        except asyncio.TimeoutError:
                            break
                        buffer.append(item)

                    batch = buffer
                    buffer = []
                    await self._flush_batch_async(batch)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        "Chat Archive: batch writer error, discarding "
                        f"{len(buffer)} messages: {e}"
                    )
                    buffer.clear()
        except asyncio.CancelledError:
            buffer.extend(self._drain_queue())
            if buffer:
                try:
                    await asyncio.to_thread(self.flush_batches_sync, buffer)
                except Exception as e:
                    logger.error(f"Chat Archive: final flush error: {e}")
            raise

    async def _flush_batch_async(self, batch: list[tuple]) -> None:
        """Keep the worker alive until an already-started thread flush finishes."""
        flush_task = asyncio.create_task(
            asyncio.to_thread(self.flush_batch_sync, batch)
        )
        try:
            await asyncio.shield(flush_task)
        except asyncio.CancelledError:
            # Cancelling asyncio.to_thread only cancels the waiter. The SQLite
            # work continues, so stop() must await it before reporting success.
            await flush_task
            raise

    def _drain_queue(self) -> list[tuple]:
        records: list[tuple] = []
        while True:
            try:
                records.append(self._write_queue.get_nowait())
            except asyncio.QueueEmpty:
                return records

    def write_failed_batch(self, batch: list[tuple], reason: str):
        """Persist failed DB writes for later manual recovery instead of silently losing them."""
        try:
            self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            failed_path = self.data_dir / "chat_archive_failed_writes.jsonl"
            with suppress(Exception):
                failed_path.touch(mode=0o600, exist_ok=True)
            with open(failed_path, "a", encoding="utf-8") as f:
                for item in batch:
                    f.write(
                        json.dumps(
                            {
                                "reason": reason,
                                "record": item,
                                "failed_at": int(time.time()),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            with suppress(Exception):
                failed_path.chmod(0o600)
            logger.error(
                f"Chat Archive: {len(batch)} 条写入失败记录已保存到 {failed_path}"
            )
        except Exception as e:
            logger.error(f"Chat Archive: 保存失败写入记录也失败了: {e}")

    def flush_batches_sync(self, records: list[tuple]):
        """Flush records in bounded chunks to avoid long SQLite write locks."""
        for start in range(0, len(records), self.batch_size):
            self.flush_batch_sync(records[start : start + self.batch_size])

    def flush_batch_sync(self, batch: list[tuple]):
        """Flush a batch of messages to the database."""
        if not batch:
            return
        try:
            batch = [self.normalize_record_tuple(record) for record in batch]
        except Exception as e:
            logger.error(
                f"Chat Archive: invalid archive batch, dropping {len(batch)} messages: {e}"
            )
            self.write_failed_batch(batch, str(e))
            return

        conn = None
        try:
            conn = get_db_connection()
            conn.executemany(
                "INSERT INTO chat_history ("
                "user_id, sender_name, message, timestamp, session_id, "
                "message_type, session_name, msg_id, platform_id, platform_name, avatar_url, guild_avatar_url"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception as rollback_error:
                logger.error(f"Chat Archive: DB rollback failed: {rollback_error}")
            if isinstance(e, sqlite3.OperationalError):
                logger.error(
                    "Chat Archive: Failed to flush batch after atomic DB retries "
                    f"({len(batch)} msgs): {e}"
                )
            else:
                logger.error(
                    f"Chat Archive: Failed to flush batch ({len(batch)} msgs): {e}"
                )
            self.write_failed_batch(batch, str(e))
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def normalize_record_tuple(record: tuple) -> tuple:
        """Normalize archive write tuples across old and new in-memory callers."""
        if len(record) == 12:
            return record
        if len(record) == 11:
            return (*record, "")
        if len(record) == 10:
            return (*record, "", "")
        if len(record) == 8:
            return (*record, "", "", "", "")
        raise ValueError(f"unexpected archive record length: {len(record)}")
