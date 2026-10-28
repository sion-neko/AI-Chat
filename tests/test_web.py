"""画面とのやりとり（HTTP）のテスト。アプリのサーバーを本当に立てて、APIを叩く。

LLMは決まった応答を返す偽物（tests/test_agent.py の FakeLLM）に差し替える。外にはつながない。
"""

import base64
import json
import os
import queue
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

with mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://unused.invalid/v1", "LLM_API_KEY": "x", "LLM_MODEL": "fake"}):
    from app import agent, db, models, sources, web
    from app.sources import dummy, mcp_servers

from tests.test_agent import FakeLLM


class Gate(FakeLLM):
    """open() されるまで応答を返さないLLM。調査中の状態を作る。"""

    def __init__(self):
        super().__init__([])
        self.opened = threading.Event()

    def invoke(self, messages):
        self.opened.wait(10)
        return super().invoke(messages)


class WebTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.llm = FakeLLM([])
        for patch in (
            mock.patch.object(db, "DB_PATH", str(Path(tmp.name) / "chat.db")),
            mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://unused.invalid/v1", "LLM_MODEL": "fake"}),
            mock.patch.object(agent, "_llm", self.llm),
            # 手元の .env や MCPサーバーの設定によらず、サンプルのソースだけで動かす
            mock.patch.object(sources, "BUILTIN", [dummy.CHAT, dummy.WIKI]),
            mock.patch.object(mcp_servers, "_loaded", {}),
            mock.patch.object(mcp_servers, "CONFIG_PATH", str(Path(tmp.name) / "mcp.json")),
            # アクセスのログでテストの出力を埋めない
            mock.patch.object(web.Handler, "log_message"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        db.init()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method: str, path: str, body=None, headers: dict | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                status, raw, kind = res.status, res.read(), res.headers.get_content_type()
        except urllib.error.HTTPError as e:
            status, raw, kind = e.code, e.read(), e.headers.get_content_type()
        return status, json.loads(raw) if kind == "application/json" else raw.decode()

    def events(self) -> queue.Queue:
        """/api/events を読み続け、届いたイベントを入れるキュー。

        読んでいる最中のレスポンスを閉じると、読み終わる（時間切れになる）まで待たされるので、
        ソケットを直接使い、終わったら shutdown で読み取りを止める。
        """
        sock = socket.create_connection(self.server.server_address, timeout=10)
        self.addCleanup(sock.close)
        self.addCleanup(sock.shutdown, socket.SHUT_RDWR)
        sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        lines = sock.makefile("rb")
        while lines.readline().strip():  # ヘッダーを読み飛ばす
            pass
        q = queue.Queue()

        def read():
            try:
                for line in lines:
                    q.put(json.loads(line))
            except (OSError, ValueError):
                pass

        threading.Thread(target=read, daemon=True).start()
        return q

    def wait_for(self, q: queue.Queue, kind: str) -> dict:
        while True:
            event = q.get(timeout=10)
            if event["type"] == kind:
                return event

    # ---- 画面と、手元から以外の断り ----

    def test_config_and_static_files(self):
        status, config = self.call("GET", "/api/config?x=1")  # クエリが付いていても同じ
        self.assertEqual(status, 200)
        self.assertEqual((config["model"], config["demo"]), ("fake", True))
        self.assertEqual([s["id"] for s in config["sources"]], ["dummy_chat", "dummy_wiki"])
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("おばけ探偵事務所", page)
        self.assertEqual(self.call("GET", "/static/../app/web.py")[0], 404)
        self.assertEqual(self.call("GET", "/api/nope"), (404, {"error": "not found"}))
        self.assertEqual(self.call("DELETE", "/api/config")[0], 404)

    def test_rejects_other_hosts_and_sites(self):
        self.assertEqual(self.call("GET", "/api/config", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/folders", {"name": "x"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/folders", {"name": "x"}, {"Origin": self.base})[0], 200)

    # ---- 調査 ----

    def test_consult_turn_is_streamed_saved_and_titled(self):
        q = self.events()
        self.wait_for(q, "ready")
        status, started = self.call("POST", "/api/conversations", {"content": "要約して", "sources": []})
        self.assertEqual(status, 200)
        job = self.wait_for(q, "job")
        self.assertEqual((job["turn_id"], job["consult"], job["question"]), (started["turn_id"], True, "要約して"))
        done = self.wait_for(q, "done")
        self.assertEqual((done["turn"]["kind"], done["turn"]["content"]), ("chat", "回答"))
        # 最初の回答のあとに、所長が題名を付ける
        self.assertEqual(self.wait_for(q, "title")["title"], "回答")

        conv_id = started["conversation_id"]
        _, conv = self.call("GET", f"/api/conversations/{conv_id}")
        self.assertEqual([t["role"] for t in conv["turns"]], ["user", "assistant"])
        _, listed = self.call("GET", "/api/conversations")
        self.assertEqual([(c["title"], c["unread"]) for c in listed], [("回答", True)])
        # 画面で読んだら、未読の印を外す
        self.call("PATCH", f"/api/conversations/{conv_id}", {"unread": False})
        self.assertFalse(self.call("GET", "/api/conversations")[1][0]["unread"])
        # LLMとのやりとりも残る
        _, trace = self.call("GET", f"/api/traces/{done['turn']['trace_id']}")
        self.assertEqual([c["who"] for c in trace], ["シロ所長（相談）"])

    def test_one_turn_at_a_time_per_conversation(self):
        gate = Gate()
        q = self.events()
        with mock.patch.object(agent, "_llm", gate):
            _, started = self.call("POST", "/api/conversations", {"content": "要約して", "sources": []})
            conv_id = started["conversation_id"]
            status, body = self.call("POST", f"/api/conversations/{conv_id}/messages", {"content": "続き", "sources": []})
            self.assertEqual(status, 400)
            self.assertIn("まだ調べています", body["error"])
            gate.opened.set()
            self.wait_for(q, "done")
        self.assertEqual(self.call("POST", f"/api/conversations/{conv_id}/messages", {"content": "続き", "sources": []})[0], 200)
        self.wait_for(q, "done")

    def test_cancel_new_conversation_removes_it(self):
        gate = Gate()
        q = self.events()
        with mock.patch.object(agent, "_llm", gate):
            _, started = self.call("POST", "/api/conversations", {"content": "要約して", "sources": []})
            self.call("POST", f"/api/turns/{started['turn_id']}/cancel")
            gate.opened.set()
            cancelled = self.wait_for(q, "cancelled")
        self.assertTrue(cancelled["deleted"])
        self.assertEqual(self.call("GET", f"/api/conversations/{started['conversation_id']}")[0], 404)

    def test_question_images_are_saved_and_served(self):
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
        body = {"content": "これ何？", "sources": [], "images": [png]}
        # 画像を読めないモデルでは断る
        self.assertIn("画像を読めません", self.call("POST", "/api/conversations", body)[1]["error"])
        q = self.events()
        with mock.patch.object(models, "vision", return_value=True):
            for bad, message in (([png] * 5, "4枚まで"), (["data:image/svg+xml;base64,AAAA"], "PNG・JPEG")):
                self.assertIn(message, self.call("POST", "/api/conversations", {**body, "images": bad})[1]["error"])
            self.assertEqual(self.call("POST", "/api/conversations", body)[0], 200)
            done = self.wait_for(q, "done")
        [image_id] = done["question"]["images"]
        with urllib.request.urlopen(f"{self.base}/api/images/{image_id}", timeout=10) as res:
            self.assertEqual((res.headers["Content-Type"], res.read()), ("image/png", b"\x89PNG\r\n\x1a\n"))
            # 画像は変わらないので、画面に覚えさせる
            self.assertIn("max-age", res.headers["Cache-Control"])
        self.assertEqual(self.call("GET", "/api/images/nope")[0], 404)

    def test_deleting_conversation_stops_its_investigation(self):
        gate = Gate()
        q = self.events()
        with mock.patch.object(agent, "_llm", gate):
            _, first = self.call("POST", "/api/conversations", {"content": "要約して", "sources": []})
            gate.opened.set()
            self.wait_for(q, "done")
            gate.opened.clear()
            conv_id = first["conversation_id"]
            self.call("POST", f"/api/conversations/{conv_id}/messages", {"content": "続き", "sources": []})
            self.assertEqual(self.call("DELETE", f"/api/conversations/{conv_id}"), (200, {}))
            gate.opened.set()
            # 保存先がないので、続きの回答は保存しない（会話を作り直さない）
            self.wait_for(q, "cancelled")
        self.assertEqual(self.call("GET", f"/api/conversations/{conv_id}")[0], 404)

    def test_unexpected_errors_are_500(self):
        with mock.patch.object(db, "list_folders", side_effect=RuntimeError("壊れた")), mock.patch.object(web.traceback, "print_exc"):
            self.assertEqual(self.call("GET", "/api/folders"), (500, {"error": "RuntimeError: 壊れた"}))

    def test_bad_requests(self):
        bad = [
            ("POST", "/api/conversations", {"content": "x", "sources": ["nope"]}),
            ("POST", "/api/conversations", {"content": "x", "sources": [], "folder_id": "nope"}),
            ("POST", "/api/conversations", {"content": "x", "sources": [], "images": ["data:image/png;base64,AAAA"]}),
            ("POST", "/api/folders", {"name": "  "}),
            ("PATCH", "/api/settings", {"memo": "あ" * 2001}),
            ("PATCH", "/api/settings", {"selection": {"unchecked": "x"}}),
        ]
        for method, path, body in bad:
            status, data = self.call(method, path, body)
            self.assertEqual(status, 400, (path, body, data))
            self.assertTrue(data["error"])
        self.assertEqual(self.call("PATCH", "/api/conversations/nope", {"title": "x"})[0], 404)
        self.assertEqual(self.call("POST", "/api/conversations/nope/messages", {"content": "x", "sources": []})[0], 404)

    def test_redo_and_followup_are_checked(self):
        q = self.events()
        _, started = self.call("POST", "/api/conversations", {"content": "要約して", "sources": []})
        self.wait_for(q, "done")
        conv_id = started["conversation_id"]
        # 回答のターン（1）からはやり直せない。相談の回答には続報を頼めない
        for body, message in (
            ({"content": "x", "sources": [], "from_turn": 1}, "やり直す質問の位置"),
            ({"content": "x", "sources": ["dummy_chat"], "followup": True}, "所長が回答した後"),
            ({"content": "x", "sources": [], "followup": True}, "調べる場所を選んで"),
        ):
            status, data = self.call("POST", f"/api/conversations/{conv_id}/messages", body)
            self.assertEqual(status, 400)
            self.assertIn(message, data["error"])

    # ---- フォルダ・設定・キャラクター ----

    def test_folders_and_settings(self):
        _, folder = self.call("POST", "/api/folders", {"name": " 案件X "})
        self.assertEqual(folder["name"], "案件X")
        _, folder = self.call("PATCH", f"/api/folders/{folder['id']}", {"memo": "VPNは拠点間のこと"})
        self.assertEqual(folder["memo"], "VPNは拠点間のこと")
        self.assertEqual([f["name"] for f in self.call("GET", "/api/folders")[1]], ["案件X"])
        self.call("DELETE", f"/api/folders/{folder['id']}")
        self.assertEqual(self.call("GET", "/api/folders")[1], [])

        _, saved = self.call("PATCH", "/api/settings", {"memo": "結論から", "selection": {"unchecked": ["dummy_wiki"], "scopes": {}}})
        self.assertEqual(saved, {"memo": "結論から", "selection": {"unchecked": ["dummy_wiki"], "scopes": {}}})

    def test_characters(self):
        _, listed = self.call("PUT", "/api/characters/director", {"name": "クロ", "persona": "せっかち"})
        self.assertEqual((listed[0]["name"], listed[0]["persona"]), ("クロ", "せっかち"))
        self.assertEqual(self.call("GET", "/api/config")[1]["director"], "クロ")
        _, listed = self.call("DELETE", "/api/characters/director")
        self.assertEqual(listed[0]["name"], "シロ")
        self.assertEqual(self.call("PUT", "/api/characters/nope", {"name": "x", "persona": ""})[0], 404)
        self.assertEqual(self.call("PUT", "/api/characters/director", {"name": "", "persona": ""})[0], 400)

    def test_mcp_servers(self):
        self.assertIn("表示名", self.call("POST", "/api/mcp", {"label": " ", "url": "http://x"})[1]["error"])
        self.assertEqual(self.call("PUT", "/api/mcp/nope", {"label": "x", "url": "http://x"})[0], 404)
        # つながらないサーバーでも設定は残り、理由が一覧に出る
        _, listed = self.call("POST", "/api/mcp", {"label": "Wiki", "url": "http://127.0.0.1:9/mcp"})
        [entry] = listed["servers"]
        self.assertEqual((entry["key"], entry["connected"]), ("wiki", False))
        self.assertTrue(entry["error"])
        _, listed = self.call("DELETE", "/api/mcp/wiki")
        self.assertEqual(listed["servers"], [])


if __name__ == "__main__":
    unittest.main()
