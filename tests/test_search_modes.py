"""Search modes must preserve literal callers, filters and archive contents."""

import pytest
from fastapi.testclient import TestClient

import db_config
from web import server


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Provide an isolated archive and release every connection after use."""
    monkeypatch.setattr(db_config, "DB_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setattr(db_config, "_POOL", None)
    monkeypatch.setattr(db_config, "_FTS_READY", False)
    monkeypatch.setattr(server, "API_KEY", "synthetic-search-key")
    monkeypatch.setattr(server.app.state, "schema_ready", True, raising=False)
    monkeypatch.setattr(server, "load_right_align_ids", lambda: frozenset())
    db_config.init_db()
    try:
        with db_config.get_db_connection() as db:
            yield db
    finally:
        if db_config._POOL is not None:
            db_config._POOL.close_all()


@pytest.mark.parametrize("fts_enabled", [False, True])
@pytest.mark.parametrize(
    "keyword,matching,unrelated",
    [
        ("手机 充电", "先充电，再用手机", "手机坏了"),
        ("手机\t\n充电", "手机\n需要充电", "手机收到消息"),
        ("手机　充电", "给手机充电", "给电脑充电"),
        ('"手机 充电"', "说的是手机 充电", "给手机充电"),
        ('"server error" 修复', "已经修复 SERVER ERROR", "server 待修复 error"),
        ("服务器 错误", "错误来自服务器", "只有服务器"),
        ("服务器 错误", "prefix\x00错误来自服务器", "prefix\x00只有服务器"),
        ("café", "CAFÉ", "CAFE"),
        ("é", "CAFÉ", "CAFE"),
        ("привет", "ПРИВЕТ", "other message"),
        ("ΟΣ", "ΟΣΑ", "other message"),
        ("50% a_b", "a_b 进度50%", "aXb 进度500"),
        ("don't", "Please don't stop", "Please do stop"),
        ('{"key":"value"}', 'JSON: {"key":"value"}', "key value"),
        ('"unfinished phrase', 'text "unfinished phrase here', "unfinished phrase"),
        ('""', 'empty quotes ""', "empty quotes"),
        ("AND OR", "OR AND", "OR only"),
        ("foo\x00bar 图片", "图片 prefix foo\x00bar suffix", "图片 foo bar"),
        ("图片", "prefix\x00图片suffix", "prefix other suffix"),
        ('" OR 1=1 --', 'payload " OR 1=1 --', "unrelated"),
        ("[CQ:image,file=abc.jpg]", "[CQ:image,file=abc.jpg]", "abc.jpg"),
        (
            "https://example.invalid/a_b",
            "https://example.invalid/a_b",
            "https://example.invalid/aXb",
        ),
    ],
)
def test_terms_match_without_losing_literal_symbols(
    archive, monkeypatch, fts_enabled, keyword, matching, unrelated
):
    monkeypatch.setattr(db_config, "_FTS_READY", fts_enabled)
    archive.executemany(
        "INSERT INTO chat_history(message, msg_id) VALUES (?, ?)",
        [(matching, "match"), (unrelated, "other")],
    )
    archive.commit()
    conditions, params = [], []
    db_config.add_message_search_condition(
        archive, conditions, params, keyword, search_mode="terms"
    )
    rows = archive.execute(
        "SELECT msg_id FROM chat_history WHERE " + " AND ".join(conditions), params
    ).fetchall()
    assert [row["msg_id"] for row in rows] == ["match"]
    assert archive.execute(
        "SELECT message FROM chat_history ORDER BY id"
    ).fetchall() == [{"message": matching}, {"message": unrelated}]


def test_long_terms_keep_index_and_short_terms_still_filter(archive):
    conditions, params = [], []
    assert db_config.add_message_search_condition(
        archive, conditions, params, "服务器 错误", search_mode="terms"
    )
    assert params == ['"服务器"', "服务器", "错误"]
    assert "MATCH ?" in conditions[0]
    plan = archive.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM chat_history WHERE "
        + " AND ".join(conditions),
        params,
    ).fetchall()
    assert any("VIRTUAL TABLE INDEX" in row["detail"] for row in plan)
    assert any("idx_chat_history_nul" in row["detail"] for row in plan)


def test_legacy_python_api_retains_contiguous_matching(archive):
    archive.executemany(
        "INSERT INTO chat_history(message, msg_id) VALUES (?, ?)",
        [("手机 充电", "literal"), ("给手机充电", "separate")],
    )
    archive.commit()
    rows = db_config.DatabaseManager.get_history(keyword="手机 充电")
    assert [row["msg_id"] for row in rows] == ["literal"]


def test_http_modes_keep_filters_totals_cursor_and_schema(archive):
    archive.executemany(
        "INSERT INTO chat_history(message, session_id, user_id, timestamp) "
        "VALUES (?, ?, ?, ?)",
        [
            ("服务器 错误", "selected", "user-a", 100),
            ("错误来自服务器", "selected", "user-a", 200),
            ("服务器出了错误", "selected", "user-a", 300),
            ("服务器 错误", "other", "user-a", 200),
            ("服务器 错误", "selected", "user-b", 200),
            ("服务器 错误", "selected", "user-a", 900),
        ],
    )
    archive.commit()
    schema_before = archive.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    headers = {"X-API-Key": "synthetic-search-key"}
    params = {
        "keyword": "服务器 错误",
        "session_id": "selected",
        "user_id": "user-a",
        "time_start": 100,
        "time_end": 300,
        "include_total": True,
        "limit": 2,
    }
    with TestClient(server.app) as client:
        original = client.get("/api/history", params=params, headers=headers).json()
        assert original["success"]
        assert original["total"] == 1
        assert [row["id"] for row in original["data"]] == [1]
        explicit = client.get(
            "/api/history", params={**params, "search_mode": "literal"}, headers=headers
        ).json()
        assert explicit["data"] == original["data"]
        terms = client.get(
            "/api/history", params={**params, "search_mode": "terms"}, headers=headers
        ).json()
        assert terms["success"]
        assert terms["total"] == 3
        assert terms["total_exact"] is True
        assert terms["has_more"] is True
        assert [row["id"] for row in terms["data"]] == [3, 2]
        following = client.get(
            "/api/history",
            params={**params, "search_mode": "terms", "cursor": terms["next_cursor"]},
            headers=headers,
        ).json()
        assert [row["id"] for row in following["data"]] == [1]
        assert following["has_more"] is False
        assert following["total"] == 3
        quoted = client.get(
            "/api/history",
            params={**params, "search_mode": "terms", "keyword": '"服务器 错误"'},
            headers=headers,
        ).json()
        assert [row["id"] for row in quoted["data"]] == [1]
        assert (
            client.get(
                "/api/history", params={"search_mode": "unknown"}, headers=headers
            ).status_code
            == 422
        )
    assert (
        archive.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        == schema_before
    )
