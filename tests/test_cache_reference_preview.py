"""Preview-only cache tests; every source, queue and attachment is synthetic."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contrib import archive_migrate
from contrib import cache_reference_preview as tool


class CachePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.bundle = self.root / "bundle"
        self.cache = self.source / "web_cache"
        self.cache.mkdir()
        for name in (
            "active.png",
            "nested.png",
            "shared.png",
            "trash.mp4",
            "queued.ogg",
            "maybe.png",
            "download.tmp",
        ):
            (self.cache / name).write_bytes(b"synthetic attachment")
        with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
            db.execute(
                "CREATE TABLE chat_history(id INTEGER PRIMARY KEY, message TEXT)"
            )
            db.execute(
                "CREATE TABLE archive_trash(original_id INTEGER PRIMARY KEY, row_json TEXT)"
            )
            nested = {
                "nodes": [
                    {
                        "content": [
                            {
                                "type": "image",
                                "data": {"url": "/static/cache/nested.png"},
                            }
                        ]
                    }
                ]
            }
            db.execute(
                "INSERT INTO chat_history VALUES (?,?)",
                [
                    1,
                    "[合并转发]\n    [CQ:image,url=/static/cache/active.png]\n    [CQ:image,url=/static/cache/shared.png]\nsynthetic-body-do-not-print\n[合并转发结束]",
                ],
            )
            db.execute("INSERT INTO chat_history VALUES (?,?)", [2, json.dumps(nested)])
            db.execute(
                "INSERT INTO archive_trash VALUES (?,?)",
                [
                    9,
                    json.dumps(
                        {
                            "message": "[CQ:video,url=/static/cache/trash.mp4][CQ:image,url=/static/cache/shared.png]"
                        }
                    ),
                ],
            )
            db.commit()
        record = [
            "synthetic",
            "sender",
            "[CQ:record,url=/static/cache/queued.ogg]",
            100,
            "qq:group:42",
            "GroupMessage",
            "synthetic",
            "1",
        ]
        (self.source / "chat_archive_failed_writes.jsonl").write_text(
            json.dumps({"record": record}) + "\n"
        )

    def tearDown(self):
        self.temp.cleanup()

    def make_bundle(self):
        archive_migrate.copy_archive(
            self.source, self.bundle, confirm_stopped=True, quiet_seconds=0
        )

    def hashes(self, root):
        return {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")
            if p.is_file()
        }

    def test_active_trash_nested_and_failed_queue_observations_preserve_all_files(self):
        source_before = self.hashes(self.source)
        self.make_bundle()
        before = self.hashes(self.bundle)
        result = tool.preview_bundle(self.bundle)
        files = {item["name"]: item for item in result["files"]}
        self.assertEqual(files["active.png"]["referencing_records"], {"active": 1})
        self.assertEqual(files["nested.png"]["referencing_records"], {"active": 1})
        self.assertEqual(
            files["shared.png"]["referencing_records"], {"active": 1, "trash": 1}
        )
        self.assertEqual(files["trash.mp4"]["referencing_records"], {"trash": 1})
        self.assertEqual(
            files["queued.ogg"]["referencing_records"], {"failed_writes": 1}
        )
        self.assertEqual(
            files["maybe.png"]["state"],
            "no_reference_observed_in_supported_snapshot_fields",
        )
        self.assertEqual(files["download.tmp"]["state"], "held_temporary")
        self.assertFalse(result["reclamation_authorized"])
        self.assertFalse(result["delete_or_quarantine_enabled"])
        self.assertFalse(result["in_memory_queue_or_downloads_checked"])
        self.assertEqual(result["warnings"], {})
        self.assertEqual(before, self.hashes(self.bundle))
        self.assertEqual(source_before, self.hashes(self.source))

    def test_cq_json_double_escaping_encoded_urls_and_filename_only_metadata(self):
        payload = json.dumps(
            {"nodes": [{"content": "[CQ:image,url=/static/cache/nested.png]"}]}
        )
        escaped = (
            payload.replace("&", "&amp;")
            .replace("[", "&#91;")
            .replace("]", "&#93;")
            .replace(",", "&#44;")
        )
        with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
            db.execute(
                "INSERT INTO chat_history VALUES (?,?)",
                [3, "[CQ:json,data=" + escaped + "]"],
            )
            db.execute(
                "INSERT INTO chat_history VALUES (?,?)",
                [4, "[CQ:image,url=/static/cache/%61ctive.png?x=1&amp;amp;y=2]"],
            )
            db.execute(
                "INSERT INTO chat_history VALUES (?,?)",
                [5, json.dumps({"filename": "maybe.png"})],
            )
            db.commit()
        self.make_bundle()
        result = tool.preview_bundle(self.bundle)
        files = {item["name"]: item for item in result["files"]}
        self.assertEqual(files["nested.png"]["referencing_records"]["active"], 2)
        self.assertEqual(files["active.png"]["referencing_records"]["active"], 2)
        self.assertEqual(files["maybe.png"]["referencing_records"], {})
        self.assertIn("filename_only_metadata_is_ambiguous", result["warnings"])
        self.assertFalse(result["supported_snapshot_fields_scanned_without_warnings"])

    def test_unparsed_records_and_additional_state_are_explicit_uncertainty(self):
        with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
            db.execute("INSERT INTO archive_trash VALUES (?,?)", [10, "{broken"])
            db.execute(
                "INSERT INTO chat_history VALUES (?,?)", [3, "[CQ:json,data={broken]"]
            )
            db.execute(
                "INSERT INTO chat_history VALUES (?,?)",
                [4, "[CQ:image,file=maybe.png]"],
            )
            db.commit()
        with (self.source / "chat_archive_failed_writes.jsonl").open("a") as stream:
            stream.write('{"unsupported":"maybe.png"}\n{broken\n')
        (self.source / "other-state.json").write_text('{"filename":"maybe.png"}')
        self.make_bundle()
        result = tool.preview_bundle(self.bundle)
        for warning in (
            "unparsed_trash_record",
            "unparsed_failed_writes_record",
            "unparsed_cq_json",
            "unresolved_media_location",
            "additional_state_entries_not_scanned",
        ):
            self.assertIn(warning, result["warnings"])
        self.assertFalse(result["reclamation_authorized"])

    def test_paths_links_nested_cache_and_incomplete_bundle_fail_closed(self):
        self.make_bundle()
        link = self.root / "link"
        link.symlink_to(self.bundle, target_is_directory=True)
        with self.assertRaises(archive_migrate.MigrationError):
            tool.preview_bundle(link)
        (self.bundle / "data/web_cache/linked.png").symlink_to(
            self.cache / "active.png"
        )
        with self.assertRaises(archive_migrate.MigrationError):
            tool.preview_bundle(self.bundle)
        reader = tool.ReferenceReader()
        self.assertEqual(
            reader.names("/static/cache/../outside.png /static/cache/%2Foutside.png"),
            set(),
        )
        self.assertIn("unsupported_local_cache_path", reader.warnings)
        (self.bundle / "data/web_cache/linked.png").unlink()
        (self.bundle / "INCOMPLETE.json").write_text("{}")
        with self.assertRaises(archive_migrate.MigrationError):
            tool.preview_bundle(self.bundle)

    def test_new_reference_during_scan_rejects_result_without_moving_cache(self):
        self.make_bundle()
        scanned, written = threading.Event(), threading.Event()
        cache_before = self.hashes(self.bundle / "data/web_cache")
        original = tool._scan_database

        def scan_and_wait(*args):
            original(*args)
            scanned.set()
            self.assertTrue(written.wait(3))

        def writer():
            self.assertTrue(scanned.wait(3))
            with closing(sqlite3.connect(self.bundle / "data/chat_history.db")) as db:
                db.execute(
                    "INSERT INTO chat_history VALUES (?,?)",
                    [3, "[CQ:image,url=/static/cache/maybe.png]"],
                )
                db.commit()
            written.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            with patch.object(tool, "_scan_database", side_effect=scan_and_wait):
                with self.assertRaises(archive_migrate.MigrationError):
                    tool.preview_bundle(self.bundle)
            future.result(timeout=3)
        self.assertEqual(cache_before, self.hashes(self.bundle / "data/web_cache"))

    def test_cache_change_and_manifest_change_reject_stale_observations(self):
        self.make_bundle()
        original = tool._scan_database

        def mutate_cache(*args):
            original(*args)
            (self.bundle / "data/web_cache/maybe.png").write_bytes(
                b"new synthetic bytes"
            )

        with patch.object(tool, "_scan_database", side_effect=mutate_cache):
            with self.assertRaises(archive_migrate.MigrationError):
                tool.preview_bundle(self.bundle)
        (self.bundle / "data/web_cache/maybe.png").write_bytes(
            (self.cache / "maybe.png").read_bytes()
        )

        def mutate_manifest(*args):
            original(*args)
            ready = self.bundle / "READY.json"
            ready.write_text(ready.read_text() + "\n")

        with patch.object(tool, "_scan_database", side_effect=mutate_manifest):
            with self.assertRaisesRegex(
                archive_migrate.MigrationError, "manifest changed"
            ):
                tool.preview_bundle(self.bundle)

    def test_cli_has_only_preview_and_never_emits_message_content(self):
        self.make_bundle()
        before = self.hashes(self.bundle)
        result = subprocess.run(
            [sys.executable, tool.__file__, str(self.bundle)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("synthetic-body-do-not-print", result.stdout)
        self.assertEqual(json.loads(result.stdout)["mode"], "read_only_preview")
        refused = subprocess.run(
            [sys.executable, tool.__file__, str(self.bundle), "--apply"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(before, self.hashes(self.bundle))

    def test_resource_limits_opaque_fields_and_unsupported_cache_entries(self):
        with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
            db.execute(
                "INSERT INTO chat_history VALUES (?,?)", [3, b"opaque synthetic"]
            )
            db.execute("INSERT INTO chat_history VALUES (?,?)", [4, "x" * 101])
            db.commit()
        (self.cache / "subdirectory").mkdir()
        (self.cache / "subdirectory/nested.png").write_bytes(b"held")
        self.make_bundle()
        with patch.object(tool, "MAX_FIELD", 100):
            result = tool.preview_bundle(self.bundle)
        self.assertIn("unparsed_active_record", result["warnings"])
        self.assertIn("reference_field_limit", result["warnings"])
        self.assertIn("unsupported_cache_entry", result["warnings"])
        reader = tool.ReferenceReader()
        value = "/static/cache/maybe.png"
        for _ in range(30):
            value = [value]
        self.assertEqual(reader.names(value), set())
        self.assertIn("reference_structure_limit", reader.warnings)

    def test_non_archive_schema_and_error_output_are_safe(self):
        with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
            db.execute(
                "ALTER TABLE chat_history RENAME COLUMN message TO opaque_column"
            )
            db.commit()
        self.make_bundle()
        with self.assertRaises(archive_migrate.MigrationError):
            tool.preview_bundle(self.bundle)
        output = io.StringIO()
        with (
            patch.object(
                tool,
                "preview_bundle",
                side_effect=sqlite3.OperationalError("synthetic-body-do-not-print"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(tool.main([str(self.bundle)]), 1)
        self.assertNotIn("synthetic-body-do-not-print", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["reclamation_authorized"])

    def test_other_string_fields_derived_stats_and_unknown_tables_are_accounted_for(
        self,
    ):
        with closing(sqlite3.connect(self.source / "chat_history.db")) as db:
            db.execute("ALTER TABLE chat_history ADD COLUMN avatar_url TEXT")
            db.execute(
                "UPDATE chat_history SET avatar_url=? WHERE id=2",
                ["/static/cache/active.png"],
            )
            db.execute("CREATE TABLE session_stats(last_msg TEXT)")
            db.execute(
                "INSERT INTO session_stats VALUES (?)", ["/static/cache/shared.png"]
            )
            db.execute("CREATE TABLE future_refs(url TEXT)")
            db.execute(
                "INSERT INTO future_refs VALUES (?)", ["/static/cache/maybe.png"]
            )
            db.commit()
        self.make_bundle()
        result = tool.preview_bundle(self.bundle)
        files = {item["name"]: item for item in result["files"]}
        self.assertEqual(files["active.png"]["referencing_records"]["active"], 2)
        self.assertEqual(files["shared.png"]["referencing_records"]["derived_stats"], 1)
        self.assertIn(
            "unparsed_unrecognized_database_tables_record", result["warnings"]
        )
        self.assertFalse(result["supported_snapshot_fields_scanned_without_warnings"])
        self.assertFalse(result["reclamation_authorized"])


if __name__ == "__main__":
    unittest.main()
