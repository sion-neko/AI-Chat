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

    def test_rename_and_archive(self):
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
        self.assertEqual(db.list_conversations(), [{"id": "a", "title": "古い会話", "updated_at": "x", "archived": False, "folder_id": None}])

    def test_replace_turns(self):
        db.append_turns(self.disk, [{"role": "user", "content": "その後は？"}, {"role": "assistant", "content": "古い回答", "trace_id": db.save_trace(self.disk, [{"who": "x"}])}])
        old_trace = db.load_turns(self.disk)[3]["trace_id"]
        db.replace_turns(self.disk, 2, [{"role": "user", "content": "その後どうなった？"}, {"role": "assistant", "content": "新しい回答"}])
        self.assertEqual([t["content"] for t in db.load_turns(self.disk)], ["ディスク増設どうなった？", "NASを入れ替えました 100%_done", "その後どうなった？", "新しい回答"])
        self.assertIsNone(db.load_trace(old_trace))

    def test_folders(self):
        folder = db.create_folder("案件X")
        db.update_conversation(self.disk, folder_id=folder)
        self.assertEqual(self.titles_in(folder), ["ディスク増設どうなった？"])
        self.assertEqual([(f["name"], f["count"]) for f in db.list_folders()], [("案件X", 1)])

        db.update_folder(folder, memo="監視サーバー＝mon01", selection={"unchecked": ["dummy_wiki"], "scopes": {"github": ["me/infra"]}})
        self.assertEqual(db.get_folder(folder)["selection"]["scopes"], {"github": ["me/infra"]})
        self.assertEqual(web.memos_for(self.disk, None)["folder"], {"name": "案件X", "memo": "監視サーバー＝mon01"})

        # フォルダから出す
        db.update_conversation(self.disk, folder_id=None)
        self.assertEqual(self.titles_in(folder), [])
        # フォルダを消しても、中の調査は消えずにフォルダの外に出る
        db.update_conversation(self.vpn, folder_id=folder)
        db.delete_folder(folder)
        self.assertEqual(db.get_conversation(self.vpn)["folder_id"], None)
        self.assertEqual(len(self.titles()), 2)

    def titles_in(self, folder_id):
        return [c["title"] for c in db.list_conversations(folder_id=folder_id)]

    def test_settings(self):
        self.assertEqual(web.settings(), {"memo": "", "selection": {}})
        db.set_setting("memo", "回答は箇条書きで")
        self.assertEqual(web.memos_for(None, None), {"global": "回答は箇条書きで", "folder": None})

    def test_add_to_memo(self):
        folder = db.create_folder("案件X")
        db.update_conversation(self.disk, folder_id=folder)
        db.update_folder(folder, memo="- 既存のメモ")
        added = web.add_to_memo(self.disk, [{"scope": "folder", "note": "監視サーバーは mon01"}, {"scope": "global", "note": "結論から書く"}])
        self.assertEqual(added, [
            {"scope": "folder", "note": "監視サーバーは mon01", "folder": "案件X", "saved": True},
            {"scope": "global", "note": "結論から書く", "folder": None, "saved": True},
        ])
        self.assertEqual(db.get_folder(folder)["memo"], "- 既存のメモ\n- 監視サーバーは mon01")
        self.assertEqual(db.get_setting("memo"), "- 結論から書く")

        # 上限を超えるものは書かない。フォルダに入っていない調査のフォルダ向けメモは捨てる
        db.set_setting("memo", "あ" * (web.MEMO_LENGTH - 3))
        added = web.add_to_memo(self.vpn, [{"scope": "global", "note": "長すぎる"}, {"scope": "folder", "note": "x"}])
        self.assertEqual(added, [{"scope": "global", "note": "長すぎる", "folder": None, "saved": False}])
        self.assertEqual(len(db.get_setting("memo")), web.MEMO_LENGTH - 3)

    def test_folder_validation(self):
        self.assertEqual(web.folder_changes({"name": " 案件X ", "memo": " メモ "}), {"name": "案件X", "memo": "メモ"})
        self.assertEqual(web.folder_changes({"selection": {"unchecked": ["a"], "scopes": {"github": [], "gitlab": ["x/y"]}}}), {"selection": {"unchecked": ["a"], "scopes": {"gitlab": ["x/y"]}}})
        for bad in ({"name": " "}, {"memo": "あ" * (web.MEMO_LENGTH + 1)}, {"selection": []}, {"selection": {"unchecked": "a"}}, {"selection": {"scopes": {"g": "x"}}}):
            with self.assertRaises(web.InputError):
                web.folder_changes(bad)
        with self.assertRaises(web.InputError):
            web.conversation_changes({"folder_id": "nope"})

    def test_followup_since(self):
        conv = {"updated_at": "2026-10-20T03:00:00+00:00"}
        asked = {"role": "user", "content": "ディスク増設どうなった？"}
        self.assertEqual(web.followup_since([asked, {"role": "assistant", "content": "a", "date": "2026-10-22"}], conv), "2026-10-22")
        # 日付を持つ前の回答は、会話の更新日から
        self.assertEqual(web.followup_since([asked, {"role": "assistant", "content": "a"}], conv), "2026-10-20")
        for history in ([], [asked, {"role": "assistant", "kind": "clarify", "content": "どの件？"}]):
            with self.assertRaises(web.InputError):
                web.followup_since(history, conv)

    def test_validation(self):
        self.assertEqual(web.conversation_changes({"title": " VPN証明書 ", "archived": 1}), {"title": "VPN証明書", "archived": True})
        with self.assertRaises(web.InputError):
            web.conversation_changes({"title": "  "})


if __name__ == "__main__":
    unittest.main()
