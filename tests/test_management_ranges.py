"""Exercise range management against an isolated archive, never production data."""

import json
import time
from contextlib import contextmanager

import archive_management
import db_config
import pytest
from fastapi.testclient import TestClient
from web import server


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Provide two synthetic sessions with more than one export chunk."""
    monkeypatch.setattr(db_config, "DB_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setattr(db_config, "_POOL", None)
    monkeypatch.setattr(db_config, "_FTS_READY", False)
    db_config.init_db()
    manager = archive_management.ArchiveManager(db_config.get_db_connection, tmp_path)
    with db_config.get_db_connection() as db:
        db.executemany(
            "INSERT INTO chat_history (session_id, user_id, sender_name, message, timestamp, message_type, msg_id) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    session,
                    "user",
                    "Sender",
                    f"Synthetic 雪 {i}",
                    1700000000 + i,
                    "GroupMessage",
                    f"{session}-{i}",
                )
                for session, count in [("session-a", 2505), ("session-b", 8)]
                for i in range(count)
            ],
        )
        db.commit()
    monkeypatch.setattr(server, "_ARCHIVE_MANAGER", manager)
    monkeypatch.setattr(server, "API_KEY", "synthetic-test-key")
    monkeypatch.setattr(server.app.state, "schema_ready", True, raising=False)
    client = TestClient(server.app, headers={"X-API-Key": "synthetic-test-key"})
    yield manager, client
    client.close()
    db_config._POOL.close_all()


@pytest.mark.parametrize(
    "bounds, expected",
    [
        ({}, 2505),
        ({"start_ts": 1700002000}, 505),
        ({"end_ts": 1700000600}, 601),
        ({"start_ts": 1700000500, "end_ts": 1700001600}, 1101),
        ({"start_ts": 1700000500, "end_ts": 1700000500}, 1),
        ({"before_ts": 1700000500}, 500),
    ],
)
def test_preview_and_full_export_respect_all_time_bounds(archive, bounds, expected):
    manager, client = archive
    response = client.post(
        "/api/manage/preview", json={"session_id": "session-a", **bounds}
    )
    assert response.status_code == 200
    preview = response.json()
    assert preview["matched_count"] == expected
    assert preview["count"] == expected
    response = client.post(
        "/api/manage/export", json={"preview_token": preview["preview_token"]}
    )
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assert "attachment" in response.headers["content-disposition"]
    payload = response.json()
    assert payload["message_count"] == expected
    assert len(payload["messages"]) == expected
    assert len({row["id"] for row in payload["messages"]}) == expected
    assert {row["session_id"] for row in payload["messages"]} == {"session-a"}
    assert payload["attachments_included"] is False


def test_invalid_and_empty_ranges(archive):
    _, client = archive
    response = client.post(
        "/api/manage/preview",
        json={"session_id": "session-a", "start_ts": 30, "end_ts": 20},
    )
    assert response.status_code == 400
    assert (
        client.post(
            "/api/manage/preview", json={"session_id": "session-a", "start_ts": -1}
        ).status_code
        == 422
    )
    empty = client.post(
        "/api/manage/preview", json={"session_id": "session-a", "end_ts": 10}
    ).json()
    assert empty["matched_count"] == 0 and empty["preview_token"] is None


def test_export_is_bound_to_owner_expiry_and_preview_watermark(archive):
    manager, _ = archive
    preview = manager.preview("owner", "session-a")
    token = preview["preview_token"]
    with pytest.raises(archive_management.ManagementConflict):
        manager.export("other", token)
    with db_config.get_db_connection() as db:
        db.execute(
            "INSERT INTO chat_history (session_id,user_id,message,timestamp) VALUES (?,?,?,?)",
            ["session-a", "user", "Later arrival", 1700000001],
        )
        db.commit()
    with manager.export("owner", token) as backup:
        assert len(json.load(backup)["messages"]) == 2505
    manager._previews[token]["expires"] = time.time() - 1
    with pytest.raises(archive_management.ManagementConflict):
        manager.export("owner", token)


def test_changed_preview_cannot_export_or_delete(archive):
    manager, _ = archive
    preview = manager.preview("owner", "session-a")
    with db_config.get_db_connection() as db:
        db.execute("UPDATE chat_history SET message = 'Changed' WHERE id = 1")
        db.commit()
    with pytest.raises(archive_management.ManagementConflict):
        manager.export("owner", preview["preview_token"])
    with pytest.raises(archive_management.ManagementConflict):
        manager.delete(
            "owner",
            preview["preview_token"],
            "session-a",
            confirm_count=preview["count"],
        )


def test_range_delete_and_restore_cover_the_entire_range(archive):
    manager, _ = archive
    preview = manager.preview(
        "owner", "session-a", start_ts=1700000500, end_ts=1700001600
    )
    result = manager.delete(
        "owner", preview["preview_token"], "session-a", confirm_count=preview["count"]
    )
    assert result["count"] == 1101
    with db_config.get_db_connection() as db:
        rows = db.execute("SELECT row_json FROM archive_trash").fetchall()
        assert len(rows) == 1101
        assert all(
            1700000500 <= json.loads(row["row_json"])["timestamp"] <= 1700001600
            for row in rows
        )
        assert (
            db.execute(
                "SELECT COUNT(*) AS n FROM chat_history WHERE session_id='session-b'"
            ).fetchone()["n"]
            == 8
        )
    assert manager.restore(result["operation_id"], "session-a")["count"] == 1101
    assert manager.preview("owner", "session-a")["matched_count"] == 2505


def test_export_download_closes_temporary_file(archive, monkeypatch):
    manager, client = archive
    files = []
    original = manager.export

    def capture(*args):
        result = original(*args)
        files.append(result)
        return result

    monkeypatch.setattr(manager, "export", capture)
    preview = client.post(
        "/api/manage/preview", json={"session_id": "session-a"}
    ).json()
    response = client.post(
        "/api/manage/export", json={"preview_token": preview["preview_token"]}
    )
    assert response.status_code == 200
    assert files[0].closed
    assert (
        client.post("/api/manage/export", json={"preview_token": "invalid"}).status_code
        == 409
    )


def test_preview_and_export_require_authentication_and_same_origin(archive):
    _, client = archive
    client.headers.pop("X-API-Key")
    assert (
        client.post("/api/manage/preview", json={"session_id": "session-a"}).status_code
        == 401
    )
    assert (
        client.post("/api/manage/export", json={"preview_token": "invalid"}).status_code
        == 401
    )
    client.headers["X-API-Key"] = "synthetic-test-key"
    assert (
        client.post(
            "/api/manage/preview",
            json={"session_id": "session-a"},
            headers={"Origin": "https://other.invalid"},
        ).status_code
        == 403
    )


def test_http_delete_requires_exact_confirmed_count(archive):
    _, client = archive
    preview = client.post(
        "/api/manage/preview", json={"session_id": "session-a"}
    ).json()
    body = {
        "preview_token": preview["preview_token"],
        "confirm_session_id": "session-a",
    }
    assert client.post("/api/manage/delete", json=body).status_code == 422
    assert (
        client.post(
            "/api/manage/delete", json={**body, "confirm_count": 500}
        ).status_code
        == 400
    )
    body["confirm_count"] = 2505
    response = client.post("/api/manage/delete", json=body)
    assert response.status_code == 200
    assert response.json()["count"] == 2505
    assert client.post("/api/manage/delete", json=body).status_code == 409
    with db_config.get_db_connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) AS n FROM chat_history WHERE session_id='session-a'"
            ).fetchone()["n"]
            == 0
        )
        assert (
            db.execute("SELECT COUNT(*) AS n FROM archive_trash").fetchone()["n"]
            == 2505
        )
        assert (
            db.execute(
                "SELECT message_count FROM user_stats WHERE user_id='user'"
            ).fetchone()["message_count"]
            == 8
        )
    restored = client.post(
        "/api/manage/restore",
        json={
            "session_id": "session-a",
            "operation_id": response.json()["operation_id"],
        },
    )
    assert restored.status_code == 200 and restored.json()["count"] == 2505


def test_permanent_delete_requires_typed_confirmation_for_full_range(archive):
    manager, _ = archive
    preview = manager.preview("owner", "session-a")
    with pytest.raises(ValueError):
        manager.delete(
            "owner",
            preview["preview_token"],
            "session-a",
            delete_mode="permanent",
            confirm_count=2505,
        )
    result = manager.delete(
        "owner",
        preview["preview_token"],
        "session-a",
        delete_mode="permanent",
        confirm_permanent="永久删除",
        confirm_count=2505,
    )
    assert result["count"] == 2505 and result["recoverable"] is False
    with db_config.get_db_connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM chat_history").fetchone()["n"] == 8
        assert (
            db.execute("SELECT COUNT(*) AS n FROM archive_trash").fetchone()["n"] == 0
        )


def test_new_arrivals_are_not_deleted_by_an_older_confirmation(archive):
    manager, _ = archive
    preview = manager.preview("owner", "session-a")
    with db_config.get_db_connection() as db:
        db.execute(
            "INSERT INTO chat_history (session_id,user_id,message,timestamp) VALUES (?,?,?,?)",
            ["session-a", "user", "New arrival", 1700000001],
        )
        db.commit()
    assert (
        manager.delete(
            "owner", preview["preview_token"], "session-a", confirm_count=2505
        )["count"]
        == 2505
    )
    with db_config.get_db_connection() as db:
        rows = db.execute(
            "SELECT message FROM chat_history WHERE session_id='session-a'"
        ).fetchall()
        assert rows == [{"message": "New arrival"}]


def test_changes_beyond_first_chunk_invalidate_confirmation(archive):
    manager, _ = archive
    preview = manager.preview("owner", "session-a")
    with db_config.get_db_connection() as db:
        db.execute(
            "UPDATE chat_history SET message = 'Changed after preview' WHERE id = 2400"
        )
        db.commit()
    with pytest.raises(archive_management.ManagementConflict):
        manager.delete(
            "owner", preview["preview_token"], "session-a", confirm_count=2505
        )
    with db_config.get_db_connection() as db:
        assert (
            db.execute("SELECT COUNT(*) AS n FROM chat_history").fetchone()["n"] == 2513
        )
        assert (
            db.execute("SELECT COUNT(*) AS n FROM archive_trash").fetchone()["n"] == 0
        )


def test_later_chunk_failure_rolls_back_all_deleted_messages(archive, monkeypatch):
    manager, _ = archive
    preview = manager.preview("owner", "session-a")
    original_factory = manager.connection_factory

    @contextmanager
    def failing_factory():
        with original_factory() as db:
            execute = db.execute
            calls = 0

            def fail_later(sql, params=None):
                nonlocal calls
                if sql.startswith("DELETE FROM chat_history WHERE id IN"):
                    calls += 1
                    if calls == 3:
                        raise RuntimeError("Synthetic chunk failure")
                return execute(sql, params)

            monkeypatch.setattr(db, "execute", fail_later)
            yield db

    monkeypatch.setattr(manager, "connection_factory", failing_factory)
    with pytest.raises(RuntimeError, match="Synthetic chunk failure"):
        manager.delete(
            "owner", preview["preview_token"], "session-a", confirm_count=2505
        )
    with db_config.get_db_connection() as db:
        assert (
            db.execute("SELECT COUNT(*) AS n FROM chat_history").fetchone()["n"] == 2513
        )
        assert (
            db.execute("SELECT COUNT(*) AS n FROM archive_trash").fetchone()["n"] == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) AS n FROM archive_management_operations"
            ).fetchone()["n"]
            == 0
        )


def test_late_restore_conflict_rolls_back_earlier_chunks(archive):
    manager, _ = archive
    preview = manager.preview("owner", "session-a")
    result = manager.delete(
        "owner", preview["preview_token"], "session-a", confirm_count=2505
    )
    with db_config.get_db_connection() as db:
        db.execute(
            "INSERT INTO chat_history (id,session_id,user_id,message,timestamp) VALUES (?,?,?,?,?)",
            [2505, "session-b", "user", "Do not overwrite", 1700000001],
        )
        db.commit()
    with pytest.raises(archive_management.ManagementConflict):
        manager.restore(result["operation_id"], "session-a")
    with db_config.get_db_connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) AS n FROM chat_history WHERE session_id='session-a'"
            ).fetchone()["n"]
            == 0
        )
        assert (
            db.execute("SELECT message FROM chat_history WHERE id=2505").fetchone()[
                "message"
            ]
            == "Do not overwrite"
        )
        assert (
            db.execute("SELECT COUNT(*) AS n FROM archive_trash").fetchone()["n"]
            == 2505
        )
