"""ローカルWeb UI。標準ライブラリのHTTPサーバーで静的ファイルとチャットAPIを返す。

API:
  GET    /api/config
  GET    /api/conversations?q=&archived=1 会話一覧。q は空白区切りのAND（tag:名前 でタグ、それ以外はタイトルと中身）
  PATCH  /api/conversations/{id}          {title?, archived?, tags?} 名前・アーカイブ・タグを変える
  GET    /api/tags                        使われているタグの一覧
  POST   /api/conversations               {content, sources} 新しい会話を作って最初の質問をする
  GET    /api/conversations/{id}          会話の中身（ターン単位）
  GET    /api/traces/{trace_id}           1ターン分のLLMとのやりとり
  POST   /api/conversations/{id}/messages {content, sources, from_turn?} 続きの質問をする
                                          from_turn（質問のターンの位置）があれば、そこから後を消して質問し直す（成功したときだけ置き換える）
  DELETE /api/conversations/{id}
  POST   /api/turns/{turn_id}/cancel        調査中のターンを中止する（turn_id は start イベントで届く）
  GET    /api/characters                  所長と所員（使えるソースの分）
  PUT    /api/characters/{key}            {name, persona} キャラクターを編集する。key は "director" かソースID
  DELETE /api/characters/{key}            キャラクターを既定に戻す
  GET    /api/sources/{id}/scopes         そのソースで選べる検索範囲（リポジトリやチャンネル）
  GET    /api/status                      LLMの設定と、すべてのソース（未設定のものも）
  POST   /api/check                       LLMと設定済みのソースに実際につないでみる

skip_clarify: true なら所長は聞き返さずに調べる。
sources は検索に使うソースIDの配列。scopes は {ソースID: [リポジトリやチャンネルの名前]}（省略可。ないソースは絞らない）。

質問するPOSTは、進み具合をNDJSON（1行1イベント）で逐次返す。
  {"type": "start", "turn_id"}                  調査を始めた。中止するときに turn_id を使う
  {"type": "intake_start", "agent"}             所長が依頼を受け付けている（曖昧なら聞き返す）
  {"type": "member_start", "source", "agent"}   所員が調査を始めた
  {"type": "tool", "source", "tool", "args"}     所員が検索した
  {"type": "member_done", "report"}             所員が報告した
  {"type": "director_start", "agent"}           所長がまとめ始めた（再調査の後にもう一度来る）
  {"type": "followup", "source", "agent", "instruction"}  所長が所員に再調査を頼んだ（続いて member_start 〜 member_done が来る）
  {"type": "delta", "text"}                    所長の回答の続き（director_start で書き直しになる）
  {"type": "done", "conversation_id", "turn"}   回答（保存済み）
  {"type": "cancelled"}                         中止した（何も保存しない。新しい会話なら会話ごと消す）
  {"type": "error", "message"}
入力の検証で弾くときは、ストリームを始める前に通常のJSONで400を返す。
"""

import json
import os
import queue
import re
import threading
import traceback
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import agent, characters, db, sources

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
TITLE_LENGTH = 40
TAG_LENGTH = 20
TAG_COUNT = 10


class InputError(Exception):
    """リクエストの中身が不正。メッセージはそのまま画面に出る。"""


def conversation_changes(req: dict) -> dict:
    """会話の名前・アーカイブ・タグの変更を確かめて、db.update_conversation に渡せる形にする。"""
    changes = {}
    if "title" in req:
        title = str(req["title"]).strip()
        if not title:
            raise InputError("名前を入れてください")
        changes["title"] = title[:TITLE_LENGTH * 2]
    if "archived" in req:
        changes["archived"] = bool(req["archived"])
    if "tags" in req:
        if not isinstance(req["tags"], list):
            raise InputError("タグは配列で送ってください")
        tags = list(dict.fromkeys(str(t).strip().removeprefix("tag:") for t in req["tags"] if str(t).strip()))
        if any(len(t) > TAG_LENGTH or " " in t for t in tags):
            raise InputError(f"タグは空白なしの{TAG_LENGTH}文字以内にしてください")
        if len(tags) > TAG_COUNT:
            raise InputError(f"タグは{TAG_COUNT}個までにしてください")
        changes["tags"] = tags
    return changes

