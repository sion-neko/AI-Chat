"""GitHub（Issue・Pull Request とそのコメント）。

環境変数:
  GITHUB_TOKEN    必須。個人のアクセストークン（読み取り権限があればよい）
  GITHUB_SCOPE    検索範囲の絞り込み。GitHubの検索修飾子をそのまま書く（例: "org:my-org repo:me/tool"）
  GITHUB_API_URL  GitHub Enterprise Server のときだけ（例: https://github.example.com/api/v3）
"""

import os

from langchain_core.tools import tool

from app.sources import http
from app.sources.base import Source

API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
SEARCH_LIMIT = 20
COMMENT_LIMIT = 30


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, params: dict | None = None):
    return http.get_json(f"{API_URL}{path}", params, _headers())


def _date_range(since: str | None, until: str | None) -> str | None:
    if since and until:
        return f"{since}..{until}"
    if since:
        return f">={since}"
    if until:
        return f"<={until}"
    return None


@tool
def search_github(query: str, since: str | None = None, until: str | None = None, state: str | None = None) -> list[dict]:
    """GitHubのIssueとPull Requestを検索する（タイトル・本文・コメントが対象）。

    query: 検索語。空白区切りはAND。OR でつなぐとOR検索（例: "VPN OR 証明書"）
    since, until: 更新日の範囲（YYYY-MM-DD）。省略可
    state: "open" か "closed"。省略すると両方
    新しく更新された順に最大20件。本文は先頭だけなので、詳しくは get_github_issue で読む。
    """
    terms = [query, os.environ.get("GITHUB_SCOPE", "")]
    if updated := _date_range(since, until):
        terms.append(f"updated:{updated}")
    if state:
        terms.append(f"state:{state}")
    data = _get("/search/issues", {"q": " ".join(t for t in terms if t), "sort": "updated", "per_page": SEARCH_LIMIT})
    return [
        {
            "kind": "pull_request" if "pull_request" in item else "issue",
            "repo": item["repository_url"].rsplit("/repos/", 1)[-1],
            "number": item["number"],
            "title": item["title"],
            "state": item["state"],
            "author": (item.get("user") or {}).get("login"),
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
            "comments": item["comments"],
            "body": http.clip(item.get("body"), 300),
            "url": item["html_url"],
        }
        for item in data["items"]
    ]


@tool
def get_github_issue(repo: str, number: int) -> dict:
    """GitHubのIssueかPull Requestを1件、本文とコメントつきで読む。

    repo: "owner/name" の形式
    number: Issue・Pull Requestの番号
    コメントは古い順に最大30件。
    """
    issue = _get(f"/repos/{repo}/issues/{number}")
    comments = _get(f"/repos/{repo}/issues/{number}/comments", {"per_page": COMMENT_LIMIT})
    return {
        "kind": "pull_request" if "pull_request" in issue else "issue",
        "title": issue["title"],
        "state": issue["state"],
        "author": (issue.get("user") or {}).get("login"),
        "created_at": issue["created_at"],
        "closed_at": issue.get("closed_at"),
        "body": http.clip(issue.get("body"), 3000),
        "url": issue["html_url"],
        "comments": [
            {
                "author": (c.get("user") or {}).get("login"),
                "created_at": c["created_at"],
                "body": http.clip(c.get("body"), 1000),
                "url": c["html_url"],
            }
            for c in comments
        ],
    }


GITHUB = Source(
    id="github",
    label="GitHub",
    tools=[search_github, get_github_issue],
    configured=bool(os.environ.get("GITHUB_TOKEN")),
)
