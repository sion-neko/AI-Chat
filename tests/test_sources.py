import dataclasses
import unittest
from unittest import mock

from app import sources
from app.sources import dummy, github


class SampleSourcesTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
