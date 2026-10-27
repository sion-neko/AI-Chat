import unittest

from app import links


def report(*results):
    return {"steps": [{"result": r} for r in results]}


class LinksTest(unittest.TestCase):
    def test_extract(self):
        text = "経緯は [Issue](https://github.com/me/infra/issues/7) と https://example.invalid/chat/2。また https://example.invalid/chat/2 も"
        self.assertEqual(links.extract(text), ["https://github.com/me/infra/issues/7", "https://example.invalid/chat/2"])

    def test_verify(self):
        reports = [
            report('[{"url": "https://github.com/me/infra/issues/7#issuecomment-1"}]'),
            report('{"url": "https://zulip.example.com/#narrow/stream/5-ops/topic/x/near/12"}'),
        ]
        answer = (
            "- 完了: https://github.com/me/infra/issues/7\n"  # コメントへのリンクがあったページ
            "- 予定: https://zulip.example.com/#narrow/stream/5-ops/topic/x/near/12\n"
            "- 作られたURL: https://github.com/me/infra/issues/99\n"
            "- 別のメッセージ: https://zulip.example.com/#narrow/stream/5-ops/topic/x/near/13\n"
        )
        self.assertEqual(
            [(r["url"].rsplit("/", 1)[-1], r["found"]) for r in links.verify(answer, reports)],
            [("7", True), ("12", True), ("99", False), ("13", False)],
        )

    def test_member_report_is_not_evidence(self):
        # 所員の報告（LLMの文章）にしかないURLは根拠にしない
        reports = [{"steps": [{"result": "[]"}], "content": "https://example.invalid/made-up"}]
        self.assertEqual(links.verify("https://example.invalid/made-up", reports), [{"url": "https://example.invalid/made-up", "found": False}])


if __name__ == "__main__":
    unittest.main()
