import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import db

with mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://unused.invalid/v1", "LLM_API_KEY": "x", "LLM_MODEL": "fake"}):
    from app import web


class ConversationsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(db, "DB_PATH", str(Path(tmp.name) / "chat.db"))
        patch.start()
        self.addCleanup(patch.stop)
        db.init()
        self.disk = db.create_conversation("ディスク増設どうなった？")
        db.append_turns(self.disk, [{"role": "user", "content": "ディスク増設どうなった？"}, {"role": "assistant", "content": "NASを入れ替えました 100%_done"}])
        self.vpn = db.create_conversation("VPNの件")

    def titles(self, query="", archived=False):
        return [c["title"] for c in db.list_conversations(query, archived)]

    def test_search_title_and_content(self):
        self.assertEqual(self.titles("NAS"), ["ディスク増設どうなった？"])
        self.assertEqual(self.titles("VPN"), ["VPNの件"])
        self.assertEqual(self.titles("NAS VPN"), [])  # AND
        # LIKE の記号はそのまま文字として探す
        self.assertEqual(self.titles("100%_done"), ["ディスク増設どうなった？"])
        self.assertEqual(self.titles("%"), ["ディスク増設どうなった？"])

    def test_tags_and_archive(self):
        db.update_conversation(self.disk, tags=["監視", "ストレージ"])
        self.assertEqual(self.titles("tag:監視"), ["ディスク増設どうなった？"])
        self.assertEqual(self.titles("tag:監視 VPN"), [])
        self.assertEqual(db.all_tags(), ["ストレージ", "監視"])

        db.update_conversation(self.vpn, archived=True, title="VPN証明書")
        self.assertEqual(self.titles(), ["ディスク増設どうなった？"])
        self.assertEqual(self.titles(archived=True), ["VPN証明書"])
        self.assertFalse(db.update_conversation("nope", title="x"))

    def test_old_db_gets_new_columns(self):
        with db.connect() as conn:
            conn.executescript("DROP TABLE turns; DROP TABLE traces; DROP TABLE conversations;"
                               "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
                               "INSERT INTO conversations VALUES ('a', '古い会話', 'x', 'x');")
        db.init()
        self.assertEqual(db.list_conversations(), [{"id": "a", "title": "古い会話", "updated_at": "x", "archived": False, "tags": []}])

    def test_replace_turns(self):
        db.append_turns(self.disk, [{"role": "user", "content": "その後は？"}, {"role": "assistant", "content": "古い回答", "trace_id": db.save_trace(self.disk, [{"who": "x"}])}])
        old_trace = db.load_turns(self.disk)[3]["trace_id"]
        db.replace_turns(self.disk, 2, [{"role": "user", "content": "その後どうなった？"}, {"role": "assistant", "content": "新しい回答"}])
        self.assertEqual([t["content"] for t in db.load_turns(self.disk)], ["ディスク増設どうなった？", "NASを入れ替えました 100%_done", "その後どうなった？", "新しい回答"])
        self.assertIsNone(db.load_trace(old_trace))

    def test_validation(self):
        self.assertEqual(web.conversation_changes({"tags": [" 監視 ", "tag:監視", "", "NAS"]}), {"tags": ["監視", "NAS"]})
        for bad in ({"title": "  "}, {"tags": "監視"}, {"tags": ["a b"]}, {"tags": ["あ" * 21]}, {"tags": [str(i) for i in range(11)]}):
            with self.assertRaises(web.InputError):
                web.conversation_changes(bad)


if __name__ == "__main__":
    unittest.main()
