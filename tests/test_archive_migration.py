"""Only disposable synthetic archives are used by these offline migration tests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import errno
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contrib import archive_migrate as tool


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.target = self.root / "bundle"
        with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
            db.execute(
                "CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT, payload BLOB)"
            )
            db.execute(
                "INSERT INTO messages(body,payload) VALUES (?,?)",
                ["合成记录", b"\x00\xff"],
            )
            db.commit()
        (self.source / "web_cache").mkdir()
        (self.source / "web_cache/shared.png").write_bytes(b"synthetic media")
        (self.source / "empty").mkdir()
        (self.source / "chat_archive_failed_writes.jsonl").write_text(
            '{"synthetic":true}\n'
        )
        (self.source / "unknown-state.txt").write_text("preserve all files")

    def tearDown(self):
        self.temp.cleanup()

    def hashes(self, path=None):
        root = path or self.source
        return {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")
            if p.is_file()
        }

    def copy(self, **kwargs):
        return tool.copy_archive(
            self.source, self.target, confirm_stopped=True, quiet_seconds=0, **kwargs
        )

    def test_cli_default_preview_is_read_only_and_requires_attestation(self):
        before = self.hashes()
        process = subprocess.run(
            [sys.executable, tool.__file__, str(self.source), str(self.target)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["mode"], "preview")
        self.assertFalse(self.target.exists())
        refused = subprocess.run(
            [
                sys.executable,
                tool.__file__,
                str(self.source),
                str(self.target),
                "--apply",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(self.hashes(), before)
        self.assertFalse(self.target.exists())

    def test_committed_wal_raw_snapshot_backup_and_unused_bundle_verification(self):
        dbpath = self.source / "chat_history.db"
        script = """import sqlite3,os,sys
