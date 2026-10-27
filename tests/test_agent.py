import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

with mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://unused.invalid/v1", "LLM_API_KEY": "x", "LLM_MODEL": "fake"}):
    from app import agent, db
from langchain_core.tools import tool

from app.sources import dummy, scope
from app.sources.base import Source


@tool
def search_scoped(query: str) -> str:
    """選ばれた範囲を返すだけの検索ツール。"""
    return f"範囲: {scope.get()}"


SCOPED = Source(id="scoped", label="範囲つき", tools=[search_scoped], configured=True, scopes=lambda: [], scope_label="リポジトリ")


@tool
def search_since(query: str) -> str:
    """続報の日付を返すだけの検索ツール。"""
    return f"以降: {scope.since(None)}"


SINCE = Source(id="since", label="日付つき", tools=[search_since], configured=True)


class FakeLLM:
    """ツールを渡されたかどうかと会話の中身から、決まった応答を返すLLM。"""

    def __init__(self, director_requests: list[dict], ask: str | None = None, notes: list[dict] | None = None):
        self.director_requests = director_requests
        self.ask = ask  # 受付で聞き返す内容（None なら聞き返さない）
        self.notes = notes or []  # 調査のあとにメモへ書き足す内容（remember の引数）
        self.tools = []
        self.director_calls = 0

    def bind_tools(self, tools):
        bound = type(self).__new__(type(self))
        bound.__dict__ = self.__dict__.copy()
        bound.tools = tools
        bound.parent = self
        return bound

    def stream(self, messages):
        yield self.invoke(messages)

    def invoke(self, messages):
        names = [t["function"]["name"] if isinstance(t, dict) else t.name for t in self.tools]
        last = messages[-1]
        if isinstance(last.content, list):  # 画像を読む
            return AIMessage("エラー画面。「Disk quota exceeded」（mon01）")
        if names == ["ask_user"]:  # 所長の受付
            if self.ask:
                return AIMessage("", tool_calls=[{"id": "a1", "name": "ask_user", "args": {"question": self.ask}}])
            return AIMessage("調査します")
        if names == ["remember"]:  # 所長のメモ
            return AIMessage("なし", tool_calls=[{"id": f"m{i}", "name": "remember", "args": a} for i, a in enumerate(self.notes)])
        if "request_reinvestigation" in names or not names:  # 所長
            root = getattr(self, "parent", self)
            root.director_calls += 1
            if names and self.director_requests:
                calls = [{"id": f"c{i}", "name": "request_reinvestigation", "args": a} for i, a in enumerate(self.director_requests)]
                return AIMessage("", tool_calls=calls)
            return AIMessage("回答", response_metadata={"model_name": "fake-1"})
        if isinstance(last, HumanMessage):  # 所員の最初の呼び出し
            return AIMessage("", tool_calls=[{"id": "s1", "name": names[0], "args": {"query": "ディスク"}}])
        assert isinstance(last, ToolMessage)
        return AIMessage(f"報告: {last.content[:20]}")


class AgentTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(db, "DB_PATH", str(Path(tmp.name) / "chat.db"))
        patch.start()
        self.addCleanup(patch.stop)
        db.init()
        self.events = []

    def run_turn(self, llm):
        with mock.patch.object(agent, "_llm", llm):
            return agent.run_turn([], "ディスク増設どうなった？", [dummy.CHAT, dummy.WIKI], self.events.append)

    def test_without_followup(self):
        llm = FakeLLM([])
        turn = self.run_turn(llm)
        self.assertEqual(turn["content"], "回答")
        self.assertEqual(turn["model"], "fake-1")
        self.assertEqual([r["source"] for r in turn["reports"]], ["dummy_chat", "dummy_wiki"])
        self.assertEqual(llm.director_calls, 1)

        # 受付1回、所員2人 × (検索の判断 + 報告)、所長の回答1回とメモ1回。誰の呼び出しかと、送った内容が残る
        whos = [c["who"] for c in turn["trace"]]
        self.assertEqual(turn["llm_calls"], 7)
        self.assertCountEqual(whos, ["シロ所長（受付）", "シロ所長", "シロ所長（メモ）","コソリ（ダミーWiki）", "コソリ（ダミーWiki）", "ポルタ（ダミーチャット）", "ポルタ（ダミーチャット）"])
        director_call = turn["trace"][whos.index("シロ所長")]
        self.assertEqual(director_call["tools"], ["request_reinvestigation"])
        self.assertEqual(director_call["messages"][0]["role"], "system")
        self.assertEqual(director_call["response"]["content"], "回答")
        member_call = next(c for c in turn["trace"] if c["response"].get("tool_calls"))
        self.assertEqual(member_call["response"]["tool_calls"][0]["args"], {"query": "ディスク"})
        self.assertIn("seconds", turn["reports"][0]["steps"][0])
        self.assertGreaterEqual(turn["seconds"], turn["reports"][0]["seconds"])

    def test_consult_without_sources(self):
        llm = FakeLLM([], ask="聞き返さないはず", notes=[{"scope": "global", "note": "書かないはず"}])
        with mock.patch.object(agent, "_llm", llm):
            turn = agent.run_turn([], "この文章を要約して", [], self.events.append, memos={"global": "結論から書く", "folder": None})
        self.assertEqual((turn["kind"], turn["content"], turn["model"]), ("chat", "回答", "fake-1"))
        self.assertNotIn("reports", turn)
        self.assertEqual([c["who"] for c in turn["trace"]], ["シロ所長（相談）"])
        self.assertIn("結論から書く", turn["trace"][0]["messages"][0]["content"])
        self.assertEqual([e["type"] for e in self.events if e["type"] != "delta"], ["director_start"])

    def test_explain_error(self):
        import httpx
        import openai

        request = httpx.Request("POST", "http://llm/v1/chat/completions")
        with mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://llm/v1"}):
            self.assertIn("LLM_BASE_URL", agent.explain_error(openai.APIConnectionError(request=request)))
        self.assertIn("LLM_TIMEOUT", agent.explain_error(openai.APITimeoutError(request=request)))
        too_long = openai.BadRequestError("This model's maximum context length is 8192 tokens", response=httpx.Response(400, request=request), body=None)
        self.assertIn("長すぎて", agent.explain_error(too_long))
        self.assertEqual(agent.explain_error(ValueError("x")), "ValueError: x")

    def test_followup_runs_member_again_then_answers(self):
        llm = FakeLLM([{"source": "dummy_chat", "instruction": "10月に絞って"}, {"source": "nope", "instruction": "x"}])
        turn = self.run_turn(llm)

        self.assertEqual(turn["content"], "回答")
        followups = [r for r in turn["reports"] if "request" in r]
        self.assertEqual([(r["source"], r["request"]) for r in followups], [("dummy_chat", "10月に絞って")])
        self.assertTrue(followups[0]["steps"])
        # 再調査は FOLLOWUP_ROUNDS 回まで。最後はツールなしで呼ばれて回答する
        self.assertEqual(llm.director_calls, agent.FOLLOWUP_ROUNDS + 1)
        types = [e["type"] for e in self.events]
        self.assertEqual(types.count("followup"), 1)
        # 再調査の後にもう一度まとめ始め、回答が流れてきて、最後にメモを考える
        self.assertEqual(types[-3:], ["director_start", "delta", "memo_start"])
        self.assertEqual(self.events[-2]["text"], "回答")

    def test_scope_reaches_tools_in_each_member(self):
        llm = FakeLLM([{"source": "scoped", "instruction": "もう一度"}])
        with mock.patch.object(agent, "_llm", llm):
            turn = agent.run_turn([], "VPN", [SCOPED, dummy.CHAT], self.events.append, {"scoped": ["me/infra"]})

        scoped = [r for r in turn["reports"] if r["source"] == "scoped"]
        # 最初の調査も、再調査も、選んだ範囲で検索する
        self.assertEqual([r["steps"][0]["result"] for r in scoped], ["範囲: ['me/infra']"] * 2)
        self.assertEqual([r.get("scope") for r in scoped], [["me/infra"]] * 2)
        # 範囲を選んでいない所員は絞らない
        chat = next(r for r in turn["reports"] if r["source"] == "dummy_chat")
        self.assertNotIn("scope", chat)
        prompt = next(c for c in turn["trace"] if c["who"].endswith("（範囲つき）"))["messages"][1]["content"]
        self.assertIn("検索範囲（リポジトリ）は次に絞られています: me/infra", prompt)

    def test_followup_searches_only_after_previous_answer(self):
        llm = FakeLLM([{"source": "since", "instruction": "もう一度"}], ask="どの件ですか？")
        history = [{"role": "user", "content": "ディスク増設どうなった？"}, {"role": "assistant", "content": "来週メンテで増設予定", "date": "2026-10-20"}]
        with mock.patch.object(agent, "_llm", llm):
            turn = agent.run_turn(history, "その後どうなった？", [SINCE], self.events.append, since="2026-10-20")

        # 聞き返さずに調べ、最初の調査も再調査も前回の日付より後だけを探す
        self.assertNotIn("kind", turn)
        self.assertFalse(any(c["who"].endswith("（受付）") for c in turn["trace"]))
        self.assertEqual([r["steps"][0]["result"] for r in turn["reports"]], ["以降: 2026-10-20"] * 2)
        member = next(c for c in turn["trace"] if c["who"].endswith("（日付つき）"))["messages"][1]["content"]
        self.assertIn("- ディスク増設どうなった？", member)
        self.assertIn("2026-10-20 以降の新しい動きだけ", member)
        director = next(c for c in turn["trace"] if c["who"] == "シロ所長")["messages"][0]["content"]
        self.assertIn("前回 2026-10-20 の回答から何が変わったか", director)
        self.assertRegex(turn["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_clarify_then_answer(self):
        llm = FakeLLM([], ask="どのサーバーの話ですか？（監視サーバー・ファイルサーバーなど）")
        with mock.patch.object(agent, "_llm", llm):
            first = agent.run_turn([], "あの件どうなった？", [dummy.CHAT], self.events.append)
            self.assertEqual(first["kind"], "clarify")
            self.assertEqual(first["content"], "どのサーバーの話ですか？（監視サーバー・ファイルサーバーなど）")
            self.assertNotIn("reports", first)
            self.assertEqual(llm.director_calls, 0)

            # 答えると、続けては聞き返さずに調べる。所員には確認と答えを合わせて渡す
            history = [{"role": "user", "content": "あの件どうなった？"}, first]
            second = agent.run_turn(history, "監視サーバー", [dummy.CHAT], self.events.append)
        self.assertNotIn("kind", second)
        prompt = next(c for c in second["trace"] if c["who"].startswith("ポルタ"))["messages"][1]["content"]
        self.assertIn("- あの件どうなった？", prompt)
        self.assertIn("今回の質問: 監視サーバー（所長の確認「どのサーバーの話ですか？（監視サーバー・ファイルサーバーなど）」への答え）", prompt)

    def test_skip_clarify(self):
        llm = FakeLLM([], ask="どの件ですか？")
        with mock.patch.object(agent, "_llm", llm):
            turn = agent.run_turn([], "あの件どうなった？", [dummy.CHAT], self.events.append, clarify=False)
        self.assertNotIn("kind", turn)
        self.assertFalse(any(c["who"].endswith("（受付）") for c in turn["trace"]))

    def test_intake_failure_does_not_stop_investigation(self):
        llm = FakeLLM([])
        original = llm.bind_tools

        def bind_tools(tools):
            if any(isinstance(t, dict) and t["function"]["name"] == "ask_user" for t in tools):
                raise RuntimeError("tools not supported")
            return original(tools)

        llm.bind_tools = bind_tools
        with mock.patch.object(agent, "_llm", llm):
            turn = agent.run_turn([], "ディスク", [dummy.CHAT], self.events.append)
        self.assertEqual(turn["content"], "回答")

    def test_memo_reaches_every_prompt(self):
        memos = {"global": "回答は結論から", "folder": {"name": "案件X", "memo": "- 「監視サーバー」は mon01 のこと"}}
        with mock.patch.object(agent, "_llm", FakeLLM([])):
            turn = agent.run_turn([], "監視サーバーどうなった？", [dummy.CHAT], self.events.append, memos=memos)
        for call in turn["trace"]:
            system = call["messages"][0]["content"]
            self.assertIn("## 事務所のメモ", system, call["who"])
            self.assertIn("回答は結論から", system)
            self.assertIn("「監視サーバー」は mon01 のこと", system)
            self.assertIn("この調査は案件「案件X」のものです", system)

    def test_no_memo_section_without_memos(self):
        self.assertEqual(agent.memo_section(None), "")
        self.assertEqual(agent.memo_section({"global": "", "folder": None}), "")

    def test_remember(self):
        notes = [
            {"scope": "folder", "note": "「監視サーバー」は\\n mon01 のこと"},
            {"scope": "nope", "note": "範囲外"},
            {"scope": "global", "note": " "},
            {"scope": "global", "note": "回答は結論から"},
        ]
        memos = {"global": "", "folder": {"name": "案件X", "memo": ""}}
        with mock.patch.object(agent, "_llm", FakeLLM([], notes=notes)):
            turn = agent.run_turn([], "監視サーバーどうなった？", [dummy.CHAT], self.events.append, memos=memos)
        # 不正なものは捨て、改行は詰める
        self.assertEqual(turn["memo_notes"], [{"scope": "folder", "note": "「監視サーバー」は\\n mon01 のこと"}, {"scope": "global", "note": "回答は結論から"}])

        # フォルダに入っていない調査では、フォルダのメモには書かない
        with mock.patch.object(agent, "_llm", FakeLLM([], notes=notes)):
            turn = agent.run_turn([], "監視サーバーどうなった？", [dummy.CHAT], self.events.append, memos={"global": "", "folder": None})
        self.assertEqual(turn["memo_notes"], [{"scope": "global", "note": "回答は結論から"}])

    def test_clarify_does_not_remember(self):
        with mock.patch.object(agent, "_llm", FakeLLM([], ask="どの件ですか？", notes=[{"scope": "global", "note": "x"}])):
            turn = agent.run_turn([], "あの件", [dummy.CHAT], self.events.append)
        self.assertNotIn("memo_notes", turn)

    def test_question_images_are_read_once_and_passed_as_text(self):
        image = "data:image/png;base64,iVBORw0KGgo="
        with mock.patch.object(agent, "_llm", FakeLLM([])):
            turn = agent.run_turn([], "これ前にもあった？", [dummy.CHAT, dummy.WIKI], self.events.append, images=[image, image])

        self.assertEqual(turn["image_text"], "画像1:\nエラー画面。「Disk quota exceeded」（mon01）\n\n画像2:\nエラー画面。「Disk quota exceeded」（mon01）")
        whos = [c["who"] for c in turn["trace"]]
        self.assertEqual(whos.count("シロ所長（画像）"), 2)
        # 記録には画像そのものは入れず、枚数だけ
        image_call = turn["trace"][whos.index("シロ所長（画像）")]
        self.assertIn("[画像1枚]", image_call["messages"][0]["content"])
        self.assertNotIn("base64", str(turn["trace"]))
        # 所員と所長には文字に起こした内容が届く
        member = next(c for c in turn["trace"] if c["who"].startswith("ポルタ"))
        self.assertIn("Disk quota exceeded", member["messages"][1]["content"])
        director = next(c for c in turn["trace"] if c["who"] == "シロ所長")
        self.assertIn("Disk quota exceeded", director["messages"][-1]["content"])
        self.assertEqual(self.events[0]["type"], "images_start")

    def test_image_text_in_history(self):
        history = [{"role": "user", "content": "これ何？", "image_text": "エラー画面"}, {"role": "assistant", "content": "障害です"}]
        self.assertEqual(agent.member_questions(history, "その後は？")[0], "これ何？\n\n（添えられた画像の内容）\nエラー画面")

    def test_view_image_tool_only_with_vision_model(self):
        fetched = []

        def fetch(url):
            fetched.append(url)
            return b"\x89PNG", "image/png"

        source = Source(id="pics", label="写真", tools=[dummy.search_dummy_chat], configured=True, fetch_image=fetch)
        tool = agent._view_image_tool(source, agent.Trace(), "ポルタ（写真）")
        with mock.patch.object(agent, "_llm", FakeLLM([])):
            self.assertIn("Disk quota exceeded", tool.invoke({"url": "/user_uploads/1/a.png"}))
            for _ in range(agent.VIEW_IMAGE_LIMIT - 1):
                tool.invoke({"url": "/user_uploads/1/b.png"})
            with self.assertRaisesRegex(agent.ApiError, "枚まで"):
                tool.invoke({"url": "/user_uploads/1/c.png"})
        self.assertEqual(len(fetched), agent.VIEW_IMAGE_LIMIT)

        # 画像を読めないモデルなら、所員に view_image を渡さない
        for model, expected in (("llama-3.3-70b", False), ("qwen2.5-vl-72b", True)):
            with mock.patch.dict(os.environ, {"LLM_MODEL": model}), mock.patch.object(agent, "_llm", FakeLLM([])):
                turn = agent.run_turn([], "ディスク", [source], self.events.append, clarify=False)
            member = next(c for c in turn["trace"] if c["who"].endswith("（写真）"))
            self.assertEqual("view_image" in member["tools"], expected, model)

    def test_llm_calls_are_limited(self):
        class SlowLLM(FakeLLM):
            running = 0
            peak = 0
            lock = threading.Lock()

            def invoke(self, messages):
                with SlowLLM.lock:
                    SlowLLM.running += 1
                    SlowLLM.peak = max(SlowLLM.peak, SlowLLM.running)
                time.sleep(0.05)
                with SlowLLM.lock:
                    SlowLLM.running -= 1
                return super().invoke(messages)

        # 所員は並列に動くが、LLMを同時に呼ぶのは枠の数まで
        with mock.patch.object(agent, "_llm_slots", threading.BoundedSemaphore(1)):
            turn = self.run_turn(SlowLLM([]))
        self.assertEqual(turn["content"], "回答")
        self.assertEqual(SlowLLM.peak, 1)

    def test_cancel_stops_whole_turn(self):
        def emit(event):
            if event["type"] == "tool":
                raise agent.Cancelled

        # 所員の中で投げられても、所員の調査失敗にならずターン全体が止まる
        with mock.patch.object(agent, "_llm", FakeLLM([])), self.assertRaises(agent.Cancelled):
            agent.run_turn([], "ディスク", [dummy.CHAT, dummy.WIKI], emit)


if __name__ == "__main__":
    unittest.main()
