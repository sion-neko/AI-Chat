"""ローカルWeb UI。標準ライブラリのHTTPサーバーで静的ファイルとチャットAPIを返す。

API:
  GET    /api/config
  GET    /api/conversations?q=&archived=1&folder= 会話一覧。q は空白区切りのANDで、タイトルと中身を探す。folder でフォルダを絞る
  PATCH  /api/conversations/{id}          {title?, archived?, folder_id?, unread?} 名前・アーカイブ・フォルダ・未読の印を変える（folder_id: null でフォルダの外へ）
  POST   /api/conversations               {content, sources, folder_id?, images?} 新しい会話を作って最初の質問をする
  GET    /api/folders                     フォルダの一覧
  POST   /api/folders                     {name} フォルダを作る
  PATCH  /api/folders/{id}                {name?, memo?, selection?}
  DELETE /api/folders/{id}                フォルダを消す（中の調査はフォルダの外に出る）
  GET    /api/settings                    {memo, selection} 全体のメモと、フォルダの外で選んだソースと範囲
  PATCH  /api/settings                    {memo?, selection?}
  GET    /api/conversations/{id}          会話の中身（ターン単位）
  GET    /api/traces/{trace_id}           1ターン分のLLMとのやりとり
  POST   /api/conversations/{id}/messages {content, sources, from_turn?, images?} 続きの質問をする
                                          from_turn（質問のターンの位置）があれば、そこから後を消して質問し直す（成功したときだけ置き換える）
                                          画像を送らずにやり直したときは、元の質問の画像を使う
  GET    /api/images/{id}                 質問に添えられた画像
  DELETE /api/conversations/{id}
  POST   /api/turns/{turn_id}/cancel        調査中のターンを中止する
  GET    /api/events                      調査の進み具合（NDJSON。つないでいる間ずっと流れる）
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
sources は検索に使うソースIDの配列。scopes は {ソースID: [リポジトリやチャンネルの名前]}（省略可。ないソースは絞らない）。
selection は入力欄の選択を覚えておく形 {"unchecked": [外したソースID], "scopes": {ソースID: [名前]}}。

質問するPOSTは、調査を始めて {turn_id, conversation_id} をすぐ返す（同じ会話で調査中なら400）。
調査は画面を閉じても最後まで続き、進み具合は /api/events に1行1イベントで流れる（app/jobs.py）。
つないだときは、調査中のものを始めから流し直す。どのイベントも turn_id を持つ。
  {"type": "job", "conversation_id", "title", "question", "index", "replace", "followup", "image_count", "started_at"}  調査を始めた
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
  {"type": "ready"}                             つないだときに、調査中のものを流し終えた（turn_id なし）
  {"type": "title", "conversation_id", "title"}  所長が調査に題名を付けた（最初の回答のあと。turn_id なし）
  {"type": "ping"}                              何もないときにときどき来る（turn_id なし）
入力の検証で弾くときは、ストリームを始める前に通常のJSONで400を返す。
"""

import json
import os
import queue
import re
import threading
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import base64

from app import agent, characters, db, destination, jobs, models, sources
from app.sources import mcp_servers

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
TITLE_LENGTH = 40
FOLDER_NAME_LENGTH = 40
# メモはそのままプロンプトに入るので、ローカルLLMの文脈を使い切らない長さに抑える
MEMO_LENGTH = 2000
# 質問に添えられる画像の枚数と大きさ
IMAGE_COUNT = 4
IMAGE_SIZE = 5 * 1024 * 1024
# 進み具合のストリームで、何もないときに書く間隔（秒）
EVENTS_PING = 15
IMAGE_URL = re.compile(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)")


class InputError(Exception):
    """リクエストの中身が不正。メッセージはそのまま画面に出る。"""


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


def _data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


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
    if len(memo) > MEMO_LENGTH:
        raise InputError(f"メモは{MEMO_LENGTH}文字以内にしてください（今は{len(memo)}文字）")
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
    """会話の名前・アーカイブ・フォルダの変更を確かめて、db.update_conversation に渡せる形にする。"""
    changes = {}
    if "title" in req:
        changes["title"] = _name(req["title"], TITLE_LENGTH * 2)
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