db=sqlite3.connect(sys.argv[1]);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0');db.execute("INSERT INTO messages(body) VALUES ('committed only in WAL')");db.commit();os._exit(0)
"""
        subprocess.run(
            [sys.executable, "-c", script, str(dbpath)], check=True, timeout=5
        )
        self.assertTrue(Path(str(dbpath) + "-wal").stat().st_size > 0)
        db_only = self.root / "db-only.db"
        db_only.write_bytes(dbpath.read_bytes())
        with closing(sqlite3.connect(db_only)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1
            )
        before = self.hashes()
        self.assertTrue(self.copy()["ready"])
        self.assertEqual(self.hashes(), before)
        self.assertEqual(self.hashes(self.target / "snapshot"), before)
        with closing(sqlite3.connect(self.target / "data/chat_history.db")) as db:
            self.assertEqual(
                db.execute("SELECT body FROM messages ORDER BY id").fetchall(),
                [("合成记录",), ("committed only in WAL",)],
            )
            self.assertEqual(
                db.execute("SELECT payload FROM messages WHERE id=1").fetchone()[0],
                b"\x00\xff",
            )
        for name in (
            "web_cache/shared.png",
            "chat_archive_failed_writes.jsonl",
            "unknown-state.txt",
        ):
            self.assertEqual(
                (self.target / "data" / name).read_bytes(),
                (self.source / name).read_bytes(),
            )
        self.assertTrue((self.target / "data/empty").is_dir())
        self.assertTrue(tool.verify_bundle(self.target)["ready"])
        self.assertEqual(self.hashes(self.target / "snapshot"), before)
        self.assertFalse((self.target / "data/chat_history.db-wal").exists())
        self.assertEqual((self.target.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.target / "READY.json").stat().st_mode & 0o777), 0o600)

    def test_existing_target_nested_paths_and_external_db_refused(self):
        before = self.hashes()
        for destination in (
            self.source,
            self.source / "child",
            self.root,
            self.root / "missing/child",
        ):
            with self.assertRaises(tool.MigrationError):
                tool.preview(self.source, destination)
        self.target.mkdir()
        sentinel = self.target / "sentinel"
        sentinel.write_text("keep")
        with self.assertRaises(tool.MigrationError):
            self.copy()
        self.assertEqual(sentinel.read_text(), "keep")
        for name in ("../outside.db", "/outside.db", "missing.db"):
            with self.assertRaises(tool.MigrationError):
                tool.preview(self.source, self.root / "new", name)
        self.assertEqual(self.hashes(), before)

    def test_symlink_paths_entries_hardlinks_and_fifo_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(tool.MigrationError):
            tool.preview(link, self.target)
        with self.assertRaises(tool.MigrationError):
            tool.preview(self.source, link / "bundle")
        file_link = self.source / "linked"
        file_link.symlink_to(self.source / "chat_history.db")
        with self.assertRaises(tool.MigrationError):
            self.copy()
        file_link.unlink()
        os.link(self.source / "unknown-state.txt", self.source / "hardlinked")
        with self.assertRaises(tool.MigrationError):
            self.copy()
        (self.source / "hardlinked").unlink()
        os.mkfifo(self.source / "fifo")
        with self.assertRaises(tool.MigrationError):
            self.copy()
        self.assertFalse(self.target.exists())

    def test_low_space_and_mid_copy_enospc_preserve_source_and_reject_retry(self):
        before = self.hashes()
        with patch.object(
            tool.shutil, "disk_usage", return_value=type("Space", (), {"free": 0})()
        ):
            with self.assertRaisesRegex(tool.MigrationError, "space"):
                self.copy()
        self.assertFalse(self.target.exists())
        original = tool._read_file

        def out_of_space(path, deadline, destination=None):
            if destination is not None and path.name == "unknown-state.txt":
                raise OSError(errno.ENOSPC, "synthetic disk full")
            return original(path, deadline, destination)

        with patch.object(tool, "_read_file", side_effect=out_of_space):
            with self.assertRaises(OSError):
                self.copy()
        self.assertTrue((self.target / "INCOMPLETE.json").exists())
        with self.assertRaises(tool.MigrationError):
            tool.verify_bundle(self.target)
        with self.assertRaises(tool.MigrationError):
            self.copy()
        self.assertEqual(self.hashes(), before)

    def test_corrupt_database_and_validation_failure_leave_incomplete_bundle(self):
        (self.source / "chat_history.db").write_bytes(b"synthetic invalid SQLite")
        before = self.hashes()
        with self.assertRaises(sqlite3.DatabaseError):
            self.copy()
        self.assertEqual(self.hashes(), before)
        self.assertTrue((self.target / "INCOMPLETE.json").exists())
        self.assertFalse((self.target / "READY.json").exists())

    def test_actual_concurrent_write_is_detected_and_committed_source_row_survives(
        self,
    ):
        copied = threading.Event()
        written = threading.Event()
        original = tool._copy_tree

        def copy_and_wait(*args, **kwargs):
            original(*args, **kwargs)
            if args[1].name == "snapshot":
                copied.set()
                self.assertTrue(written.wait(3))

        def writer():
            self.assertTrue(copied.wait(3))
            with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
                db.execute(
                    "INSERT INTO messages(body) VALUES ('concurrent committed row')"
                )
                db.commit()
            written.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            with patch.object(tool, "_copy_tree", side_effect=copy_and_wait):
                with self.assertRaisesRegex(tool.MigrationError, "Source changed"):
                    self.copy()
            future.result(timeout=3)
        with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2
            )
        self.assertTrue((self.target / "INCOMPLETE.json").exists())

    def test_two_concurrent_copies_reserve_target_exclusively(self):
        before = self.hashes()
        barrier = threading.Barrier(2)
        original = tool._inventory

        def synchronized(root, deadline):
            result = original(root, deadline)
            if root == self.source and not self.target.exists():
                barrier.wait(timeout=3)
            return result

        with (
            patch.object(tool, "_inventory", side_effect=synchronized),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(self.copy) for _ in range(2)]
            results = []
            for future in futures:
                try:
                    results.append(future.result(timeout=5)["ready"])
                except (tool.MigrationError, FileExistsError):
                    results.append(False)
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.hashes(), before)
        self.assertTrue(tool.verify_bundle(self.target)["ready"])

    def test_timeout_interrupt_and_sigkill_never_mark_partial_output_ready(self):
        before = self.hashes()
        with patch.object(tool, "_normalize_database", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.copy()
        self.assertTrue((self.target / "INCOMPLETE.json").exists())
        target2 = self.root / "timeout"
        with self.assertRaises(tool.MigrationError):
            tool.copy_archive(self.source, target2, confirm_stopped=True, timeout=1e-12)
        self.assertFalse(target2.exists())
        target3 = self.root / "killed"
        script = """import sys,time
from contrib import archive_migrate as tool
def wait(*args):
 print('copy paused',flush=True);time.sleep(30)
