"""History totals must describe every active filter on isolated archives."""

import pytest
from fastapi.testclient import TestClient

import db_config
from web import server


@pytest.fixture
def history_client(tmp_path, monkeypatch):
    """Provide a synthetic archive without opening the production database."""
    old_pool = db_config._POOL
    monkeypatch.setattr(db_config, "DB_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setattr(db_config, "_POOL", None)
    monkeypatch.setattr(db_config, "_FTS_READY", False)
    monkeypatch.setattr(server, "API_KEY", "synthetic-test-key")
    monkeypatch.setattr(server.app.state, "schema_ready", True, raising=False)
    monkeypatch.setattr(server, "load_right_align_ids", lambda: frozenset())
    db_config.init_db()
    with db_config.get_db_connection() as db:
        db.executemany(
            "INSERT INTO chat_history (user_id, sender_name, message, timestamp, "
            "session_id, message_type, session_name, msg_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "user-a",
                    "Sender",
                    "Synthetic text",
                    1700000000,
                    "session-a",
                    "GroupMessage",
                    "Session",
                    f"msg-{item}",
                )
                for item in range(3)
            ],
        )
        db.commit()
        record_id = db.execute("SELECT MIN(id) AS id FROM chat_history").fetchone()[
            "id"
        ]
    try:
        with TestClient(server.app) as client:
            yield client, record_id
    finally:
        if db_config._POOL is not None and db_config._POOL is not old_pool:
            db_config._POOL.close_all()


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("include_total", [False, True])
def test_record_filter_does_not_reuse_whole_session_total(
    history_client, missing, include_total
):
    client, record_id = history_client
    response = client.get(
        "/api/history",
        params={
            "session_id": "session-a",
            "record_id": record_id + 100 if missing else record_id,
            "include_total": include_total,
            "limit": 1,
        },
        headers={"X-API-Key": "synthetic-test-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["data"]) == (0 if missing else 1)
    assert payload["total"] == ((0 if missing else 1) if include_total else None)
    assert payload["total_exact"] is include_total


def test_unfiltered_session_preserves_exact_materialized_total(history_client):
    client, _ = history_client
    response = client.get(
        "/api/history",
        params={"session_id": "session-a", "limit": 1},
        headers={"X-API-Key": "synthetic-test-key"},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert response.json()["total_exact"] is True