def settings() -> dict:
    return {"memo": db.get_setting("memo", ""), "selection": db.get_setting("selection", {})}


def add_to_memo(conv_id: str, notes: list[dict]) -> list[dict]:
    """所長が決めた内容をメモに書き足す。上限を超えるものは書かない。

    返り値は [{"scope", "note", "folder"（フォルダ名。全体なら None）, "saved"}]。画面に出す。
    """
    folder = db.get_folder((db.get_conversation(conv_id) or {}).get("folder_id") or "")
    result = []
    for n in notes:
        target = folder if n["scope"] == "folder" else None
        if n["scope"] == "folder" and not folder:
            continue
        current = target["memo"] if target else db.get_setting("memo", "")
        memo = f"{current}\n- {n['note']}".strip()
        saved = len(memo) <= MEMO_LENGTH
        if saved and target:
            db.update_folder(target["id"], memo=memo)
            target["memo"] = memo
        elif saved:
            db.set_setting("memo", memo)
        result.append({"scope": n["scope"], "note": n["note"], "folder": target["name"] if target else None, "saved": saved})
    return result


def followup_since(history: list[dict], conversation: dict) -> str:
    """「その後どうなった？」で調べ始める日。直前の回答を調べた日（日付を持つ前の回答は、会話の更新日）。"""
    last = history[-1] if history else None
    if not last or last["role"] != "assistant" or last.get("kind") == "clarify":
        raise InputError("続報を調べられるのは、所長が回答した後だけです")
    return last.get("date") or conversation["updated_at"][:10]


def retitle(conv_id: str):
    """最初の回答が出たら、所長に調査の題名を付けさせる（題名が最初の質問のままのときだけ。名前を変えていたら触らない）。"""
    conversation = db.get_conversation(conv_id)
    turns = db.load_turns(conv_id) or []
    answers = [t for t in turns if t["role"] == "assistant" and t.get("kind") != "clarify"]
    if not conversation or len(answers) != 1 or conversation["title"] != turns[0]["content"][:TITLE_LENGTH]:
        return
    questions = agent.member_questions(turns[:-2], turns[-2]["content"])
    if title := agent.make_title(questions, answers[0]["content"]):
        db.update_conversation(conv_id, title=title)
        jobs.broadcast({"type": "title", "conversation_id": conv_id, "title": title})


def memos_for(conv_id: str | None, folder_id: str | None) -> dict:
    """調査のプロンプトに入れるメモ。会話がフォルダに入っていれば、そのフォルダのメモも。"""
    if conv_id:
        folder_id = (db.get_conversation(conv_id) or {}).get("folder_id")
    folder = db.get_folder(folder_id) if folder_id else None
    return {"global": db.get_setting("memo", ""), "folder": {"name": folder["name"], "memo": folder["memo"]} if folder else None}


