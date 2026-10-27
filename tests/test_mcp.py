import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import sources
from app.sources import mcp_servers

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


class McpTest(unittest.TestCase):
    def setUp(self):
        # 設定ファイルと、つないだソースの一覧は、テストごとに分ける
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "mcp.json"
        for patch in (mock.patch.object(mcp_servers, "CONFIG_PATH", str(self.path)), mock.patch.object(mcp_servers, "_loaded", {})):
            patch.start()
            self.addCleanup(patch.stop)

    def load(self, servers: dict) -> list[sources.Source]:
        self.path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        return mcp_servers.load()

    def test_no_config(self):
        self.assertEqual(mcp_servers.load(), [])

    def test_stdio_server_becomes_source(self):
        [source] = self.load({"my-notes": {"label": "メモ", "command": sys.executable, "args": [SERVER]}})
        self.assertEqual((source.id, source.label, source.configured), ("mcp_my_notes", "メモ", True))
        tools = {t.name: t for t in source.tools}

        # 所員のツールループと同じく、同期で呼べる
        result = tools["search_notes"].invoke({"query": "NAS"})
        self.assertIn("バックアップ先のNASを入れ替えた", result)
        self.assertIn("https://example.invalid/notes/2", result)
        self.assertIn("壊れています", tools["broken"].invoke({"query": "x"}))
        self.assertEqual(sources.check(source)["state"], "ok")

    def test_unreachable_server(self):
        [source] = self.load({"wiki": {"url": "http://127.0.0.1:9/mcp"}})
        self.assertFalse(source.configured)
        self.assertIn("つながりませんでした", source.requires)
        self.assertEqual(sources.check(source)["state"], "error")

    def test_does_not_pass_app_secrets_to_stdio_server(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}):
            conn = mcp_servers._connection({"command": "x", "env": {"NOTES_DIR": "/data"}})
        self.assertNotIn("GITHUB_TOKEN", conn["env"])
        self.assertEqual(conn["env"]["NOTES_DIR"], "/data")
        self.assertIn("PATH", conn["env"])

    def test_add_edit_delete_without_restart(self):
        self.path.write_text(json.dumps({"other": "残す", "mcpServers": {}}), encoding="utf-8")
        key = mcp_servers.new_key("Notes メモ")
        self.assertEqual(key, "notes")
        source = mcp_servers.save(key, {"label": "メモ", "command": sys.executable, "args": [SERVER]})
        self.assertTrue(source.configured)
        # すぐにソースとして使える
        self.assertIn("mcp_notes", [s["id"] for s in sources.describe()])
        [entry] = mcp_servers.listing()
        self.assertEqual((entry["key"], entry["connected"], entry["tools"]), ("notes", True, ["search_notes", "broken"]))

        # 同じ表示名でも、キーは重ならない
        self.assertEqual(mcp_servers.new_key("Notes"), "notes_2")

        # つながらない設定に変えても、設定は残り、理由が分かる
        mcp_servers.save(key, {"label": "メモ", "url": "http://127.0.0.1:9/mcp", "type": "sse"})
        [entry] = mcp_servers.listing()
        self.assertFalse(entry["connected"])
        self.assertIn("つながりませんでした", entry["error"])
        self.assertNotIn("mcp_notes", [s["id"] for s in sources.describe()])

        mcp_servers.delete(key)
        self.assertEqual(mcp_servers.listing(), [])
        # mcpServers 以外の項目は残す
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"other": "残す", "mcpServers": {}})

    def test_normalize(self):
        self.assertEqual(
            mcp_servers.normalize({"label": " Wiki ", "url": "https://wiki.example.com/mcp", "headers": {"Authorization": "Bearer x", " ": "y"}}),
            {"label": "Wiki", "url": "https://wiki.example.com/mcp", "headers": {"Authorization": "Bearer x"}},
        )
        self.assertEqual(
            mcp_servers.normalize({"label": "メモ", "command": "python", "args": ["s.py"], "env": {}}),
            {"label": "メモ", "command": "python", "args": ["s.py"]},
        )
        for bad in (
            {"label": "", "url": "https://x"},
            {"label": "x"},
            {"label": "x", "url": "https://x", "command": "y"},
            {"label": "x", "url": "ftp://x"},
            {"label": "x", "command": "y", "args": "s.py"},
            {"label": "x", "url": "https://x", "headers": {"a": 1}},
        ):
            with self.assertRaises(mcp_servers.McpConfigError, msg=bad):
                mcp_servers.normalize(bad)


if __name__ == "__main__":
    unittest.main()
