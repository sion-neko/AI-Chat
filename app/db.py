"""会話の保存先（ローカルのSQLite）。

会話はターン単位のJSONで保存する。
  user:      {"role": "user", "content": ...}
  assistant: {"role": "assistant", "agent", "content", "model", "reports": [所員の報告（検索の過程も含む）]}
             再調査の報告は request（所長からの依頼）を持ち、最初の報告の後ろに並ぶ
             links: [{"url", "found"}] 回答のリンクが検索結果にあったか（app/links.py）
             seconds, llm_calls: かかった時間とLLMの呼び出し回数
             kind: "clarify" なら、調べる前に所長が聞き返したターン（content が質問。reports・links は持たない）
             trace_id: LLMとのやりとりの記録（traces テーブル。プロンプトを丸ごと含み大きいので分けて持つ）
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
    updated_at TEXT NOT NULL,
    archived   INTEGER NOT NULL DEFAULT 0,
    tags       TEXT NOT NULL DEFAULT '[]'  -- タグ名のJSON配列
);
CREATE TABLE IF NOT EXISTS turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    data            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id, id);
CREATE TABLE IF NOT EXISTS traces (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    data            TEXT NOT NULL
);
-- 画面で編集したキャラクター。key は "director" かソースID。ない場合はコードの既定値を使う
CREATE TABLE IF NOT EXISTS characters (
    key     TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    persona TEXT NOT NULL
);
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


# あとから足した列。古いDBには ALTER TABLE で足す
ADDED_COLUMNS = {
    "conversations": {
        "archived": "INTEGER NOT NULL DEFAULT 0",
        "tags": "TEXT NOT NULL DEFAULT '[]'",
    },
}


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in ADDED_COLUMNS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def list_conversations(query: str = "", archived: bool = False) -> list[dict]:
    """会話の一覧。query は空白区切りのAND検索で、tag:名前 はタグ、それ以外はタイトルと中身（質問・回答・調査の記録）を探す。"""
    where, params = ["archived = ?"], [int(archived)]
    for word in query.split():
        if word.startswith("tag:") and len(word) > 4:
            where.append("EXISTS (SELECT 1 FROM json_each(conversations.tags) WHERE value = ?)")
            params.append(word[4:])
        else:
            pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM turns WHERE turns.conversation_id = conversations.id AND data LIKE ? ESCAPE '\\'))"
            )
            params += [pattern, pattern]
    sql = f"SELECT id, title, updated_at, archived, tags FROM conversations WHERE {' AND '.join(where)} ORDER BY updated_at DESC"
    with connect() as conn:
        return [{**dict(r), "archived": bool(r["archived"]), "tags": json.loads(r["tags"])} for r in conn.execute(sql, params)]


def create_conversation(title: str) -> str:
    conv_id = uuid.uuid4().hex
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conv_id, title, now, now),
        )
    return conv_id


def update_conversation(conv_id: str, title: str | None = None, archived: bool | None = None, tags: list[str] | None = None) -> bool:
    """渡された項目だけ書き換える。会話がなければ False。更新日は変えない（並び順が動かないように）。"""
    changes = {"title": title, "archived": None if archived is None else int(archived), "tags": None if tags is None else json.dumps(tags, ensure_ascii=False)}
    changes = {k: v for k, v in changes.items() if v is not None}
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone():
            return False
        if changes:
            conn.execute(f"UPDATE conversations SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?", (*changes.values(), conv_id))
    return True


def all_tags() -> list[str]:
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT value FROM conversations, json_each(conversations.tags) ORDER BY value")
        return [r["value"] for r in rows]


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


def replace_turns(conv_id: str, start: int, turns: list[dict]):
    """start 番目（0始まり）以降のターンを消して turns に置き換える。消したターンのLLMの記録も消す。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, data FROM turns WHERE conversation_id = ? ORDER BY id LIMIT -1 OFFSET ?", (conv_id, start)
        ).fetchall()
        trace_ids = [json.loads(r["data"]).get("trace_id") for r in rows]
        conn.executemany("DELETE FROM traces WHERE id = ?", [(t,) for t in trace_ids if t])
        conn.executemany("DELETE FROM turns WHERE id = ?", [(r["id"],) for r in rows])
        conn.executemany(
            "INSERT INTO turns (conversation_id, data) VALUES (?, ?)",
            [(conv_id, json.dumps(t, ensure_ascii=False)) for t in turns],
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), conv_id))


def save_trace(conv_id: str, calls: list[dict]) -> str:
    trace_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute("INSERT INTO traces VALUES (?, ?, ?)", (trace_id, conv_id, json.dumps(calls, ensure_ascii=False)))
    return trace_id


def load_trace(trace_id: str) -> list[dict] | None:
    with connect() as conn:
        row = conn.execute("SELECT data FROM traces WHERE id = ?", (trace_id,)).fetchone()
        return json.loads(row["data"]) if row else None


def load_characters() -> dict[str, dict]:
    with connect() as conn:
        return {r["key"]: {"name": r["name"], "persona": r["persona"]} for r in conn.execute("SELECT * FROM characters")}


def save_character(key: str, name: str, persona: str):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO characters VALUES (?, ?, ?)", (key, name, persona))


def delete_character(key: str):
    with connect() as conn:
        conn.execute("DELETE FROM characters WHERE key = ?", (key,))