def config() -> dict:
    return {
        "model": os.environ["LLM_MODEL"],
        # LLMの送り先が手元のネットワークの中か（app/destination.py）
        "llm_url": os.environ["LLM_BASE_URL"],
        "destination": destination.classify(os.environ["LLM_BASE_URL"]),
        # 今のモデルが画像を読めるか（app/models.json）。読めるときだけ画面で画像を添えられる
        "vision": models.vision(),
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
    all_sources = sources.all_sources()
    with ThreadPoolExecutor() as pool:
        llm = pool.submit(agent.check_llm)
        results = dict(zip((s.id for s in all_sources), pool.map(sources.check, all_sources)))
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
        except (sources.SourceError, characters.CharacterError, InputError, mcp_servers.McpConfigError) as e:
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
        if self.path == "/api/events":
            return self._stream_events()
        if url.path == "/api/conversations":
            convs = db.list_conversations(query.get("q", ""), query.get("archived") == "1", query.get("folder") or None)
            return self._send_json(200, convs)
        if self.path == "/api/folders":
            return self._send_json(200, db.list_folders())
        if self.path == "/api/settings":
            return self._send_json(200, settings())
        if self.path == "/api/characters":
            return self._send_json(200, character_list())
        if m := re.fullmatch(r"/api/sources/(\w+)/scopes", self.path):
            return self._send_json(200, sources.scope_choices(m[1]))
        if self.path == "/api/mcp":
            return self._send_json(200, {"path": mcp_servers.CONFIG_PATH, "servers": mcp_servers.listing()})
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
        if m := re.fullmatch(r"/api/images/(\w+)", self.path):
            image = db.load_image(m[1])
            if image is None:
                return self.send_error(404)
            mime, data = image
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            return self.wfile.write(data)

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
            folder_id = _folder_id(req.get("folder_id"))
            images = _images(req.get("images"))
            conv_id = db.create_conversation(req["content"][:TITLE_LENGTH], folder_id)
            return self._start_turn(
                conv_id, [], req["content"], selected, scopes, is_new=True, clarify=not req.get("skip_clarify"), images=images
            )
        if m := re.fullmatch(r"/api/conversations/(\w+)/messages", self.path):
            history = db.load_turns(m[1])
            if history is None:
                return self._send_json(404, {"error": "not found"})
            req = self._read_json()
            selected = sources.resolve(req["sources"])
            scopes = sources.resolve_scopes(selected, req.get("scopes") or {})
            images = _images(req.get("images"))
            image_ids = None
            start = req.get("from_turn")
            if start is not None:
                if not (isinstance(start, int) and 0 <= start < len(history) and history[start]["role"] == "user"):
                    raise InputError("やり直す質問の位置が正しくありません")
                # やり直しで画像を送り直さなかったときは、元の質問の画像をそのまま使う
                if not images and history[start].get("images"):
                    image_ids = history[start]["images"]
                    images = [db.load_image(i) for i in image_ids]
                history = history[:start]
            since = None
            if req.get("followup"):
                since = followup_since(history, db.get_conversation(m[1]))
            return self._start_turn(
                m[1],
                history,
                req["content"],
                selected,
                scopes,
                is_new=False,
                replace_from=start,
                clarify=not req.get("skip_clarify"),
                images=images,
                image_ids=image_ids,
                since=since,
            )
        if self.path == "/api/mcp":
            conf = self._read_json()
            mcp_servers.save(mcp_servers.new_key(str(conf.get("label", ""))), conf)
            return self._send_json(200, {"path": mcp_servers.CONFIG_PATH, "servers": mcp_servers.listing()})
        if self.path == "/api/folders":
            folder_id = db.create_folder(_name(self._read_json().get("name", ""), FOLDER_NAME_LENGTH))
            return self._send_json(200, db.get_folder(folder_id))
        if self.path == "/api/check":
            return self._send_json(200, check())
        if m := re.fullmatch(r"/api/turns/(\w+)/cancel", self.path):
            if job := jobs.get(m[1]):
                job.cancel.set()
            return self._send_json(200, {})
        self.send_error(404)

    def _patch(self):
        if m := re.fullmatch(r"/api/conversations/(\w+)", self.path):
            if not db.update_conversation(m[1], **conversation_changes(self._read_json())):
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, {})
        if m := re.fullmatch(r"/api/folders/(\w+)", self.path):
            if not db.update_folder(m[1], **folder_changes(self._read_json())):
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, db.get_folder(m[1]))
        if self.path == "/api/settings":
            req = self._read_json()
            if "memo" in req:
                db.set_setting("memo", _memo(req["memo"]))
            if "selection" in req:
                db.set_setting("selection", _selection(req["selection"]))
            return self._send_json(200, settings())
        self.send_error(404)

    def _put(self):
        if m := re.fullmatch(r"/api/mcp/(\w+)", self.path):
            if m[1] not in mcp_servers.read_config():
                return self._send_json(404, {"error": "not found"})
            mcp_servers.save(m[1], self._read_json())
            return self._send_json(200, {"path": mcp_servers.CONFIG_PATH, "servers": mcp_servers.listing()})
        if m := re.fullmatch(r"/api/characters/(\w+)", self.path):
            if not _character_key(m[1]):
                return self._send_json(404, {"error": "not found"})
            req = self._read_json()
            characters.save(m[1], req["name"], req["persona"])
            return self._send_json(200, character_list())
        self.send_error(404)

    def _delete(self):
        if m := re.fullmatch(r"/api/conversations/(\w+)", self.path):
            # 調べている途中なら止める（保存先がなくなるので）
            for job in jobs.running():
                if job.conversation_id == m[1]:
                    job.cancel.set()
            db.delete_conversation(m[1])
            return self._send_json(200, {})
        if m := re.fullmatch(r"/api/mcp/(\w+)", self.path):
            mcp_servers.delete(m[1])
            return self._send_json(200, {"path": mcp_servers.CONFIG_PATH, "servers": mcp_servers.listing()})
        if m := re.fullmatch(r"/api/folders/(\w+)", self.path):
            db.delete_folder(m[1])
            return self._send_json(200, {})
        if m := re.fullmatch(r"/api/characters/(\w+)", self.path):
            if not _character_key(m[1]):
                return self._send_json(404, {"error": "not found"})
            characters.reset(m[1])
            return self._send_json(200, character_list())
        self.send_error(404)

    def _start_turn(
        self,
        conv_id: str,
        history: list[dict],
        question: str,
        selected: list[sources.Source],
        scopes: dict[str, list[str]],
        is_new: bool,
        replace_from: int | None = None,
        clarify: bool = True,
        images: list[tuple[str, bytes]] | None = None,
        image_ids: list[str] | None = None,
        since: str | None = None,
    ):
        """調査を別スレッドで始めて、すぐに {turn_id, conversation_id} を返す。進み具合は /api/events で配る。

        images は質問に添えられた画像。調査が成功したときだけ保存する（image_ids があれば、保存済みの同じ画像）。
        since は続報を調べるときの前回の調査日（followup_since）。
        """
        info = {
            "title": (db.get_conversation(conv_id) or {}).get("title", ""),
            "question": question,
            # 質問のターンの位置。やり直しなら、そこから後が置き換わる
            "index": len(history),
            "replace": replace_from is not None,
            "followup": since is not None,
            "image_count": len(images or []),
        }
        try:
            job = jobs.start(conv_id, info)
        except jobs.Busy:
            raise InputError("この調査はまだ調べています。終わるか中止してから質問してください")

        def emit(event: dict):
            # 中止されたら、次にイベントを出そうとしたところで調査を止める
            if job.cancel.is_set():
                raise agent.Cancelled
            jobs.publish(job, event)

        def work():
            try:
                urls = [_data_url(mime, data) for mime, data in images or []]
                turn = agent.run_turn(history, question, selected, emit, scopes, clarify, memos_for(conv_id, None), urls, since)
                turn["trace_id"] = db.save_trace(conv_id, turn.pop("trace"))
                if "memo_notes" in turn:
                    turn["memo_added"] = add_to_memo(conv_id, turn.pop("memo_notes"))
                asked = {"role": "user", "content": question}
                if since:
                    asked["since"] = since
                if images:
                    asked["images"] = image_ids or [db.save_image(conv_id, mime, data) for mime, data in images]
                    asked["image_text"] = turn.pop("image_text", "")
                new_turns = [asked, turn]
                if replace_from is None:
                    db.append_turns(conv_id, new_turns)
                else:
                    db.replace_turns(conv_id, replace_from, new_turns)
                # 画面で読むまで、一覧に印を出す（見ている画面が読んだら外す）
                db.update_conversation(conv_id, unread=True)
                jobs.publish(job, {"type": "done", "conversation_id": conv_id, "question": asked, "turn": turn})
                retitle(conv_id)
            except agent.Cancelled:
                if is_new:
                    db.delete_conversation(conv_id)
                jobs.publish(job, {"type": "cancelled", "conversation_id": conv_id, "deleted": is_new})
            except Exception as e:
                traceback.print_exc()
                if is_new:
                    db.delete_conversation(conv_id)
                jobs.publish(job, {"type": "error", "conversation_id": conv_id, "deleted": is_new, "message": f"{type(e).__name__}: {e}"})

        threading.Thread(target=work, daemon=True).start()
        return self._send_json(200, {"turn_id": job.turn_id, "conversation_id": conv_id})

    def _stream_events(self):
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
    port = int(os.environ.get("PORT", "8080"))
    print(f"listening on :{port} (model={os.environ['LLM_MODEL']}, db={db.DB_PATH})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
