"""GitLab（Issue・Merge Request とそのコメント）。セルフホストのGitLabを想定。

環境変数:
  GITLAB_URL    必須。GitLabのURL（例: https://gitlab.example.com）
  GITLAB_TOKEN  必須。個人のアクセストークン（read_api 権限があればよい）
  GITLAB_GROUP  検索範囲をグループに絞る（例: infra または infra/tools）。省略するとトークンで見られる全体
"""

import os
import urllib.parse
from typing import Literal

from langchain_core.tools import tool

from app.sources import http, scope
from app.sources.base import Source

SEARCH_LIMIT = 20
NOTE_LIMIT = 30
KINDS = {"issue": "issues", "merge_request": "merge_requests"}


def _get(path: str, params: dict | None = None):
    url = f"{os.environ['GITLAB_URL'].rstrip('/')}/api/v4{path}"
    return http.get_json(url, params, {"PRIVATE-TOKEN": os.environ["GITLAB_TOKEN"]})


def _quote(path: str) -> str:
    """グループやプロジェクトのパスをAPIのIDとして使える形にする（infra/tools → infra%2Ftools）。"""
    return urllib.parse.quote(path, safe="")


def _summary(item: dict, kind: str, body_limit: int) -> dict:
    return {
        "kind": kind,
        "ref": item["references"]["full"],
        "title": item["title"],
        "state": item["state"],
        "author": (item.get("author") or {}).get("username"),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "body": http.clip(item.get("description"), body_limit),
        "url": item["web_url"],
    }


@tool
def search_gitlab(query: str, since: str | None = None, until: str | None = None, state: str | None = None) -> list[dict]:
    """GitLabのIssueとMerge Requestを検索する（タイトルと本文が対象。コメントは対象外）。

    query: 検索語。空白区切りはAND
    since, until: 更新日の範囲（YYYY-MM-DD）。省略可
    state: "opened" / "closed" / "merged"（mergedはMerge Requestだけ）。省略するとすべて
    新しく更新された順に最大20件。本文は先頭だけなので、詳しくは get_gitlab_item で読む。
    """
    # 画面で選んだプロジェクトがあればそれぞれで検索する。なければ .env の GITLAB_GROUP か、見られる全体
    group = os.environ.get("GITLAB_GROUP")
    if projects := scope.get():
        prefixes = [f"/projects/{_quote(p)}" for p in projects]
    else:
        prefixes = [f"/groups/{_quote(group)}" if group else ""]
    params = {
        "search": query,
        "scope": None if (projects or group) else "all",
        "updated_after": f"{since}T00:00:00Z" if since else None,
        "updated_before": f"{until}T23:59:59Z" if until else None,
        "state": state,
        "order_by": "updated_at",
        "per_page": SEARCH_LIMIT,
    }
    results = []
    for prefix in prefixes:
        for kind, resource in KINDS.items():
            if kind == "issue" and state == "merged":
                continue
            results += [_summary(item, kind, 300) for item in _get(f"{prefix}/{resource}", params)]
    results.sort(key=lambda r: r["updated_at"], reverse=True)
    return results[:SEARCH_LIMIT]


@tool
def get_gitlab_item(project: str, iid: int, kind: Literal["issue", "merge_request"]) -> dict:
    """GitLabのIssueかMerge Requestを1件、本文とコメントつきで読む。

    project: プロジェクトのパス（例: "infra/tools"。search_gitlab の ref の # や ! より前）
    iid: Issue・Merge Requestの番号（ref の # や ! より後ろ）
    kind: "issue" か "merge_request"
    コメントは古い順に最大30件。クローズやマージなどの操作の記録（system: true）も含む。
    """
    path = f"/projects/{_quote(project)}/{KINDS[kind]}/{iid}"
    item = _summary(_get(path), kind, 3000)
    notes = _get(f"{path}/notes", {"sort": "asc", "order_by": "created_at", "per_page": NOTE_LIMIT})
    item["comments"] = [
        {
            "author": (n.get("author") or {}).get("username"),
            "created_at": n["created_at"],
            "system": n.get("system", False),
            "body": http.clip(n.get("body"), 1000),
        }
        for n in notes
    ]
    return item


def check() -> str:
    return f"{_get('/user')['username']} としてつながりました"


def scopes() -> list[str]:
    """自分が参加しているプロジェクト（GITLAB_GROUP があればその中）。最近動きがあった順に100件。"""
    params = {"simple": "true", "order_by": "last_activity_at", "per_page": 100}
    if group := os.environ.get("GITLAB_GROUP"):
        projects = _get(f"/groups/{_quote(group)}/projects", {**params, "include_subgroups": "true"})
    else:
        projects = _get("/projects", {**params, "membership": "true"})
    return [p["path_with_namespace"] for p in projects]


GITLAB = Source(
    id="gitlab",
    label="GitLab",
    tools=[search_gitlab, get_gitlab_item],
    configured=bool(os.environ.get("GITLAB_URL") and os.environ.get("GITLAB_TOKEN")),
    requires="GITLAB_URL, GITLAB_TOKEN",
    check=check,
    scopes=scopes,
    scope_label="プロジェクト",
)
