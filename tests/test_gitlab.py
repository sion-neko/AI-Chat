import os
import unittest
from unittest import mock

from app.sources import gitlab
from tests.fake_api import FakeApi


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
        "web_url": f"https://gitlab.example.com/{ref}",
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

    def test_get_item_with_notes(self):
        notes = [{"author": {"username": "sato"}, "created_at": "2026-10-09T00:00:00Z", "system": True, "body": "merged"}]
        self.serve({"/api/v4/projects/infra%2Ftools/merge_requests/4": MR, "/api/v4/projects/infra%2Ftools/merge_requests/4/notes": notes})
        result = gitlab.get_gitlab_item.invoke({"project": "infra/tools", "iid": 4, "kind": "merge_request"})
        self.assertEqual(result["comments"][0]["body"], "merged")
        self.assertTrue(result["comments"][0]["system"])


if __name__ == "__main__":
    unittest.main()