# 調査中のターン。turn_id → 中止の合図
RUNNING: dict[str, threading.Event] = {}


def config() -> dict:
    return {
        "model": os.environ["LLM_MODEL"],
        "director": characters.director().name,
        "sources": [{**s, "agent": characters.member(s["id"], s["label"]).name} for s in sources.describe()],
    }


def character_list() -> list[dict]:
    """編集画面に出すキャラクター。role は画面に出す役割の説明。"""
    director = characters.director()
    result = [{"key": characters.DIRECTOR_KEY, "role": "所長", "name": director.name, "persona": director.persona}]
    for s in sources.describe():
        member = characters.member(s["id"], s["label"])
        result.append({"key": s["id"], "role": f"{s['label']}の担当", "name": member.name, "persona": member.persona})
    return result


def status() -> dict:
    return {
        "llm": {"base_url": os.environ["LLM_BASE_URL"], "model": os.environ["LLM_MODEL"]},
        "sources": sources.status(),
    }


def check() -> dict:
    """LLMとソースの接続確認を並列に行う。"""
    with ThreadPoolExecutor() as pool:
        llm = pool.submit(agent.check_llm)
        results = dict(zip((s.id for s in sources.ALL_SOURCES), pool.map(sources.check, sources.ALL_SOURCES)))
        return {"llm": llm.result(), "sources": results}


