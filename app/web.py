"""ローカルWeb UI。標準ライブラリのHTTPサーバーで静的ファイルとチャットAPIを返す。

ここに置くのは、リクエストの検証と、各モジュールへの受け渡しだけ。
調査を走らせて保存するのは app/turns.py、進み具合を配るのは app/jobs.py。

API:
  GET    /api/config
  GET    /api/conversations?q=&archived=1&folder= 会話一覧。q は空白区切りのANDで、タイトルと中身を探す。folder でフォルダを絞る
  POST   /api/conversations               {content, sources, folder_id?, images?} 新しい会話を作って最初の質問をする
  GET    /api/conversations/{id}          会話の中身（ターン単位）
  PATCH  /api/conversations/{id}          {title?, archived?, folder_id?, unread?} 名前・アーカイブ・フォルダ・未読の印を変える（folder_id: null でフォルダの外へ）
  DELETE /api/conversations/{id}          会話を消す（調査中なら止める）
  POST   /api/conversations/{id}/messages {content, sources, from_turn?, followup?, images?} 続きの質問をする（app/turns.py の ask）
  POST   /api/turns/{turn_id}/cancel      調査中のターンを中止する
  GET    /api/events                      調査の進み具合（NDJSON。つないでいる間ずっと流れる）
  GET    /api/traces/{trace_id}           1ターン分のLLMとのやりとり
  GET    /api/images/{id}                 質問に添えられた画像
  GET    /api/folders                     フォルダの一覧
  POST   /api/folders                     {name} フォルダを作る
  PATCH  /api/folders/{id}                {name?, memo?, selection?}
  DELETE /api/folders/{id}                フォルダを消す（中の調査はフォルダの外に出る）
  GET    /api/settings                    {memo, selection} 全体のメモと、フォルダの外で選んだソースと範囲
  PATCH  /api/settings                    {memo?, selection?}
  GET    /api/characters                  所長と所員（使えるソースの分）
  PUT    /api/characters/{key}            {name, persona} キャラクターを編集する。key は "director" かソースID
  DELETE /api/characters/{key}            キャラクターを既定に戻す
  GET    /api/sources/{id}/scopes         そのソースで選べる検索範囲（リポジトリやチャンネル）
  GET    /api/mcp                         MCPサーバーの設定と、つないだ結果
  POST   /api/mcp                         {label, url か command, ...} MCPサーバーを足して、つなぐ
  PUT    /api/mcp/{key}                   MCPサーバーの設定を変えて、つなぎ直す
  DELETE /api/mcp/{key}                   MCPサーバーを外す
  GET    /api/status                      LLMの設定と、すべてのソース（未設定のものも）
  POST   /api/check                       LLMと設定済みのソースに実際につないでみる

skip_clarify: true なら所長は聞き返さずに調べる。
images は質問に添える画像（data URL の配列。4枚・1枚5MBまで）。画像を読めるモデル（app/models.json）のときだけ受け付ける。
sources は検索に使うソースIDの配列。空なら調べずに所長が答える（相談）。scopes は {ソースID: [リポジトリやチャンネルの名前]}（省略可。ないソースは絞らない）。
selection は入力欄の選択を覚えておく形 {"unchecked": [外したソースID], "scopes": {ソースID: [名前]}}。

質問するPOSTは、調査を始めて {turn_id, conversation_id} をすぐ返す（同じ会話で調査中なら400）。
調査は画面を閉じても最後まで続き、進み具合は /api/events に1行1イベントで流れる（app/jobs.py）。
つないだときは、調査中のものを始めから流し直す。調査のイベントは turn_id を持つ。
  {"type": "job", "conversation_id", "title", "question", "index", "replace", "followup", "consult", "image_count", "started_at"}  調査を始めた
  {"type": "images_start", "agent", "count"}    所長が添えられた画像を読んでいる
  {"type": "intake_start", "agent"}             所長が依頼を受け付けている（曖昧なら聞き返す）
  {"type": "member_start", "source", "agent"}   所員が調査を始めた
  {"type": "tool", "source", "tool", "args"}     所員が検索した
  {"type": "member_done", "report"}             所員が報告した
  {"type": "director_start", "agent"}           所長がまとめ始めた（再調査の後にもう一度来る）
  {"type": "followup", "source", "agent", "instruction"}  所長が所員に再調査を頼んだ（続いて member_start 〜 member_done が来る）
  {"type": "delta", "text"}                    所長の回答の続き（director_start で書き直しになる）
  {"type": "memo_start", "agent"}               所長がメモに書き足すことを考えている
  {"type": "done", "conversation_id", "question", "turn"}  質問と回答のターン（保存済み。会話には未読の印が付く）
  {"type": "cancelled", "conversation_id", "deleted"}  中止した（何も保存しない。新しい会話なら会話ごと消す: deleted）
  {"type": "error", "conversation_id", "deleted", "message"}
調査に紐づかないイベント（turn_id なし）:
  {"type": "ready"}                             つないだときに、調査中のものを流し終えた
  {"type": "title", "conversation_id", "title"}  所長が調査に題名を付けた（最初の回答のあと）
  {"type": "ping"}                              何もないときにときどき来る
"""

