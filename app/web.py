"""ローカルWeb UI。標準ライブラリのHTTPサーバーで静的ファイルとチャットAPIを返す。

API:
  GET    /api/config
  GET    /api/conversations               会話一覧
  POST   /api/conversations               {content, sources} 新しい会話を作って最初の質問をする
  GET    /api/conversations/{id}          会話の中身（ターン単位）
  POST   /api/conversations/{id}/messages {content, sources} 続きの質問をする
  DELETE /api/conversations/{id}
  GET    /api/characters                  所長と所員（使えるソースの分）
  PUT    /api/characters/{key}            {name, persona} キャラクターを編集する。key は "director" かソースID
  DELETE /api/characters/{key}            キャラクターを既定に戻す

sources は検索に使うソースIDの配列。

質問するPOSTは、進み具合をNDJSON（1行1イベント）で逐次返す。
  {"type": "member_start", "source", "agent"}   所員が調査を始めた
  {"type": "tool", "source", "tool", "args"}     所員が検索した
  {"type": "member_done", "report"}             所員が報告した
  {"type": "director_start", "agent"}           所長がまとめ始めた（再調査の後にもう一度来る）
  {"type": "followup", "source", "agent", "instruction"}  所長が所員に再調査を頼んだ（続いて member_start 〜 member_done が来る）
  {"type": "done", "conversation_id", "turn"}   回答（保存済み）
  {"type": "error", "message"}
入力の検証で弾くときは、ストリームを始める前に通常のJSONで400を返す。
"""

import json
import os
import queue
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import agent, characters, db, sources

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
TITLE_LENGTH = 40


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
        except (sources.SourceError, characters.CharacterError) as e:
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

    def do_DELETE(self):
        self._handle(self._delete)

    def _get(self):
        if self.path == "/api/config":
            return self._send_json(200, config())
        if self.path == "/api/conversations":
            return self._send_json(200, db.list_conversations())
        if self.path == "/api/characters":
            return self._send_json(200, character_list())
        if m := re.fullmatch(r"/api/conversations/(\w+)", self.path):
            turns = db.load_turns(m[1])
            if turns is None:
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, {"id": m[1], "turns": turns})

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
            conv_id = db.create_conversation(req["content"][:TITLE_LENGTH])
            return self._stream_turn(conv_id, [], req["content"], selected, is_new=True)
        if m := re.fullmatch(r"/api/conversations/(\w+)/messages", self.path):
            history = db.load_turns(m[1])
            if history is None:
                return self._send_json(404, {"error": "not found"})
            req = self._read_json()
            selected = sources.resolve(req["sources"])
            return self._stream_turn(m[1], history, req["content"], selected, is_new=False)
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

    def _stream_turn(self, conv_id: str, history: list[dict], question: str, selected: list[sources.Source], is_new: bool):
        """調査を別スレッドで走らせ、キューに溜まったイベントを1行ずつ書き出す。

        所員は並列で動くので、レスポンスへの書き込みはこのスレッドだけで行う。
        """
        events: queue.Queue[dict | None] = queue.Queue()

        def work():
            try:
                turn = agent.run_turn(history, question, selected, events.put)
                db.append_turns(conv_id, [{"role": "user", "content": question}, turn])
                events.put({"type": "done", "conversation_id": conv_id, "turn": turn})
            except Exception as e:
                traceback.print_exc()
                if is_new:
                    db.delete_conversation(conv_id)
                events.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                events.put(None)

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