tool._normalize_database=wait
tool.copy_archive(sys.argv[1],sys.argv[2],confirm_stopped=True,quiet_seconds=0)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.source), str(target3)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0])
            self.assertEqual(child.stdout.readline().strip(), "copy paused")
            child.kill()
            child.communicate(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
        with self.assertRaises(tool.MigrationError):
            tool.verify_bundle(target3)
        self.assertEqual(self.hashes(), before)

    def test_bundle_mutation_and_new_target_writes_invalidate_rollback_assumption(self):
        before = self.hashes()
        self.copy()
        with closing(sqlite3.connect(self.target / "data/chat_history.db")) as db:
            db.execute("INSERT INTO messages(body) VALUES ('new after cutover')")
            db.commit()
        with self.assertRaisesRegex(tool.MigrationError, "changed"):
            tool.verify_bundle(self.target)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(self.hashes(self.target / "snapshot"), before)

    def test_schema_identifiers_custom_filename_and_mismatch_validation(self):
        name = "custom database.db"
        (self.source / "chat_history.db").rename(self.source / name)
        with closing(sqlite3.connect(self.source / name)) as db:
            db.execute('CREATE TABLE "quoted""table" ("quoted""column" TEXT)')
            db.execute(
                'INSERT INTO "quoted""table" VALUES (?)',
                ["literal SQL '; DROP TABLE messages; --"],
            )
            db.commit()
        before = self.hashes()
        original = tool._logical_database
        calls = 0

        def mismatch(db, deadline):
            nonlocal calls
            calls += 1
            result = original(db, deadline)
            if calls == 2:
                result["schema_sha256"] = "synthetic mismatch"
            return result

        with patch.object(tool, "_logical_database", side_effect=mismatch):
            with self.assertRaisesRegex(tool.MigrationError, "does not match"):
                self.copy(database_name=name)
        self.assertEqual(self.hashes(), before)
        self.assertTrue((self.target / "INCOMPLETE.json").exists())
        target2 = self.root / "successful"
        tool.copy_archive(
            self.source,
            target2,
            confirm_stopped=True,
            database_name=name,
            quiet_seconds=0,
        )
        self.assertTrue(tool.verify_bundle(target2)["ready"])

    def test_public_archive_schema_trash_stats_and_cache_time_preserved(self):
        api = types.ModuleType("astrbot.api")
        api.logger = logging.getLogger("synthetic-migration")
        sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
        sys.modules.setdefault("astrbot.api", api)
        with patch.dict(
            os.environ, {"ARCHIVE_CONFIG_PATH": str(self.root / "absent.json")}
        ):
            module = importlib.import_module("db_config")
            with (
                patch.object(module, "DB_PATH", str(self.source / "chat_history.db")),
                patch.object(module, "_POOL", None),
            ):
                module.init_db()
                try:
                    with module.get_db_connection() as db:
                        db.execute(
                            "INSERT INTO chat_history(session_id,message,timestamp) VALUES (?,?,?)",
                            ["qq:group:42", "synthetic archive", 123],
                        )
                        db.execute(
                            "INSERT INTO archive_trash VALUES (?,?,?,?)",
                            [
                                99,
                                "synthetic-operation",
                                "qq:group:42",
                                '{"id":99,"message":"synthetic trash"}',
                            ],
                        )
                        db.execute(
                            "INSERT INTO archive_management_operations VALUES (?,?,?,?,?)",
                            ["synthetic-operation", "qq:group:42", 123, "manual", 1],
                        )
                        db.commit()
                finally:
                    module.get_connection_pool().close_all()
        media = self.source / "web_cache/shared.png"
        os.utime(media, ns=(1000000000, 123456789000))
        before = self.hashes()
        self.copy()
        manifest = json.loads((self.target / "READY.json").read_text())
        for table in (
            "chat_history",
            "session_stats",
            "archive_trash",
            "archive_management_operations",
        ):
            self.assertEqual(manifest["database"]["tables"][table]["rows"], 1)
        self.assertEqual(
            (self.target / "data/web_cache/shared.png").stat().st_mtime_ns,
            media.stat().st_mtime_ns,
        )
        with closing(sqlite3.connect(self.target / "data/chat_history.db")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT message FROM chat_history_fts WHERE chat_history_fts MATCH 'synthetic'"
                ).fetchall(),
                [("synthetic archive",)],
            )
        self.assertIn("chat_history_fts_data", manifest["database"]["tables"])
        self.assertIn("chat_history_fts_docsize", manifest["database"]["tables"])
        self.assertEqual(self.hashes(), before)
        self.assertTrue(tool.verify_bundle(self.target)["ready"])

    def test_unknown_virtual_table_is_rejected_without_touching_source(self):
        with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
            db.execute("CREATE VIRTUAL TABLE chat_history_fts USING fts5(message)")
            db.commit()
        before = self.hashes()
        with self.assertRaisesRegex(tool.MigrationError, "Virtual tables"):
            self.copy()
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.target / "READY.json").exists())

    def test_scan_permissions_fail_closed_instead_of_omitting_a_directory(self):
        original = tool.os.scandir

        def denied(path):
            if Path(path).name == "web_cache":
                raise PermissionError("synthetic unreadable cache")
            return original(path)

        with patch.object(tool.os, "scandir", side_effect=denied):
            with self.assertRaises(PermissionError):
                self.copy()
        self.assertFalse(self.target.exists())

    def test_invalid_manifest_and_timeout_are_rejected_without_unsafe_paths(self):
        self.copy()
        ready = self.target / "READY.json"
        original = ready.read_text()
        ready.write_text("[]")
        with self.assertRaises(tool.MigrationError):
            tool.verify_bundle(self.target)
        manifest = json.loads(original)
        manifest["database_name"] = "../../outside.db"
        ready.write_text(json.dumps(manifest))
        with self.assertRaises(tool.MigrationError):
            tool.verify_bundle(self.target)
        ready.write_text(original)
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(tool.MigrationError):
                tool.verify_bundle(self.target, timeout)


if __name__ == "__main__":
    unittest.main()
