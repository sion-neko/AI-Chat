import unittest

from app import jobs


class JobsTest(unittest.TestCase):
    def tearDown(self):
        for job in jobs.running():
            jobs.publish(job, {"type": "cancelled"})

    def drain(self, q):
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return events

    def test_late_subscriber_gets_replay_then_live(self):
        job = jobs.start("c1", {"question": "VPN"})
        jobs.publish(job, {"type": "delta", "text": "途中"})
        q = jobs.subscribe()
        self.addCleanup(jobs.unsubscribe, q)
        self.assertEqual([e["type"] for e in self.drain(q)], ["job", "delta", "ready"])
        jobs.publish(job, {"type": "done"})
        self.assertEqual(self.drain(q), [{"turn_id": job.turn_id, "type": "done"}])
        # 終わった調査は流し直さない
        q2 = jobs.subscribe()
        self.addCleanup(jobs.unsubscribe, q2)
        self.assertEqual(self.drain(q2), [{"type": "ready"}])

    def test_one_job_per_conversation(self):
        job = jobs.start("c1", {})
        with self.assertRaises(jobs.Busy):
            jobs.start("c1", {})
        jobs.start("c2", {})  # 別の会話は並行して調べられる
        jobs.publish(job, {"type": "error", "message": "x"})
        jobs.start("c1", {})  # 終われば次を始められる


if __name__ == "__main__":
    unittest.main()