def _character_key(key: str) -> bool:
    return key == characters.DIRECTOR_KEY or key in {s["id"] for s in sources.describe()}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, data):
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

    def _read_json(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def _handle(self, fn):
        try:
            fn()
        except (sources.SourceError, characters.CharacterError, InputError) as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._handle(self._get)

    def do_POST(self):
        self._handle(self._post)

    def do_PUT(self):
        self._handle(self._put)

    def do_PATCH(self):
        self._handle(self._patch)

    def do_DELETE(self):
        self._handle(self._delete)

    def _get(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if self.path == "/api/config":
            return self._send_json(200, config())
        if url.path == "/api/conversations":
            return self._send_json(200, db.list_conversations(query.get("q", ""), query.get("archived") == "1"))
        if self.path == "/api/tags":
            return self._send_json(200, db.all_tags())
        if self.path == "/api/characters":
            return self._send_json(200, character_list())
        if m := re.fullmatch(r"/api/sources/(\w+)/scopes", self.path):
            return self._send_json(200, sources.scope_choices(m[1]))
        if self.path == "/api/status":
            return self._send_json(200, status())
        if m := re.fullmatch(r"/api/conversations/(\w+)", self.path):
            turns = db.load_turns(m[1])
            if turns is None:
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, {"id": m[1], "turns": turns})
        if m := re.fullmatch(r"/api/traces/(\w+)", self.path):
            trace = db.load_trace(m[1])
            if trace is None:
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, trace)

        name = "index.html" if self.path == "/" else self.path.removeprefix("/static/")
        path = (STATIC_DIR / name).resolve()
        if path.parent != STATIC_DIR.resolve() or not path.is_file():
            return self.send_error(404)
        self._send(200, path.read_bytes(), CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

    def _post(self):
        if self.path == "/api/conversations":
            req = self._read_json()
            # 会話を作る前に検証する（使えないソースで空の会話が残らないように）
            selected = sources.resolve(req["sources"])
            scopes = sources.resolve_scopes(selected, req.get("scopes") or {})
            conv_id = db.create_conversation(req["content"][:TITLE_LENGTH])
            return self._stream_turn(conv_id, [], req["content"], selected, scopes, is_new=True, clarify=not req.get("skip_clarify"))
        if m := re.fullmatch(r"/api/conversations/(\w+)/messages", self.path):
            history = db.load_turns(m[1])
            if history is None:
                return self._send_json(404, {"error": "not found"})
            req = self._read_json()
            selected = sources.resolve(req["sources"])
            scopes = sources.resolve_scopes(selected, req.get("scopes") or {})
            start = req.get("from_turn")
            if start is not None:
                if not (isinstance(start, int) and 0 <= start < len(history) and history[start]["role"] == "user"):
                    raise InputError("やり直す質問の位置が正しくありません")
                history = history[:start]
            return self._stream_turn(
                m[1], history, req["content"], selected, scopes, is_new=False, replace_from=start, clarify=not req.get("skip_clarify")
            )
        if self.path == "/api/check":
            return self._send_json(200, check())
        if m := re.fullmatch(r"/api/turns/(\w+)/cancel", self.path):
            if cancel := RUNNING.get(m[1]):
                cancel.set()
            return self._send_json(200, {})
        self.send_error(404)

    def _patch(self):
        if m := re.fullmatch(r"/api/conversations/(\w+)", self.path):
            if not db.update_conversation(m[1], **conversation_changes(self._read_json())):
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, {})
        self.send_error(404)

    def _put(self):
        if m := re.fullmatch(r"/api/characters/(\w+)", self.path):
            if not _character_key(m[1]):
                return self._send_json(404, {"error": "not found"})
            req = self._read_json()
            characters.save(m[1], req["name"], req["persona"])
            return self._send_json(200, character_list())
        self.send_error(404)

    def _delete(self):
        if m := re.fullmatch(r"/api/conversations/(\w+)", self.path):
            db.delete_conversation(m[1])
            return self._send_json(200, {})
        if m := re.fullmatch(r"/api/characters/(\w+)", self.path):
            if not _character_key(m[1]):
                return self._send_json(404, {"error": "not found"})
            characters.reset(m[1])
            return self._send_json(200, character_list())
        self.send_error(404)

    def _stream_turn(
        self,
        conv_id: str,
        history: list[dict],
        question: str,
        selected: list[sources.Source],
        scopes: dict[str, list[str]],
        is_new: bool,
        replace_from: int | None = None,
        clarify: bool = True,
    ):
        """調査を別スレッドで走らせ、キューに溜まったイベントを1行ずつ書き出す。

        所員は並列で動くので、レスポンスへの書き込みはこのスレッドだけで行う。
        """
        events: queue.Queue[dict | None] = queue.Queue()
        turn_id = uuid.uuid4().hex
        cancel = RUNNING[turn_id] = threading.Event()

        def emit(event: dict):
            # 中止されたら、次にイベントを出そうとしたところで調査を止める
            if cancel.is_set():
                raise agent.Cancelled
            events.put(event)

        def work():
            try:
                turn = agent.run_turn(history, question, selected, emit, scopes, clarify)
                turn["trace_id"] = db.save_trace(conv_id, turn.pop("trace"))
                new_turns = [{"role": "user", "content": question}, turn]
                if replace_from is None:
                    db.append_turns(conv_id, new_turns)
                else:
                    db.replace_turns(conv_id, replace_from, new_turns)
                events.put({"type": "done", "conversation_id": conv_id, "turn": turn})
            except agent.Cancelled:
                if is_new:
                    db.delete_conversation(conv_id)
                events.put({"type": "cancelled"})
            except Exception as e:
                traceback.print_exc()
                if is_new:
                    db.delete_conversation(conv_id)
                events.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                RUNNING.pop(turn_id, None)
                events.put(None)

        events.put({"type": "start", "turn_id": turn_id})
        threading.Thread(target=work, daemon=True).start()

        # Content-Length を付けず、書き終えたら接続を閉じる（HTTP/1.0）
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.end_headers()
        while (event := events.get()) is not None:
            try:
                self.wfile.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # ブラウザが閉じられても調査と保存は最後まで続ける
                return


if __name__ == "__main__":
    db.init()
    port = int(os.environ.get("PORT", "8080"))
    print(f"listening on :{port} (model={os.environ['LLM_MODEL']}, db={db.DB_PATH})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
