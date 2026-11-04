import os
import unittest
from unittest import mock

from app.sources import gitlab, scope
from tests.fake_api import FakeApi, Raw


def item(iid, ref, updated_at):
    return {
        "iid": iid,
        "references": {"full": ref},
        "title": "VPN証明書の更新",
        "state": "closed",
        "author": {"username": "suzuki"},
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": updated_at,
        "description": "手順",
        "web_url": "https://gitlab.example.com/" + ref.replace("#", "/-/issues/").replace("!", "/-/merge_requests/"),
    }


ISSUE = item(3, "infra/tools#3", "2026-10-05T00:00:00Z")
MR = item(4, "infra/tools!4", "2026-10-09T00:00:00Z")


class GitLabTest(unittest.TestCase):
    def serve(self, routes, **env):
        api = FakeApi(routes).__enter__()
        self.addCleanup(api.__exit__)
        patch = mock.patch.dict(os.environ, {"GITLAB_URL": api.url + "/", "GITLAB_TOKEN": "t0ken", **env})
        patch.start()
        self.addCleanup(patch.stop)
        return api

    def test_search_merges_issues_and_mrs(self):
        api = self.serve({"/api/v4/issues": [ISSUE], "/api/v4/merge_requests": [MR]})
        results = gitlab.search_gitlab.invoke({"query": "VPN", "since": "2026-10-01"})

        self.assertEqual([r["ref"] for r in results], ["infra/tools!4", "infra/tools#3"])
        self.assertEqual([r["kind"] for r in results], ["merge_request", "issue"])
        params = api.requests[0]["params"]
        self.assertEqual(params["scope"], "all")
        self.assertEqual(params["updated_after"], "2026-10-01T00:00:00Z")
        self.assertNotIn("updated_before", params)
        self.assertEqual(api.requests[0]["headers"]["private-token"], "t0ken")

    def test_search_in_group(self):
        api = self.serve({"/api/v4/groups/infra%2Ftools/merge_requests": [MR]}, GITLAB_GROUP="infra/tools")
        gitlab.search_gitlab.invoke({"query": "VPN", "state": "merged"})
        # merged は Merge Request だけなので Issue は検索しない
        self.assertEqual([r["path"] for r in api.requests], ["/api/v4/groups/infra%2Ftools/merge_requests"])
        self.assertNotIn("scope", api.requests[0]["params"])

    def test_selected_projects(self):
        api = self.serve({
            "/api/v4/projects/infra%2Ftools/issues": [ISSUE],
            "/api/v4/projects/infra%2Ftools/merge_requests": [],
            "/api/v4/projects/infra%2Fweb/issues": [],
            "/api/v4/projects/infra%2Fweb/merge_requests": [MR],
        }, GITLAB_GROUP="ignored")
        scope.set(["infra/tools", "infra/web"])
        self.addCleanup(scope.set, None)
        results = gitlab.search_gitlab.invoke({"query": "VPN"})
        self.assertEqual([r["ref"] for r in results], ["infra/tools!4", "infra/tools#3"])
        self.assertEqual(len(api.requests), 4)
        self.assertNotIn("scope", api.requests[0]["params"])

    def test_scopes(self):
        api = self.serve({"/api/v4/projects": [{"path_with_namespace": "infra/tools"}]})
        self.assertEqual(gitlab.scopes(), ["infra/tools"])
        self.assertEqual(api.requests[0]["params"]["membership"], "true")

    def test_relative_uploads_become_absolute(self):
        issue = {**ISSUE, "description": "画面です ![graph](/uploads/0123456789abcdef0123456789abcdef/graph.png)"}
        notes = [{"author": {"username": "sato"}, "created_at": "2026-10-09T00:00:00Z", "body": "![x](/uploads/0123456789abcdef0123456789abcdef/x.png)"}]
        self.serve({"/api/v4/projects/infra%2Ftools/issues/3": issue, "/api/v4/projects/infra%2Ftools/issues/3/notes": notes})
        result = gitlab.get_gitlab_item.invoke({"project": "infra/tools", "iid": 3, "kind": "issue"})
        self.assertIn("(https://gitlab.example.com/infra/tools/uploads/0123456789abcdef0123456789abcdef/graph.png)", result["body"])
        self.assertIn("(https://gitlab.example.com/infra/tools/uploads/", result["comments"][0]["body"])

    def test_fetch_image_through_api(self):
        api = self.serve({"/api/v4/projects/infra%2Ftools/uploads/0123456789abcdef0123456789abcdef/graph.png": Raw(b"\x89PNG", "image/png")})
        for url in (
            f"{api.url}/infra/tools/uploads/0123456789abcdef0123456789abcdef/graph.png",
            f"{api.url}/infra/tools/-/uploads/0123456789abcdef0123456789abcdef/graph.png",
        ):
            self.assertEqual(gitlab.fetch_image(url), (b"\x89PNG", "image/png"))
        self.assertEqual(api.requests[0]["headers"]["private-token"], "t0ken")
        with self.assertRaisesRegex(Exception, "uploads"):
            gitlab.fetch_image("https://elsewhere.example.com/infra/tools/uploads/0123456789abcdef0123456789abcdef/x.png")

    def test_get_item_with_notes(self):
        notes = [{"author": {"username": "sato"}, "created_at": "2026-10-09T00:00:00Z", "system": True, "body": "merged"}]
        self.serve({"/api/v4/projects/infra%2Ftools/merge_requests/4": MR, "/api/v4/projects/infra%2Ftools/merge_requests/4/notes": notes})
        result = gitlab.get_gitlab_item.invoke({"project": "infra/tools", "iid": 4, "kind": "merge_request"})
        self.assertEqual(result["comments"][0]["body"], "merged")
        self.assertTrue(result["comments"][0]["system"])


if __name__ == "__main__":
    unittest.main()
