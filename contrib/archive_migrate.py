"""Offline archive copying, independent of AstrBot and its startup path selection."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time

RESERVE_BYTES = 64 * 1024 * 1024
FORMAT = "chat-archive-offline-copy-v1"


class MigrationError(RuntimeError):
    pass


def _check_deadline(deadline):
    if time.monotonic() > deadline:
        raise MigrationError("Operation timed out; source is preserved")


def _deadline(timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise MigrationError("Timeout must be positive and finite")
    return time.monotonic() + timeout


def _plain_path(value):
    if os.name != "posix":
        raise MigrationError(
            "This tool currently supports local POSIX filesystems only"
        )
    path = Path(os.path.abspath(Path(value).expanduser()))
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise MigrationError("Symbolic links in paths are not supported")
    return path


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_file(path, deadline, destination=None):
    _plain_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise MigrationError("Only regular, non-hardlinked source files are supported")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise MigrationError("Source changed while opening a file")
        output = None
        try:
            if destination is not None:
                out_fd = os.open(
                    destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                output = os.fdopen(out_fd, "wb")
            while block := stream.read(1024 * 1024):
                _check_deadline(deadline)
                digest.update(block)
                if output is not None:
                    output.write(block)
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise MigrationError("Source changed while reading a file")
        finally:
            if output is not None:
                output.close()
    if _identity(path.lstat()) != _identity(before):
        raise MigrationError("Source was replaced while reading a file")
    if destination is not None:
        os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
    return {
        "bytes": before.st_size,
        "sha256": digest.hexdigest(),
        "identity": _identity(before),
    }


def _inventory(root, deadline):
    result = {}

    def visit(path, depth=0):
        _check_deadline(deadline)
        _plain_path(path)
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            result[relative] = {"directory": True, "identity": _identity(info)}
            if depth > 100:
                raise MigrationError("Directory nesting exceeds the supported limit")
            with os.scandir(path) as entries:
                children = sorted(entry.name for entry in entries)
            for child in children:
                visit(path / child, depth + 1)
        elif stat.S_ISREG(info.st_mode):
            result[relative] = _read_file(path, deadline)
        else:
            raise MigrationError("Symlinks and special files are not supported")

    visit(root)
    return result


def _paths(source, target, database_name):
    source, target = _plain_path(source), _plain_path(target)
    if not source.is_dir() or source == Path(source.anchor):
        raise MigrationError(
            "Source must be an existing data directory, not a filesystem root"
        )
    if source == target or source in target.parents or target in source.parents:
        raise MigrationError(
            "Source and target must be separate, non-nested directories"
        )
    if target.exists():
        raise MigrationError("Target already exists; nothing will be overwritten")
    if not target.parent.is_dir():
        raise MigrationError(
            "Create the target parent explicitly before running this tool"
        )
    if (
        not database_name
        or Path(database_name).name != database_name
        or database_name in {".", ".."}
    ):
        raise MigrationError(
            "Database name must be a filename within the source directory"
        )
    if not (source / database_name).is_file():
        raise MigrationError(
            "Selected database is missing; external custom databases are not copied"
        )
    return source, target


def preview(source, target, database_name="chat_history.db", timeout=300):
    source, target = _paths(source, target, database_name)
    inventory = _inventory(source, _deadline(timeout))
    source_bytes = sum(item.get("bytes", 0) for item in inventory.values())
    required = 3 * source_bytes + RESERVE_BYTES
    return {
        "mode": "preview",
        "source": str(source),
        "target_bundle": str(target),
        "database_name": database_name,
        "source_files": sum(not item.get("directory") for item in inventory.values()),
        "source_bytes": source_bytes,
        "estimated_required_free_bytes": required,
        "available_free_bytes": shutil.disk_usage(target.parent).free,
        "all_writers_must_be_stopped": True,
        "changes_made": False,
    }


def _copy_tree(source, target, inventory, deadline, skip=()):
    target.mkdir(mode=0o700)
    for relative, item in inventory.items():
        if relative == "." or relative in skip:
            continue
        output = target / relative
        if item.get("directory"):
            output.mkdir(mode=0o700)
        else:
            copied = _read_file(source / relative, deadline, output)
            if copied != item:
                raise MigrationError("Source changed during copying")


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _logical_database(connection, deadline):
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    connection.execute("PRAGMA trusted_schema=OFF")
    check = connection.execute("PRAGMA integrity_check").fetchall()
    if check != [("ok",)]:
        raise MigrationError("SQLite integrity check failed")
    schema = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    tables = {}
    for kind, name, _, sql in schema:
        if kind != "table":
            continue
        if sql and "CREATE VIRTUAL TABLE" in sql.upper():
            raise MigrationError("Virtual tables need a separate migration procedure")
        columns = connection.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
        ordering = ",".join(_quote(row[1]) for row in columns)
        digest, count = hashlib.sha256(), 0
        for row in connection.execute(
            f"SELECT * FROM {_quote(name)} ORDER BY {ordering}"
        ):
            _check_deadline(deadline)
            values = [
                ("blob", value.hex())
                if isinstance(value, bytes)
                else (type(value).__name__, value)
                for value in row
            ]
            digest.update(
                json.dumps(values, ensure_ascii=True, allow_nan=False).encode() + b"\n"
            )
            count += 1
        tables[name] = {"rows": count, "sha256": digest.hexdigest()}
    return {
        "schema_sha256": hashlib.sha256(
            json.dumps(schema, ensure_ascii=True).encode()
        ).hexdigest(),
        "tables": tables,
    }


def _normalize_database(snapshot, data, work, database_name, deadline):
    work.mkdir(mode=0o700)
    for suffix in ("", "-wal", "-shm", "-journal"):
        original = snapshot / (database_name + suffix)
        if original.exists():
            _read_file(original, deadline, work / original.name)
    normalized = data / database_name
    fd = os.open(normalized, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(
        sqlite3.connect(
            (work / database_name).as_uri() + "?mode=ro", uri=True, timeout=1
        )
    ) as source_db:
        expected = _logical_database(source_db, deadline)
        with closing(sqlite3.connect(normalized, timeout=1)) as target_db:
            source_db.backup(
                target_db,
                pages=64,
                progress=lambda *_: _check_deadline(deadline),
                sleep=0.01,
            )
            target_db.execute("PRAGMA journal_mode=DELETE")
            actual = _logical_database(target_db, deadline)
    if expected != actual:
        raise MigrationError("Normalized database does not match the snapshot")
    with normalized.open("rb") as stream:
        os.fsync(stream.fileno())
    return actual


def _public_inventory(inventory):
    return {
        name: {key: value for key, value in item.items() if key != "identity"}
        for name, item in inventory.items()
    }


def _write_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_archive(
    source,
    target,
    *,
    confirm_stopped=False,
    database_name="chat_history.db",
    timeout=300,
    quiet_seconds=1,
):
    if confirm_stopped is not True:
        raise MigrationError(
            "Stop all archive, media and Web writers, drain queues, then confirm explicitly"
        )
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or not math.isfinite(quiet_seconds)
        or quiet_seconds < 0
    ):
        raise MigrationError("Timeout and quiet window are invalid")
    source, target = _paths(source, target, database_name)
    deadline = _deadline(timeout)
    inventory = _inventory(source, deadline)
    required = (
        3 * sum(item.get("bytes", 0) for item in inventory.values()) + RESERVE_BYTES
    )
    if shutil.disk_usage(target.parent).free < required:
        raise MigrationError("Insufficient free space; no target was created")
    # Exclusive reservation prevents two copies or an existing target from being overwritten.
    target.mkdir(mode=0o700)
    _write_json(
        target / "INCOMPLETE.json", {"format": FORMAT, "use_for_cutover": False}
    )
    _fsync_directory(target)
    try:
        snapshot, data, work = target / "snapshot", target / "data", target / "work"
        _copy_tree(source, snapshot, inventory, deadline)
        if _inventory(source, deadline) != inventory:
            raise MigrationError("Source changed; all writers must remain stopped")
        skip = {database_name + suffix for suffix in ("", "-wal", "-shm", "-journal")}
        _copy_tree(snapshot, data, _inventory(snapshot, deadline), deadline, skip)
        database = _normalize_database(snapshot, data, work, database_name, deadline)
        shutil.rmtree(work)
        # A quiet window catches accidental writers; it does not establish that writers are stopped.
        time.sleep(quiet_seconds)
        if _inventory(source, deadline) != inventory:
            raise MigrationError(
                "Source changed before completion; target is not ready"
            )
        snapshot_inventory = _public_inventory(_inventory(snapshot, deadline))
        if snapshot_inventory != _public_inventory(inventory):
            raise MigrationError("Raw snapshot does not match the preserved source")
        manifest = {
            "format": FORMAT,
            "source": str(source),
            "database_name": database_name,
            "snapshot": snapshot_inventory,
            "data": _public_inventory(_inventory(data, deadline)),
            "database": database,
            "automatic_cutover": False,
            "rollback_requires_no_new_writes": True,
        }
        for tree in (snapshot, data):
            for directory in sorted(
                (p for p in tree.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                _fsync_directory(directory)
            _fsync_directory(tree)
        _write_json(target / "READY.json", manifest)
        (target / "INCOMPLETE.json").unlink()
        _fsync_directory(target)
        _fsync_directory(target.parent)
        return {
            "mode": "copied",
            "target_bundle": str(target),
            "data_directory": str(data),
            "ready": True,
            "automatic_cutover": False,
        }
    except BaseException:
        # Keep partial output for inspection. Never delete or move any source entry.
        if not (target / "INCOMPLETE.json").exists():
            _write_json(
                target / "INCOMPLETE.json", {"format": FORMAT, "use_for_cutover": False}
            )
        raise


def verify_bundle(bundle, timeout=300):
    root = _plain_path(bundle)
    if (root / "INCOMPLETE.json").exists() or not (root / "READY.json").is_file():
        raise MigrationError("Bundle is incomplete; do not use it for cutover")
    # Reject root-level links or unexpected files before opening the manifest/database.
    if {entry.name for entry in root.iterdir()} != {"READY.json", "snapshot", "data"}:
        raise MigrationError("Bundle contains unexpected entries")
    for entry in root.iterdir():
        if entry.is_symlink():
            raise MigrationError("Bundle links are not supported")
    manifest = json.loads((root / "READY.json").read_text())
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise MigrationError("Unsupported manifest format")
    deadline = _deadline(timeout)
    for name in ("snapshot", "data"):
        if _public_inventory(_inventory(root / name, deadline)) != manifest[name]:
            raise MigrationError(
                "Bundle has changed; do not switch or assume rollback is safe"
            )
    name = manifest["database_name"]
    if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
        raise MigrationError("Invalid database filename in manifest")
    with closing(
        sqlite3.connect((root / "data" / name).as_uri() + "?mode=ro", uri=True)
    ) as db:
        if _logical_database(db, deadline) != manifest["database"]:
            raise MigrationError("Database validation failed")
    return {"mode": "verified", "ready": True, "automatic_cutover": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?")
    parser.add_argument("target", nargs="?")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Copy into a new bundle; does not switch paths",
    )
    parser.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="Attest all writers stopped and queues drained",
    )
    parser.add_argument("--database-name", default="chat_history.db")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument(
        "--verify",
        metavar="BUNDLE",
        help="Read-only verification of an unused completed bundle",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.verify and (
        args.source or args.target or args.apply or args.confirm_stopped
    ):
        parser.error("--verify cannot be combined with a copy request")
    if not args.verify and (not args.source or not args.target):
        parser.error("source and target are required")
    try:
        if args.verify:
            result = verify_bundle(args.verify, args.timeout_seconds)
        elif args.apply:
            result = copy_archive(
                args.source,
                args.target,
                confirm_stopped=args.confirm_stopped,
                database_name=args.database_name,
                timeout=args.timeout_seconds,
            )
        else:
            result = preview(
                args.source, args.target, args.database_name, args.timeout_seconds
            )
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (
        MigrationError,
        OSError,
        sqlite3.Error,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(
            json.dumps(
                {"ready": False, "error": str(exc), "source_preserved": True},
                ensure_ascii=True,
            )
        )
        return 1
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "ready": False,
                    "error": "Interrupted; do not use incomplete target",
                    "source_preserved": True,
                }
            )
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