import base64
import json
import os
import queue
import re
import traceback
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import agent, characters, db, destination, jobs, models, sources, turns
from app.errors import InputError, NotFound
from app.sources import mcp_servers

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
TITLE_LENGTH = 80
FOLDER_NAME_LENGTH = 40
# 質問に添えられる画像の枚数と大きさ
IMAGE_COUNT = 4
IMAGE_SIZE = 5 * 1024 * 1024
IMAGE_URL = re.compile(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)")
# 進み具合のストリームで、何もないときに書く間隔（秒）
EVENTS_PING = 15

# 画面を開いてよいホスト名。ほかの名前で届いたリクエストは断る（DNSリバインディングで、
# 別のサイトから手元の会話を読まれないように）。ALLOWED_HOSTS（カンマ区切り）で足せる
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", *filter(None, os.environ.get("ALLOWED_HOSTS", "").split(","))}


# ---- 入力の検証 ----


def _images(value) -> list[tuple[str, bytes]]:
    """質問に添えられた画像（data URL の配列）を確かめて、(MIMEタイプ, バイト列) にする。"""
    if not value:
        return []
    if not models.vision():
        raise InputError(f"今のモデル（{os.environ['LLM_MODEL']}）は画像を読めません")
    if not isinstance(value, list) or len(value) > IMAGE_COUNT:
        raise InputError(f"画像は{IMAGE_COUNT}枚までにしてください")
    images = []
    for url in value:
        m = IMAGE_URL.fullmatch(str(url))
        if not m:
            raise InputError("画像は PNG・JPEG・WebP・GIF にしてください")
        data = base64.b64decode(m[2])
        if len(data) > IMAGE_SIZE:
            raise InputError(f"画像は1枚{IMAGE_SIZE // 1024 // 1024}MBまでにしてください")
        images.append((m[1], data))
    return images


def question(req: dict) -> turns.Question:
    """質問するPOSTの中身を確かめる。会話を作る前に呼ぶ（使えないソースで空の会話が残らないように）。"""
    selected = sources.resolve(req["sources"])
    return turns.Question(
        content=req["content"],
        sources=selected,
        scopes=sources.resolve_scopes(selected, req.get("scopes") or {}),
        clarify=not req.get("skip_clarify"),
        images=_images(req.get("images")),
    )


def _name(value, length: int) -> str:
    name = str(value).strip()
    if not name:
        raise InputError("名前を入れてください")
    return name[:length]


def _folder_id(value) -> str | None:
    if value is None:
        return None
    if not db.get_folder(str(value)):
        raise InputError("そのフォルダはありません")
    return str(value)


def _memo(value) -> str:
    memo = str(value).strip()
    if len(memo) > turns.MEMO_LENGTH:
        raise InputError(f"メモは{turns.MEMO_LENGTH}文字以内にしてください（今は{len(memo)}文字）")
    return memo


def _selection(value) -> dict:
    """入力欄の選択（外したソースと範囲）を確かめる。中のIDや名前が今使えるかまでは見ない（使うときに確かめる）。"""
    if not isinstance(value, dict):
        raise InputError("選択の形が正しくありません")
    unchecked = value.get("unchecked", [])
    scopes = value.get("scopes", {})
    if not (isinstance(unchecked, list) and all(isinstance(s, str) for s in unchecked)):
        raise InputError("選択の形が正しくありません")
    if not (isinstance(scopes, dict) and all(isinstance(v, list) and all(isinstance(n, str) for n in v) for v in scopes.values())):
        raise InputError("選択の形が正しくありません")
    return {"unchecked": unchecked, "scopes": {k: v for k, v in scopes.items() if v}}


def conversation_changes(req: dict) -> dict:
    """会話の名前・アーカイブ・フォルダ・未読の変更を確かめて、db.update_conversation に渡せる形にする。"""
    changes = {}
    if "title" in req:
        changes["title"] = _name(req["title"], TITLE_LENGTH)
    if "archived" in req:
        changes["archived"] = bool(req["archived"])
    if "folder_id" in req:
        changes["folder_id"] = _folder_id(req["folder_id"])
    if "unread" in req:
        changes["unread"] = bool(req["unread"])
    return changes


def folder_changes(req: dict) -> dict:
    changes = {}
    if "name" in req:
        changes["name"] = _name(req["name"], FOLDER_NAME_LENGTH)
    if "memo" in req:
        changes["memo"] = _memo(req["memo"])
    if "selection" in req:
        changes["selection"] = _selection(req["selection"])
    return changes


