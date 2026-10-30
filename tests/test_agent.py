import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

with mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://unused.invalid/v1", "LLM_API_KEY": "x", "LLM_MODEL": "fake"}):
    from app import agent, db
from app.sources import dummy


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
        self.assertEqual(types[-1], "director_start")


if __name__ == "__main__":
    unittest.main()
