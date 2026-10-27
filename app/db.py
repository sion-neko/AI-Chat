"""会話の保存先（ローカルのSQLite）。

会話はターン単位のJSONで保存する。
  user:      {"role": "user", "content": ...}
             images: [画像ID]（images テーブル）、image_text: 画像を文字に起こしたもの（添えられたときだけ）
  assistant: {"role": "assistant", "agent", "content", "model", "reports": [所員の報告（検索の過程も含む）]}
             再調査の報告は request（所長からの依頼）を持ち、最初の報告の後ろに並ぶ
             links: [{"url", "found"}] 回答のリンクが検索結果にあったか（app/links.py）
             seconds, llm_calls: かかった時間とLLMの呼び出し回数
             kind: "clarify" なら、調べる前に所長が聞き返したターン（content が質問。reports・links は持たない）
                   "chat" なら、調べる場所を選ばずに所長がそのまま答えたターン（reports・links は持たない）
             memo_added: [{"scope", "note", "folder", "saved"}] 調査のあとに所長がメモに書き足した内容
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
-- 案件ごとに調査をまとめるフォルダ
CREATE TABLE IF NOT EXISTS folders (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    memo       TEXT NOT NULL DEFAULT '',   -- このフォルダの調査にだけ効くメモ
    selection  TEXT NOT NULL DEFAULT '{}', -- このフォルダで入力欄に選んだソースと範囲（SELECTION の形）
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived   INTEGER NOT NULL DEFAULT 0,
    folder_id  TEXT REFERENCES folders(id) ON DELETE SET NULL,
    unread     INTEGER NOT NULL DEFAULT 0  -- 回答が届いたが、まだ画面で読んでいない
);
-- 質問に添えられた画像。会話のターンには id だけを持たせる
CREATE TABLE IF NOT EXISTS images (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    mime            TEXT NOT NULL,
    data            BLOB NOT NULL
);
-- 全体の設定（値はJSON）。memo: すべての調査に効くメモ、selection: フォルダの外で入力欄に選んだソースと範囲
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
        "folder_id": "TEXT REFERENCES folders(id) ON DELETE SET NULL",
        "unread": "INTEGER NOT NULL DEFAULT 0",
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


def _conversation(row) -> dict:
    return {**dict(row), "archived": bool(row["archived"]), "unread": bool(row["unread"])}


def list_conversations(query: str = "", archived: bool = False, folder_id: str | None = None) -> list[dict]:
    """会話の一覧。query は空白区切りのAND検索で、タイトルと中身（質問・回答・調査の記録）を探す。

    folder_id を渡すとそのフォルダの会話だけ。None ならすべて。
    """
    where, params = ["archived = ?"], [int(archived)]
    if folder_id is not None:
        where.append("folder_id = ?")
        params.append(folder_id)
    for word in query.split():
        pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(
            "(title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM turns WHERE turns.conversation_id = conversations.id AND data LIKE ? ESCAPE '\\'))"
        )
        params += [pattern, pattern]
    sql = f"SELECT id, title, updated_at, archived, folder_id, unread FROM conversations WHERE {' AND '.join(where)} ORDER BY updated_at DESC"
    with connect() as conn:
        return [_conversation(r) for r in conn.execute(sql, params)]


def get_conversation(conv_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT id, title, updated_at, archived, folder_id, unread FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return _conversation(row) if row else None


def create_conversation(title: str, folder_id: str | None = None) -> str:
    conv_id = uuid.uuid4().hex
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, folder_id) VALUES (?, ?, ?, ?, ?)",
            (conv_id, title, now, now, folder_id),
        )
    return conv_id


def update_conversation(conv_id: str, **changes) -> bool:
    """渡された項目（title, archived, folder_id, unread）だけ書き換える。folder_id=None はフォルダから出す。

    会話がなければ False。更新日は変えない（並び順が動かないように）。
    """
    assert set(changes) <= {"title", "archived", "folder_id", "unread"}
    for key in ("archived", "unread"):
        if key in changes:
            changes[key] = int(changes[key])
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone():
            return False
        if changes:
            conn.execute(f"UPDATE conversations SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?", (*changes.values(), conv_id))
    return True


def delete_conversation(conv_id: str):
    with connect() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def delete_empty_conversations():
    """ターンが1つもない会話を消す。最初の調査の途中でアプリが止まると残る（起動したときに呼ぶ）。"""
    with connect() as conn:
        conn.execute("DELETE FROM conversations WHERE NOT EXISTS (SELECT 1 FROM turns WHERE turns.conversation_id = conversations.id)")


# ---- フォルダ ----


def _folder(row) -> dict:
    return {**dict(row), "selection": json.loads(row["selection"])}


def list_folders() -> list[dict]:
    """フォルダの一覧（名前順）。count はアーカイブしていない調査の数。"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT f.*, (SELECT COUNT(*) FROM conversations c WHERE c.folder_id = f.id AND c.archived = 0) AS count
               FROM folders f ORDER BY f.name"""
        )
        return [_folder(r) for r in rows]


def get_folder(folder_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        return _folder(row) if row else None


def create_folder(name: str) -> str:
    folder_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute("INSERT INTO folders (id, name, created_at) VALUES (?, ?, ?)", (folder_id, name, _now()))
    return folder_id


def update_folder(folder_id: str, **changes) -> bool:
    """渡された項目（name, memo, selection）だけ書き換える。フォルダがなければ False。"""
    assert set(changes) <= {"name", "memo", "selection"}
    if "selection" in changes:
        changes["selection"] = json.dumps(changes["selection"], ensure_ascii=False)
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,)).fetchone():
            return False
        if changes:
            conn.execute(f"UPDATE folders SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?", (*changes.values(), folder_id))
    return True


def delete_folder(folder_id: str):
    """フォルダを消す。中の調査は消さず、フォルダの外に出す。"""
    with connect() as conn:
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))


# ---- 画像 ----


def save_image(conv_id: str, mime: str, data: bytes) -> str:
    image_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute("INSERT INTO images VALUES (?, ?, ?, ?)", (image_id, conv_id, mime, data))
    return image_id


def load_image(image_id: str) -> tuple[str, bytes] | None:
    """(MIMEタイプ, バイト列)。なければ None。"""
    with connect() as conn:
        row = conn.execute("SELECT mime, data FROM images WHERE id = ?", (image_id,)).fetchone()
        return (row["mime"], bytes(row["data"])) if row else None


# ---- 全体の設定 ----


def get_setting(key: str, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default


def set_setting(key: str, value):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))


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
