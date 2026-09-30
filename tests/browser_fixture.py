"""Disposable synthetic Web fixture; never opens an existing archive."""

import base64
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parents[1]
data = tempfile.TemporaryDirectory(prefix="codex-synthetic-management-")
path = Path(data.name).resolve()
os.environ.update(
    {
        "ARCHIVE_DATA_DIR": str(path),
        "ARCHIVE_DB_PATH": str(path / "fixture.db"),
        "ARCHIVE_CONFIG_PATH": str(path / "absent-config.json"),
        "ARCHIVE_API_KEY": "synthetic-browser-only",
    }
)
sys.path.insert(0, str(ROOT))
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("synthetic-browser")
sys.modules["astrbot"] = types.ModuleType("astrbot")
sys.modules["astrbot.api"] = api
import db_config
from web import server
import uvicorn

assert Path(db_config.DB_PATH) == path / "fixture.db"
db_config.init_db()
cache = path / "web_cache"
cache.mkdir()
(cache / "shared.png").write_bytes(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII="
    )
)
with db_config.get_db_connection() as db:
    for sid, text, ts, plat, name in [
        (
            "qq:group:42",
            "UI message one [CQ:image,url=/static/cache/shared.png]",
            100,
            "qq",
            "Synthetic QQ",
        ),
        ("qq:group:42", "UI message two", 200, "qq", "Synthetic QQ"),
        (
            "tg:group:42",
            "UI message other [CQ:image,url=/static/cache/shared.png]",
            100,
            "telegram",
            "Synthetic TG",
        ),
    ]:
        db.execute(
            "INSERT INTO chat_history (session_id,message,timestamp,user_id,sender_name,message_type,session_name,msg_id,platform_name,platform_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                sid,
                text,
                ts,
                "synthetic",
                "Synthetic sender",
                "GroupMessage",
                name,
                ts,
                plat,
                plat,
            ],
        )
    db.commit()
server.load_right_align_ids = lambda: frozenset({"synthetic-bot"})
server._custom_apis_loaded = True


@server.app.middleware("http")
async def isolate_assets(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; font-src 'self'; frame-src 'none'; object-src 'none'"
    )
    return response


@server.app.get("/api/test/state")
def fixture_state():
    schema = json.loads((ROOT / "_conf_schema.json").read_text())["basic"]["items"]
    with db_config.get_db_connection() as db:
        rows = db.execute(
            "SELECT id,message,session_id FROM chat_history ORDER BY id"
        ).fetchall()
        trash = db.execute("SELECT COUNT(*) AS count FROM archive_trash").fetchone()[
            "count"
        ]
        stats = db.execute(
            "SELECT session_id,message_count FROM session_stats ORDER BY session_id"
        ).fetchall()
    return {
        "synthetic_only": True,
        "rows": rows,
        "trash_count": trash,
        "stats": stats,
        "cache_bytes": (cache / "shared.png").read_bytes().hex(),
        "retention_disabled_by_default": schema["message_retention_days"]["default"]
        == 0
        and schema["message_retention_global"]["default"] is False,
    }


try:
    uvicorn.run(server.app, host="127.0.0.1", port=18993, log_level="warning")
finally:
    db_config.get_connection_pool().close_all()
    data.cleanup()
