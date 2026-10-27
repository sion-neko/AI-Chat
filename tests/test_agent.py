import os
import tempfile
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


class FakeLLM:
    """ツールを渡されたかどうかと会話の中身から、決まった応答を返すLLM。"""

    def __init__(self, director_requests: list[dict]):
        self.director_requests = director_requests
        self.tools = []
        self.director_calls = 0

    def bind_tools(self, tools):
        bound = FakeLLM.__new__(FakeLLM)
        bound.__dict__ = self.__dict__.copy()
        bound.tools = tools
        bound.parent = self
        return bound

    def stream(self, messages):
        yield self.invoke(messages)

    def invoke(self, messages):
        names = [t["function"]["name"] if isinstance(t, dict) else t.name for t in self.tools]
        last = messages[-1]
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

        # 所員2人 × (検索の判断 + 報告) と所長1回。誰の呼び出しかと、送った内容が残る
        whos = [c["who"] for c in turn["trace"]]
        self.assertEqual(turn["llm_calls"], 5)
        self.assertCountEqual(whos, ["シロ所長", "コソリ（ダミーWiki）", "コソリ（ダミーWiki）", "ポルタ（ダミーチャット）", "ポルタ（ダミーチャット）"])
        director_call = turn["trace"][whos.index("シロ所長")]
        self.assertEqual(director_call["tools"], ["request_reinvestigation"])
        self.assertEqual(director_call["messages"][0]["role"], "system")
        self.assertEqual(director_call["response"]["content"], "回答")
        member_call = next(c for c in turn["trace"] if c["response"].get("tool_calls"))
        self.assertEqual(member_call["response"]["tool_calls"][0]["args"], {"query": "ディスク"})
        self.assertIn("seconds", turn["reports"][0]["steps"][0])
        self.assertGreaterEqual(turn["seconds"], turn["reports"][0]["seconds"])

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
        # 再調査の後にもう一度まとめ始め、回答が流れてくる
        self.assertEqual(types[-2:], ["director_start", "delta"])
        self.assertEqual(self.events[-1]["text"], "回答")

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

    def test_cancel_stops_whole_turn(self):
        def emit(event):
            if event["type"] == "tool":
                raise agent.Cancelled

        # 所員の中で投げられても、所員の調査失敗にならずターン全体が止まる
        with mock.patch.object(agent, "_llm", FakeLLM([])), self.assertRaises(agent.Cancelled):
            agent.run_turn([], "ディスク", [dummy.CHAT, dummy.WIKI], emit)


if __name__ == "__main__":
    unittest.main()
