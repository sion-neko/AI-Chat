import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage

from app import db

with mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://unused.invalid/v1", "LLM_API_KEY": "x", "LLM_MODEL": "fake"}):
    from app import agent, turns


class FakeLLM:
    def __init__(self, text):
        self.text = text

    def invoke(self, messages):
        return AIMessage(self.text)


class TitleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(db, "DB_PATH", str(Path(tmp.name) / "chat.db"))
        patch.start()
        self.addCleanup(patch.stop)
        db.init()
        self.conv = db.create_conversation("あの件どうなった？")

    def answer(self, *added, llm="「監視サーバーのディスク増設」。\n補足"):
        db.append_turns(self.conv, list(added))
        with mock.patch.object(agent, "_llm", FakeLLM(llm)):
            turns.retitle(self.conv)
        return db.get_conversation(self.conv)["title"]

    def test_titles_after_first_answer_even_after_clarify(self):
        clarify = [{"role": "user", "content": "あの件どうなった？"}, {"role": "assistant", "kind": "clarify", "content": "どの件？"}]
        self.assertEqual(self.answer(*clarify), "あの件どうなった？")
        answered = [{"role": "user", "content": "ディスク増設"}, {"role": "assistant", "content": "増設しました"}]
        self.assertEqual(self.answer(*answered), "監視サーバーのディスク増設")

    def test_keeps_renamed_title_and_later_turns(self):
        db.update_conversation(self.conv, title="自分で付けた名前")
        self.assertEqual(self.answer({"role": "user", "content": "あの件どうなった？"}, {"role": "assistant", "content": "a"}), "自分で付けた名前")

    def test_failure_keeps_title(self):
        self.assertEqual(self.answer({"role": "user", "content": "あの件どうなった？"}, {"role": "assistant", "content": "a"}, llm="  "), "あの件どうなった？")


if __name__ == "__main__":
    unittest.main()
