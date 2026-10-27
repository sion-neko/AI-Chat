import os
import unittest
from unittest import mock

from app import sources
from app.sources import github, scope
from app.sources.http import ApiError
from tests.fake_api import FakeApi, Raw

ISSUE = {
    "number": 7,
    "title": "VPN証明書の更新",
    "state": "closed",
    "user": {"login": "suzuki"},
    "created_at": "2026-10-01T00:00:00Z",
    "updated_at": "2026-10-10T00:00:00Z",
    "closed_at": "2026-10-10T00:00:00Z",
    "comments": 1,
    "body": "手順をまとめる" + "あ" * 400,
    "html_url": "https://github.com/me/infra/issues/7",
    "repository_url": "https://api.github.com/repos/me/infra",
}
COMMENT = {"user": {"login": "sato"}, "created_at": "2026-10-10T00:00:00Z", "body": "更新しました", "html_url": "https://github.com/me/infra/issues/7#issuecomment-1"}


class GitHubTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t0ken", "GITHUB_SCOPE": "org:me"})
        env.start()
        self.addCleanup(env.stop)

    def serve(self, routes):
        api = FakeApi(routes).__enter__()
        self.addCleanup(api.__exit__)
        patch = mock.patch.object(github, "API_URL", api.url)
        patch.start()
        self.addCleanup(patch.stop)
        return api

    def test_search_builds_query_and_summarizes(self):
        api = self.serve({"/search/issues": {"items": [ISSUE, {**ISSUE, "number": 8, "pull_request": {}}]}})
        results = github.search_github.invoke({"query": "VPN 証明書", "since": "2026-10-01", "state": "closed"})

        req = api.requests[0]
        self.assertEqual(req["params"]["q"], "VPN 証明書 org:me updated:>=2026-10-01 state:closed")
        self.assertEqual(req["headers"]["authorization"], "Bearer t0ken")
        self.assertEqual([r["kind"] for r in results], ["issue", "pull_request"])
        self.assertEqual(results[0]["repo"], "me/infra")
        self.assertTrue(results[0]["body"].endswith("…（省略）"))

    def test_date_range(self):
        api = self.serve({"/search/issues": {"items": []}})
        github.search_github.invoke({"query": "x", "since": "2026-09-01", "until": "2026-09-30"})
        self.assertIn("updated:2026-09-01..2026-09-30", api.requests[0]["params"]["q"])

    def test_get_issue_with_comments(self):
        self.serve({"/repos/me/infra/issues/7": ISSUE, "/repos/me/infra/issues/7/comments": [COMMENT]})
        issue = github.get_github_issue.invoke({"repo": "me/infra", "number": 7})
        self.assertEqual(issue["comments"][0]["body"], "更新しました")
        self.assertEqual(issue["url"], ISSUE["html_url"])

    def test_api_error_is_readable(self):
        self.serve({"/search/issues": (422, {"message": "Validation Failed"})})
        with self.assertRaisesRegex(ApiError, "HTTP 422.*Validation Failed"):
            github.search_github.invoke({"query": "x"})

    def test_selected_repos_replace_env_scope(self):
        api = self.serve({"/search/issues": {"items": []}})
        scope.set(["me/infra", "me/tools"])
        self.addCleanup(scope.set, None)
        github.search_github.invoke({"query": "VPN"})
        self.assertEqual(api.requests[0]["params"]["q"], "VPN repo:me/infra repo:me/tools")

    def test_scopes(self):
        api = self.serve({"/user/repos": [{"full_name": "me/infra"}, {"full_name": "org/app"}]})
        self.assertEqual(github.scopes(), ["me/infra", "org/app"])
        self.assertEqual(api.requests[0]["params"]["sort"], "pushed")

    def test_fetch_image_does_not_send_token_to_redirect_target(self):
        storage = FakeApi({"/signed/x.png": Raw(b"\x89PNG", "image/png")}).__enter__()
        self.addCleanup(storage.__exit__)
        api = self.serve({"/user-attachments/assets/abc": Raw(status=302, headers={"Location": f"{storage.url}/signed/x.png"})})

        data, mime = github.fetch_image(f"{api.url}/user-attachments/assets/abc")
        self.assertEqual((data, mime), (b"\x89PNG", "image/png"))
        self.assertEqual(api.requests[0]["headers"]["authorization"], "Bearer t0ken")
        self.assertNotIn("authorization", storage.requests[0]["headers"])

    def test_fetch_image_only_from_github(self):
        self.serve({})
        with self.assertRaisesRegex(ApiError, "GitHubに貼られた画像"):
            github.fetch_image("https://evil.example.com/x.png")

    def test_fetch_image_rejects_non_images(self):
        api = self.serve({"/user-attachments/assets/abc": Raw(b"<html>", "text/html")})
        with self.assertRaisesRegex(ApiError, "画像ではありません"):
            github.fetch_image(f"{api.url}/user-attachments/assets/abc")

    def test_check(self):
        self.serve({"/user": {"login": "sion"}})
        self.assertEqual(sources.check(github.GITHUB), {"state": "ok", "message": "sion としてつながりました"})

    def test_check_error(self):
        self.serve({"/user": (401, {"message": "Bad credentials"})})
        result = sources.check(github.GITHUB)
        self.assertEqual(result["state"], "error")
        self.assertIn("Bad credentials", result["message"])


if __name__ == "__main__":
    unittest.main()
