"""質問の受け付けから保存までのテスト（app/turns.py）。LLMは tests/test_agent.py の FakeLLM。"""

import os
import queue
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

with mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://unused.invalid/v1", "LLM_API_KEY": "x", "LLM_MODEL": "fake"}):
    from app import agent, db, jobs, turns
    from app.errors import InputError
    from app.sources import dummy

from tests.test_agent import FakeLLM

PNG = ("image/png", b"\x89PNG\r\n\x1a\n")


def wait_for_work_threads(test: unittest.TestCase):
    """テストの後片付けで、調査のスレッドが終わるまで待つ。

    調査のスレッドは done を配ったあとも題名付けでDBを触るので、待たずに一時DBを消すとエラーが出る。
    """
    started = []

    class Recorded(threading.Thread):
        def start(self):
            started.append(self)
            super().start()

    # threading モジュールそのものではなく、turns.py から見える分だけを差し替える（サーバーのスレッドは待たない）
    patch = mock.patch.object(turns, "threading", types.SimpleNamespace(Thread=Recorded))
    patch.start()
    test.addCleanup(lambda: [t.join(10) for t in started])
    test.addCleanup(patch.stop)


class Broken(FakeLLM):
    def invoke(self, messages):
        raise RuntimeError("LLMが落ちた")


class TurnsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (
            mock.patch.object(db, "DB_PATH", str(Path(tmp.name) / "chat.db")),
            mock.patch.object(agent, "_llm", FakeLLM([])),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        db.init()
        wait_for_work_threads(self)
        self.events = jobs.subscribe()
        self.addCleanup(jobs.unsubscribe, self.events)

    def finish(self, job: jobs.Job) -> dict:
        """調査が終わるのを待ち、最後のイベント（done / cancelled / error）を返す。"""
        while True:
            event = self.events.get(timeout=10)
            if event.get("turn_id") == job.turn_id and event["type"] in ("done", "cancelled", "error"):
                return event

    def question(self, content="ディスク増設どうなった？", **fields) -> turns.Question:
        return turns.Question(content=content, sources=fields.pop("sources", [dummy.CHAT]), **fields)

    def test_new_conversation_is_saved_and_marked_unread(self):
        job = turns.ask_new(self.question(), None)
        done = self.finish(job)
        self.assertEqual(done["type"], "done")
        saved = db.load_turns(job.conversation_id)
        self.assertEqual([(t["role"], t["content"]) for t in saved], [("user", "ディスク増設どうなった？"), ("assistant", "回答")])
        self.assertIn("trace_id", saved[1])
        self.assertNotIn("trace", saved[1])
        self.assertTrue(db.get_conversation(job.conversation_id)["unread"])

    def test_failure_removes_new_conversation_but_keeps_old_one(self):
        job = turns.ask_new(self.question(), None)
        self.finish(job)
        conv_id = job.conversation_id
        # 失敗したときに出すトレースバックは、テストの出力では黙らせる
        with mock.patch.object(agent, "_llm", Broken([])), mock.patch.object(turns.traceback, "print_exc"):
            failed = self.finish(turns.ask_new(self.question(), None))
            self.assertEqual((failed["type"], failed["deleted"]), ("error", True))
            self.assertIsNone(db.get_conversation(failed["conversation_id"]))
            # 続きの質問が失敗しても、それまでの会話は残る（何も足さない）
            failed = self.finish(turns.ask(conv_id, self.question("その後は？")))
        self.assertEqual((failed["type"], failed["deleted"]), ("error", False))
        self.assertEqual(len(db.load_turns(conv_id)), 2)

    def test_redo_replaces_turns_and_reuses_images(self):
        job = turns.ask_new(self.question(images=[PNG]), None)
        self.finish(job)
        conv_id = job.conversation_id
        first = db.load_turns(conv_id)[0]
        self.assertEqual(len(first["images"]), 1)
        self.assertTrue(first["image_text"])
        self.finish(turns.ask(conv_id, self.question("その後は？")))

        # 最初の質問からやり直す。画像を送り直さなければ、元の画像を（保存し直さずに）使う
        done = self.finish(turns.ask(conv_id, self.question("ディスク増設は終わった？"), from_turn=0))
        self.assertEqual(done["question"]["images"], first["images"])
        saved = db.load_turns(conv_id)
        self.assertEqual([t["content"] for t in saved], ["ディスク増設は終わった？", "回答"])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)

    def test_followup_records_since(self):
        job = turns.ask_new(self.question(), None)
        self.finish(job)
        answered = db.load_turns(job.conversation_id)[1]
        done = self.finish(turns.ask(job.conversation_id, self.question("その後どうなった？"), followup=True))
        self.assertEqual(done["question"]["since"], answered.get("date") or db.get_conversation(job.conversation_id)["updated_at"][:10])

    def test_cancel_conversation_stops_its_job(self):
        started = queue.Queue()

        class Waiting(FakeLLM):
            def invoke(self, messages):
                started.put(True)
                job.cancel.wait(10)
                return super().invoke(messages)

        with mock.patch.object(agent, "_llm", Waiting([])):
            job = turns.ask_new(self.question(), None)
            started.get(timeout=10)
            turns.cancel_conversation(job.conversation_id)
            cancelled = self.finish(job)
        self.assertEqual((cancelled["type"], cancelled["deleted"]), ("cancelled", True))
        self.assertIsNone(db.get_conversation(job.conversation_id))

    def test_redo_position_must_be_a_question(self):
        job = turns.ask_new(self.question(), None)
        self.finish(job)
        for bad in (1, 2, -1, "0"):
            with self.assertRaises(InputError):
                turns.ask(job.conversation_id, self.question(), from_turn=bad)


if __name__ == "__main__":
    unittest.main()
