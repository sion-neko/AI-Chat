import dataclasses
import unittest
from unittest import mock

from app import sources
from app.sources import dummy, github, mcp_servers


class SampleSourcesTest(unittest.TestCase):
    def setUp(self):
        # 手元に設定したMCPサーバーがあっても、本物のソースとして数えないように
        patch = mock.patch.object(mcp_servers, "_loaded", {})
        patch.start()
        self.addCleanup(patch.stop)

    def ids(self):
        return [s["id"] for s in sources.describe()]

    def test_samples_only_without_real_sources(self):
        with mock.patch.object(sources, "BUILTIN", [dummy.CHAT, dummy.WIKI, dataclasses.replace(github.GITHUB, configured=False)]):
            self.assertTrue(sources.demo_mode())
            self.assertEqual(self.ids(), ["dummy_chat", "dummy_wiki"])
        with mock.patch.object(sources, "BUILTIN", [dummy.CHAT, dummy.WIKI, dataclasses.replace(github.GITHUB, configured=True)]):
            self.assertFalse(sources.demo_mode())
            self.assertEqual(self.ids(), ["github"])
            # 架空の話が回答に混ざらないよう、サンプルは選んでも使えない
            with self.assertRaises(sources.SourceError):
                sources.resolve(["dummy_chat"])

    def test_check_states(self):
        def broken():
            raise sources.SourceError("HTTP 401 Unauthorized")

        cases = [
            (dummy.CHAT, "ok", "確認不要"),
            (dataclasses.replace(github.GITHUB, configured=False), "unset", "GITHUB_TOKEN"),
            (dataclasses.replace(github.GITHUB, configured=True, check=lambda: "me としてつながりました"), "ok", "me として"),
            (dataclasses.replace(github.GITHUB, configured=True, check=broken), "error", "401"),
            # MCPサーバーは .env ではなく画面で直すので、つながらなかった理由をそのまま出す
            (sources.Source(id="mcp_wiki", label="Wiki", tools=[], configured=False, requires="つながりませんでした"), "error", "つながりませんでした"),
        ]
        for source, state, message in cases:
            result = sources.check(source)
            self.assertEqual(result["state"], state, source.id)
            self.assertIn(message, result["message"])

    def test_scopes(self):
        scoped = dataclasses.replace(github.GITHUB, configured=True, scopes=lambda: ["me/infra", "me/app"])
        with mock.patch.object(sources, "BUILTIN", [scoped]):
            self.assertEqual(sources.scope_choices("github"), ["me/infra", "me/app"])
            # 選んでいないソースの分と、形の正しくない分は捨てる
            picked = {"github": ["me/infra"], "zulip": ["general"]}
            self.assertEqual(sources.resolve_scopes([scoped], picked), {"github": ["me/infra"]})
            self.assertEqual(sources.resolve_scopes([scoped], {"github": "me/infra"}), {})
        with mock.patch.object(sources, "BUILTIN", [dummy.CHAT]), self.assertRaisesRegex(sources.SourceError, "範囲を選べない"):
            sources.scope_choices("dummy_chat")


if __name__ == "__main__":
    unittest.main()
