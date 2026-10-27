import json
import unittest

from app import links


def report(*results, label="GitHub"):
    return {"label": label, "steps": [{"result": r} for r in results]}


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
        reports = [{"label": "x", "steps": [{"result": "[]"}], "content": "https://example.invalid/made-up"}]
        self.assertEqual(links.verify("https://example.invalid/made-up", reports), [{"url": "https://example.invalid/made-up", "found": False}])

    def test_titles_from_each_source(self):
        github_issue = json.dumps({
            "title": "VPN証明書の更新", "url": "https://github.com/me/infra/issues/7", "updated_at": "2026-10-10T00:00:00Z",
            "comments": [{"body": "更新しました", "url": "https://github.com/me/infra/issues/7#issuecomment-1", "created_at": "2026-10-11T00:00:00Z"}],
        }, ensure_ascii=False)
        zulip = json.dumps([{"channel": "infra", "topic": "ディスク", "content": "完了", "date": "2026-10-08T03:00:00+00:00", "url": "https://zulip.example.com/#narrow/near/12"}], ensure_ascii=False)
        mcp_text = "バックアップ先を変えた https://example.invalid/notes/1"
        reports = [report(github_issue), report(zulip, label="Zulip"), report(mcp_text, label="メモ")]
        answer = "https://github.com/me/infra/issues/7#issuecomment-1 https://zulip.example.com/#narrow/near/12 https://example.invalid/notes/1 https://github.com/me/infra/issues/7"
        self.assertEqual(
            [(r["title"], r["date"], r["source"]) for r in links.verify(answer, reports)],
            [
                ("VPN証明書の更新 のコメント", "2026-10-11", "GitHub"),
                ("infra > ディスク", "2026-10-08", "Zulip"),
                ("", "", "メモ"),
                ("VPN証明書の更新", "2026-10-10", "GitHub"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
