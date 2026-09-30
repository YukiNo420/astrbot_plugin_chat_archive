"""Read-only reference observations from an unused offline migration bundle."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
from urllib.parse import unquote, urlsplit

try:
    from .archive_migrate import MigrationError, verify_bundle
except ImportError:
    from archive_migrate import MigrationError, verify_bundle

SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
MAX_FIELD = 4 * 1024 * 1024
MAX_NODES = 20000
MAX_DEPTH = 24


class ReferenceReader:
    def __init__(self):
        self.warnings = set()
        self.nodes = 0

    def names(self, value, depth=0):
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            self.warnings.add("reference_structure_limit")
            return set()
        names = set()
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in {"filename", "file_name", "file", "name"}
                    and isinstance(item, str)
                    and "/" not in item
                ):
                    self.warnings.add("filename_only_metadata_is_ambiguous")
                names.update(self.names(item, depth + 1))
            return names
        if isinstance(value, list):
            for item in value:
                names.update(self.names(item, depth + 1))
            return names
        if isinstance(value, bytes):
            self.warnings.add("opaque_reference_field")
            return names
        if not isinstance(value, str):
            return names
        if len(value.encode()) > MAX_FIELD:
            self.warnings.add("reference_field_limit")
            return names
        # Parse real JSON objects/arrays; a filename field alone is not a local URL.
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except (ValueError, RecursionError):
                parsed = None
                if stripped.startswith(("{", "[{", '["')):
                    self.warnings.add("unparsed_structured_message")
            if isinstance(parsed, (dict, list)):
                names.update(self.names(parsed, depth + 1))
        variants = [value]
        for _ in range(3):
            decoded = html.unescape(variants[-1])
            if decoded == variants[-1]:
                break
            variants.append(decoded)
        if html.unescape(variants[-1]) != variants[-1]:
            self.warnings.add("reference_encoding_limit")
        for text in variants:
            for match in re.finditer(r"/static/cache/([^\s\"\'<>\[\],()]+)", text):
                tail = match.group(1)
                name = unquote(urlsplit("/" + tail).path[1:])
                if SAFE_NAME.fullmatch(name) and name not in {".", ".."}:
                    names.add(name)
                else:
                    self.warnings.add("unsupported_local_cache_path")
        # CQ JSON is explicitly encoded and must be decoded before parsing nested nodes.
        for payload in re.findall(r"\[CQ:json,data=([^\]]*)\]", value):
            decoded = (
                payload.replace("&#44;", ",")
                .replace("&#91;", "[")
                .replace("&#93;", "]")
                .replace("&amp;", "&")
            )
            try:
                parsed = json.loads(decoded)
            except (ValueError, RecursionError):
                self.warnings.add("unparsed_cq_json")
            else:
                names.update(self.names(parsed, depth + 1))
        for attributes in re.findall(
            r"\[CQ:(?:image|video|record|file),([^\]]*)\]", value
        ):
            if "/static/cache/" not in attributes and not re.search(
                r"(?:^|,)url=https?://", attributes
            ):
                self.warnings.add("unresolved_media_location")
        return names


def _scan_database(path, observe, deadline):
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        db.execute("PRAGMA trusted_schema=OFF")
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "chat_history" not in tables:
            raise MigrationError("Bundle does not contain a supported archive schema")
        if "message" not in {
            row[1] for row in db.execute("PRAGMA table_info(chat_history)")
        }:
            raise MigrationError("Archive message column is missing")
        for row in db.execute("SELECT * FROM chat_history"):
            observe("active", dict(row), isinstance(row["message"], (str, type(None))))
        if "archive_trash" in tables:
            if "row_json" not in {
                row[1] for row in db.execute("PRAGMA table_info(archive_trash)")
            }:
                raise MigrationError("Trash payload column is missing")
            for (payload,) in db.execute("SELECT row_json FROM archive_trash"):
                try:
                    row = json.loads(payload)
                    supported = (
                        isinstance(row, dict)
                        and isinstance(row.get("message"), (str, type(None)))
                        and "message" in row
                    )
                except (ValueError, TypeError, RecursionError):
                    row, supported = None, False
                observe("trash", row if supported else None, supported)
        known = {
            "chat_history",
            "archive_trash",
            "session_stats",
            "archive_management_operations",
            "sqlite_sequence",
            "sqlite_stat1",
            "sqlite_stat4",
        }
        if tables - known:
            observe("unrecognized_database_tables", None, False)
        if "session_stats" in tables:
            for row in db.execute("SELECT * FROM session_stats"):
                observe("derived_stats", dict(row))


def preview_bundle(bundle, timeout=300):
    if not math.isfinite(timeout) or timeout <= 0:
        raise MigrationError("Timeout must be positive and finite")
    deadline = time.monotonic() + timeout
    root = Path(os.path.abspath(Path(bundle).expanduser()))
    verify_bundle(root, timeout)
    ready_bytes = (root / "READY.json").read_bytes()
    manifest = json.loads(ready_bytes)
    data = root / "data"
    cache = data / "web_cache"
    if not cache.is_dir():
        raise MigrationError("Bundle does not contain a controlled web_cache directory")
    warnings, references, counts = Counter(), {}, Counter()

    def observe(category, value, supported=True):
        if time.monotonic() > deadline:
            raise MigrationError("Reference preview timed out")
        counts[category] += 1
        if not supported:
            warnings["unparsed_" + category + "_record"] += 1
            return
        reader = ReferenceReader()
        for name in reader.names(value):
            references.setdefault(name, Counter())[category] += 1
        warnings.update(reader.warnings)

    # Open only a disposable main-file copy. Never let SQLite create sidecars in the bundle.
    name = manifest["database_name"]
    if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
        raise MigrationError("Invalid database filename")
    with tempfile.TemporaryDirectory(prefix="archive-reference-preview-") as tmp:
        database = Path(tmp).resolve() / "database.db"
        fd = os.open(data / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb") as source, database.open("xb") as target:
            while block := source.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise MigrationError("Reference preview timed out")
                digest.update(block)
                target.write(block)
        if digest.hexdigest() != manifest["data"][name]["sha256"]:
            raise MigrationError("Database changed before reference scanning")
        _scan_database(database, observe, deadline)
    queue = data / "chat_archive_failed_writes.jsonl"
    if queue.exists():
        with queue.open(encoding="utf-8") as stream:
            while line := stream.readline(MAX_FIELD + 1):
                if len(line.encode()) > MAX_FIELD:
                    observe("failed_writes", None, False)
                    warnings["failed_queue_record_limit"] += 1
                    while not line.endswith("\n"):
                        if time.monotonic() > deadline:
                            raise MigrationError("Reference preview timed out")
                        line = stream.readline(MAX_FIELD + 1)
                        if not line:
                            break
                    continue
                try:
                    item = json.loads(line)
                    record = item.get("record") if isinstance(item, dict) else None
                    supported = (
                        isinstance(record, list)
                        and len(record) in {8, 12}
                        and isinstance(record[2], str)
                    )
                except (ValueError, RecursionError):
                    record, supported = None, False
                observe("failed_writes", record if supported else None, supported)
    known = {name, "chat_archive_failed_writes.jsonl", "web_cache"}
    for entry in data.iterdir():
        if entry.name not in known:
            warnings["additional_state_entries_not_scanned"] += 1
    files, unobserved_bytes = [], 0
    for entry in sorted(cache.iterdir()):
        info = entry.lstat()
        refs = references.get(entry.name, {})
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not SAFE_NAME.fullmatch(entry.name)
            or entry.name in {".", ".."}
        ):
            state = "held_unsupported"
            warnings["unsupported_cache_entry"] += 1
        elif entry.name.endswith(".tmp"):
            state = "held_temporary"
        elif refs:
            state = "reference_observed"
        else:
            state = "no_reference_observed_in_supported_snapshot_fields"
            unobserved_bytes += info.st_size
        files.append(
            {
                "name": entry.name,
                "bytes": info.st_size if stat.S_ISREG(info.st_mode) else 0,
                "state": state,
                "referencing_records": dict(refs),
            }
        )
    # New references, cache changes or sidecars invalidate this observation; nothing is moved.
    verify_bundle(root, max(0.001, deadline - time.monotonic()))
    if (root / "READY.json").read_bytes() != ready_bytes:
        raise MigrationError("Bundle manifest changed during reference scanning")
    return {
        "mode": "read_only_preview",
        "files": files,
        "scanned_records": dict(counts),
        "warnings": dict(warnings),
        "supported_snapshot_fields_scanned_without_warnings": not warnings,
        "no_reference_observed_bytes": unobserved_bytes,
        "in_memory_queue_or_downloads_checked": False,
        "reclamation_authorized": False,
        "delete_or_quarantine_enabled": False,
        "sqlite_file_shrink_promised": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundle", help="Completed, unused offline bundle from archive_migrate.py"
    )
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                preview_bundle(args.bundle, args.timeout_seconds),
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0
    except MigrationError as exc:
        print(
            json.dumps(
                {
                    "mode": "read_only_preview",
                    "error": str(exc),
                    "reclamation_authorized": False,
                }
            )
        )
        return 1
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "mode": "read_only_preview",
                    "error": "Preview failed: " + type(exc).__name__,
                    "reclamation_authorized": False,
                }
            )
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
