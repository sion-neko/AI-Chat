import json
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from app.sources import zulip
from tests.fake_api import FakeApi


def message(id, day, content, **extra):
    ts = int(datetime.fromisoformat(f"{day}T03:00:00+00:00").timestamp())
    base = {"id": id, "sender_full_name": "山田", "timestamp": ts, "content": content, "type": "stream",
            "display_recipient": "infra.ops", "stream_id": 5, "subject": "ディスク (監視)"}
    return {**base, **extra}


MESSAGES = [
    message(1, "2026-09-20", "ディスク増設を検討"),
    message(2, "2026-10-01", "来週メンテで実施"),
    message(3, "2026-10-08", "完了しました", type="private", display_recipient=[{"email": "a@example.com"}]),
]


class ZulipTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi({"/api/v1/messages": {"messages": MESSAGES}}).__enter__()
        self.addCleanup(self.api.__exit__)
        env = {"ZULIP_URL": self.api.url, "ZULIP_EMAIL": "me@example.com", "ZULIP_API_KEY": "k3y"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)

    def test_search_filters_dates_newest_first(self):
        results = zulip.search_zulip.invoke({"query": "ディスク", "since": "2026-10-01", "channel": "infra.ops"})

        self.assertEqual([r["content"] for r in results], ["完了しました", "来週メンテで実施"])
        req = self.api.requests[0]
        self.assertEqual(
            json.loads(req["params"]["narrow"]),
            [{"operator": "search", "operand": "ディスク"}, {"operator": "stream", "operand": "infra.ops"}],
        )
        self.assertEqual(req["params"]["anchor"], "newest")
        self.assertEqual(req["headers"]["authorization"], "Basic bWVAZXhhbXBsZS5jb206azN5")

    def test_links(self):
        results = zulip.search_zulip.invoke({"query": "x"})
        dm, stream = results[0], results[1]
        self.assertEqual(dm["url"], f"{self.api.url}/#narrow/near/3")
        self.assertEqual(dm["channel"], "（DM）")
        self.assertEqual(
            stream["url"],
            f"{self.api.url}/#narrow/stream/5-infra.2Eops/topic/.E3.83.87.E3.82.A3.E3.82.B9.E3.82.AF.20.28.E7.9B.A3.E8.A6.96.29/near/2",
        )

    def test_topic_oldest_first(self):
        results = zulip.get_zulip_topic.invoke({"channel": "infra.ops", "topic": "ディスク (監視)"})
        self.assertEqual(results[0]["content"], "ディスク増設を検討")
        narrow = json.loads(self.api.requests[0]["params"]["narrow"])
        self.assertEqual(narrow[1], {"operator": "topic", "operand": "ディスク (監視)"})


if __name__ == "__main__":
    unittest.main()