def request_allowed(method: str, host: str | None, origin: str | None) -> bool:
    """手元のブラウザでこのアプリの画面から来たリクエストか。

    Host が許したホスト名でなければ断る。書き換えるリクエスト（GET以外）は、Origin があれば同じホストからのものに限る
    （ほかのサイトに開かれたページから、調査を頼んだり消したりされないように）。
    """
    host = host or ""
    # [::1]:8080 の : で切らないよう、IPv6 はかっこまでを名前とみなす
    hostname = host if host.endswith("]") else host.rsplit(":", 1)[0]
    if hostname not in ALLOWED_HOSTS:
        return False
    if method != "GET" and origin and urllib.parse.urlsplit(origin).netloc != host:
        return False
    return True


# ---- 画面に返す中身 ----


def settings() -> dict:
    return {"memo": db.get_setting("memo", ""), "selection": db.get_setting("selection", {})}


def config() -> dict:
    return {
        "model": os.environ["LLM_MODEL"],
        # LLMの送り先が手元のネットワークの中か（app/destination.py）
        "llm_url": os.environ["LLM_BASE_URL"],
        "destination": destination.classify(os.environ["LLM_BASE_URL"]),
        # 今のモデルが画像を読めるか（app/models.json）。読めるときだけ画面で画像を添えられる
        "vision": models.vision(),
        "director": characters.director().name,
        # 本物のソースが1つもなく、サンプルで動いている（画面で「はじめに」を出す）
        "demo": sources.demo_mode(),
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


def _character_key(key: str) -> str:
    if key != characters.DIRECTOR_KEY and key not in {s["id"] for s in sources.describe()}:
        raise NotFound
    return key


def status() -> dict:
    return {
        "llm": {"base_url": os.environ["LLM_BASE_URL"], "model": os.environ["LLM_MODEL"]},
        "sources": sources.status(),
    }


def check() -> dict:
    """LLMとソースの接続確認を並列に行う。"""
    listed = sources.listed()
    with ThreadPoolExecutor() as pool:
        llm = pool.submit(agent.check_llm)
        results = dict(zip((s.id for s in listed), pool.map(sources.check, listed)))
        return {"llm": llm.result(), "sources": results}


def mcp_listing() -> dict:
    return {"path": mcp_servers.CONFIG_PATH, "servers": mcp_servers.listing()}


# ---- 振り分け ----

# (メソッド, パスの正規表現, 処理)。処理は (Handler, パスから取った値...) を受け取り、
# JSONで返す中身を返す。自分でレスポンスを書いたとき（ストリーム・画像）は None を返す。
ROUTES: list[tuple[str, re.Pattern, Callable]] = []


def route(method: str, pattern: str):
    def register(fn):
        ROUTES.append((method, re.compile(pattern), fn))
        return fn

    return register


@route("GET", r"/api/config")
def get_config(h):
    return config()


@route("GET", r"/api/events")
def get_events(h):
    h.stream_events()


@route("GET", r"/api/conversations")
def list_conversations(h):
    return db.list_conversations(h.query.get("q", ""), h.query.get("archived") == "1", h.query.get("folder") or None)


@route("POST", r"/api/conversations")
def create_conversation(h):
    req = h.read_json()
    job = turns.ask_new(question(req), _folder_id(req.get("folder_id")))
    return {"turn_id": job.turn_id, "conversation_id": job.conversation_id}


@route("GET", r"/api/conversations/(\w+)")
def get_conversation(h, conv_id):
    if (found := db.load_turns(conv_id)) is None:
        raise NotFound
    return {"id": conv_id, "turns": found}


@route("PATCH", r"/api/conversations/(\w+)")
def update_conversation(h, conv_id):
    if not db.update_conversation(conv_id, **conversation_changes(h.read_json())):
        raise NotFound
    return {}


@route("DELETE", r"/api/conversations/(\w+)")
def delete_conversation(h, conv_id):
    turns.cancel_conversation(conv_id)
    db.delete_conversation(conv_id)
    return {}


@route("POST", r"/api/conversations/(\w+)/messages")
def ask_more(h, conv_id):
    req = h.read_json()
    job = turns.ask(conv_id, question(req), req.get("from_turn"), bool(req.get("followup")))
    return {"turn_id": job.turn_id, "conversation_id": conv_id}


@route("POST", r"/api/turns/(\w+)/cancel")
def cancel_turn(h, turn_id):
    if job := jobs.get(turn_id):
        job.cancel.set()
    return {}


@route("GET", r"/api/traces/(\w+)")
def get_trace(h, trace_id):
    if (trace := db.load_trace(trace_id)) is None:
        raise NotFound
    return trace


@route("GET", r"/api/images/(\w+)")
def get_image(h, image_id):
    if (image := db.load_image(image_id)) is None:
        raise NotFound
    mime, data = image
    h.send_bytes(data, mime, cache=True)


@route("GET", r"/api/folders")
def list_folders(h):
    return db.list_folders()


@route("POST", r"/api/folders")
def create_folder(h):
    return db.get_folder(db.create_folder(_name(h.read_json().get("name", ""), FOLDER_NAME_LENGTH)))


@route("PATCH", r"/api/folders/(\w+)")
def update_folder(h, folder_id):
    if not db.update_folder(folder_id, **folder_changes(h.read_json())):
        raise NotFound
    return db.get_folder(folder_id)


@route("DELETE", r"/api/folders/(\w+)")
def delete_folder(h, folder_id):
    db.delete_folder(folder_id)
    return {}


@route("GET", r"/api/settings")
def get_settings(h):
    return settings()


@route("PATCH", r"/api/settings")
def update_settings(h):
    req = h.read_json()
    if "memo" in req:
        db.set_setting("memo", _memo(req["memo"]))
    if "selection" in req:
        db.set_setting("selection", _selection(req["selection"]))
    return settings()


@route("GET", r"/api/characters")
def list_characters(h):
    return character_list()


@route("PUT", r"/api/characters/(\w+)")
def save_character(h, key):
    req = h.read_json()
    characters.save(_character_key(key), req["name"], req["persona"])
    return character_list()


@route("DELETE", r"/api/characters/(\w+)")
def reset_character(h, key):
    characters.reset(_character_key(key))
    return character_list()


@route("GET", r"/api/sources/(\w+)/scopes")
def list_scopes(h, source_id):
    return sources.scope_choices(source_id)


@route("GET", r"/api/mcp")
def get_mcp(h):
    return mcp_listing()


@route("POST", r"/api/mcp")
def add_mcp(h):
    conf = h.read_json()
    mcp_servers.save(mcp_servers.new_key(str(conf.get("label", ""))), conf)
    return mcp_listing()


@route("PUT", r"/api/mcp/(\w+)")
def update_mcp(h, key):
    if key not in mcp_servers.read_config():
        raise NotFound
    mcp_servers.save(key, h.read_json())
    return mcp_listing()


@route("DELETE", r"/api/mcp/(\w+)")
def delete_mcp(h, key):
    mcp_servers.delete(key)
    return mcp_listing()


@route("GET", r"/api/status")
def get_status(h):
    return status()


@route("POST", r"/api/check")
def run_check(h):
    return check()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._dispatch()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET

    def _dispatch(self):
        if not request_allowed(self.command, self.headers.get("Host"), self.headers.get("Origin")):
            return self.send_json(403, {"error": "このアプリは、自分のPCのブラウザから開いてください"})
        url = urllib.parse.urlsplit(self.path)
        self.query = dict(urllib.parse.parse_qsl(url.query))
        try:
            for method, pattern, fn in ROUTES:
                if method == self.command and (m := pattern.fullmatch(url.path)):
                    if (result := fn(self, *m.groups())) is not None:
                        self.send_json(200, result)
                    return
            if self.command == "GET" and not url.path.startswith("/api/"):
                return self._send_static(url.path)
            raise NotFound
        except NotFound:
            self.send_json(404, {"error": "not found"})
        except (InputError, sources.SourceError, characters.CharacterError, mcp_servers.McpConfigError) as e:
            self.send_json(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def read_json(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def send_bytes(self, body: bytes, content_type: str, cache: bool = False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: str):
        name = "index.html" if path == "/" else path.removeprefix("/static/")
        file = (STATIC_DIR / name).resolve()
        # static の外（../ など）は返さない
        if file.parent != STATIC_DIR.resolve() or not file.is_file():
            raise NotFound
        self.send_bytes(file.read_bytes(), f"{CONTENT_TYPES.get(file.suffix, 'application/octet-stream')}; charset=utf-8")

    def stream_events(self):
        """調査の進み具合を、つないでいる間ずっと1行1イベントで流す。調査中のものは溜めた分から流す。"""
        q = jobs.subscribe()
        # Content-Length を付けず、閉じられるまで書き続ける（HTTP/1.0）
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                try:
                    event = q.get(timeout=EVENTS_PING)
                except queue.Empty:
                    # 画面が閉じられたことに気づけるよう、ときどき書く
                    event = {"type": "ping"}
                self.wfile.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            jobs.unsubscribe(q)


if __name__ == "__main__":
    db.init()
    # 調査中に止まったときの、最初の回答が出る前の会話（中身がない）を片付ける
    db.delete_empty_conversations()
    port = int(os.environ.get("PORT", "8080"))
    print(f"listening on :{port} (model={os.environ['LLM_MODEL']}, db={db.DB_PATH})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
