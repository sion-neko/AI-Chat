"""回答に出てくるリンクが、所員の検索結果に本当にあったかを照合し、出典の一覧を作る。

LLMはそれらしいURLを作ってしまうことがある。照合の相手は検索ツールが返した結果だけにする
（所員の報告もLLMが書いたものなので、根拠にしない）。
"""

import json
import re

URL = re.compile(r"https?://[^\s\"'<>()\[\]{}）」、。]+")
# 文末の句読点などはURLに含めない
TRAILING = ".,;:!?"
TITLE_LENGTH = 60


def extract(text: str) -> list[str]:
    """文章からURLを出てきた順に取り出す（重複は除く）。"""
    return list(dict.fromkeys(m.rstrip(TRAILING) for m in URL.findall(text or "")))


def _normalize(url: str) -> str:
    return url.rstrip("/")


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= TITLE_LENGTH else text[:TITLE_LENGTH] + "…"


def _describe(item: dict, parent_title: str | None) -> dict:
    """検索結果の1件から、出典に出す見出しと日付を取り出す。ソースごとの形の違いをここで吸収する。"""
    if item.get("title"):
        title = item["title"]
    elif item.get("channel") and item.get("topic"):
        title = f"{item['channel']} > {item['topic']}"
    elif parent_title:
        # コメントなど、親（Issue等）の中の1件
        title = f"{parent_title} のコメント"
    else:
        title = item.get("text") or item.get("content") or item.get("body") or ""
    date = item.get("date") or item.get("updated_at") or item.get("created_at") or ""
    return {"title": _short(title), "date": str(date)[:10]}


def _walk(value, parent_title: str | None, found: dict):
    """JSONの中の url を持つ項目を探す。"""
    if isinstance(value, list):
        for v in value:
            _walk(v, parent_title, found)
    elif isinstance(value, dict):
        if isinstance(value.get("url"), str):
            found.setdefault(_normalize(value["url"]), _describe(value, parent_title))
        for v in value.values():
            if isinstance(v, (list, dict)):
                _walk(v, value.get("title") or parent_title, found)


def _evidence(reports: list[dict]) -> dict[str, dict]:
    """検索結果に出てきたURL → {title, date, source}。"""
    found = {}
    for report in reports:
        for step in report["steps"]:
            result = step["result"] or ""
            items = {}
            try:
                _walk(json.loads(result), None, items)
            except ValueError:
                pass
            # JSONでない結果（MCPのツール等）や、JSONの中の文字列に埋まったURLも拾う
            for url in extract(result):
                items.setdefault(_normalize(url), {"title": "", "date": ""})
            for url, info in items.items():
                info = {**info, "source": report["label"]}
                found.setdefault(url, info)
                # コメントへのリンク（ページ#コメント）があれば、ページ自体へのリンクも根拠ありとみなす
                found.setdefault(url.split("#", 1)[0], info)
    return found


def verify(answer: str, reports: list[dict]) -> list[dict]:
    """回答のリンクを出てきた順に返す。found は検索結果にあったか。あれば title・date・source も付ける。"""
    evidence = _evidence(reports)
    result = []
    for url in extract(answer):
        info = evidence.get(_normalize(url))
        result.append({"url": url, "found": info is not None, **(info or {})})
    return result
