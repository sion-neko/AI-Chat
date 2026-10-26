"""会話の保存先（ローカルのSQLite）。

会話はターン単位のJSONで保存する。
  user:      {"role": "user", "content": ...}
  assistant: {"role": "assistant", "agent", "content", "model", "reports": [所員の報告（検索の過程も含む）]}
"""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/work/data/chat.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    data            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    """接続を開き、抜けるときにコミット（例外ならロールバック）して閉じる。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)


def list_conversations() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC")
        return [dict(r) for r in rows]


def create_conversation(title: str) -> str:
    conv_id = uuid.uuid4().hex
    now = _now()
    with connect() as conn:
        conn.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", (conv_id, title, now, now))
    return conv_id


def delete_conversation(conv_id: str):
    with connect() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def load_turns(conv_id: str) -> list[dict] | None:
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone():
            return None
        rows = conn.execute("SELECT data FROM turns WHERE conversation_id = ? ORDER BY id", (conv_id,))
        return [json.loads(r["data"]) for r in rows]


def append_turns(conv_id: str, turns: list[dict]):
    with connect() as conn:
        conn.executemany(
            "INSERT INTO turns (conversation_id, data) VALUES (?, ?)",
            [(conv_id, json.dumps(t, ensure_ascii=False)) for t in turns],
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), conv_id))
