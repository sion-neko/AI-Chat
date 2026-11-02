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
    def load(self, servers: dict) -> list[sources.Source]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "mcp.json"
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        with mock.patch.object(mcp_servers, "CONFIG_PATH", str(path)):
            return mcp_servers.load()

    def test_no_config(self):
        with mock.patch.object(mcp_servers, "CONFIG_PATH", "/nonexistent/mcp.json"):
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

    def test_does_not_pass_app_secrets_to_stdio_server(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}):
            conn = mcp_servers._connection({"command": "x", "env": {"NOTES_DIR": "/data"}})
        self.assertNotIn("GITHUB_TOKEN", conn["env"])
        self.assertEqual(conn["env"]["NOTES_DIR"], "/data")
        self.assertIn("PATH", conn["env"])


if __name__ == "__main__":
    unittest.main()
