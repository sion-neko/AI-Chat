"""回答に出てくるリンクが、所員の検索結果に本当にあったかを照合する。

LLMはそれらしいURLを作ってしまうことがある。照合の相手は検索ツールが返した結果だけにする
（所員の報告もLLMが書いたものなので、根拠にしない）。
"""

import re

URL = re.compile(r"https?://[^\s\"'<>()\[\]{}）」、。]+")
# 文末の句読点などはURLに含めない
TRAILING = ".,;:!?"


def extract(text: str) -> list[str]:
    """文章からURLを出てきた順に取り出す（重複は除く）。"""
    return list(dict.fromkeys(m.rstrip(TRAILING) for m in URL.findall(text or "")))


def _normalize(url: str) -> str:
    return url.rstrip("/")


def verify(answer: str, reports: list[dict]) -> list[dict]:
    """回答のリンクごとに、検索結果にあったか（found）を返す。

    検索結果にコメント等へのリンク（ページ#コメント）があれば、そのページ自体へのリンクも根拠ありとみなす。
    """
    found = set()
    for report in reports:
        for step in report["steps"]:
            for url in extract(step["result"]):
                found.add(_normalize(url))
                found.add(_normalize(url.split("#", 1)[0]))
    return [{"url": url, "found": _normalize(url) in found} for url in extract(answer)]
