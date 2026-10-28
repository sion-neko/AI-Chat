"""お試し用のサンプルソース。架空のメッセージを返すだけで、外部には一切つながらない。

ほかのソースを1つも設定していないときだけ使う（app/sources/__init__.py の usable）。
"""

from langchain_core.tools import tool

from app.sources.base import Source

_CHAT_MESSAGES = [
    {
        "date": "2026-10-01",
        "author": "山田",
        "text": "監視サーバーのディスク増設は来週メンテで実施予定です",
        "url": "https://example.invalid/chat/1",
    },
    {
        "date": "2026-10-08",
        "author": "佐藤",
        "text": "ディスク増設完了しました。容量アラートも閾値を戻しています",
        "url": "https://example.invalid/chat/2",
    },
]

_WIKI_PAGES = [
    {
        "date": "2026-10-10",
        "author": "鈴木",
        "text": "案件Xの VPN証明書の更新手順をWikiにまとめました",
        "url": "https://example.invalid/wiki/1",
    },
]


def _search(messages: list[dict], query: str) -> list[dict]:
    words = query.split()
    return [m for m in messages if any(w in m["text"] for w in words)]


@tool
def search_dummy_chat(query: str) -> list[dict]:
    """ダミーのチャット履歴をキーワード検索する。queryは空白区切りでOR検索。"""
    return _search(_CHAT_MESSAGES, query)


@tool
def search_dummy_wiki(query: str) -> list[dict]:
    """ダミーのWikiをキーワード検索する。queryは空白区切りでOR検索。"""
    return _search(_WIKI_PAGES, query)


CHAT = Source(id="dummy_chat", label="サンプルチャット", tools=[search_dummy_chat], configured=True)
WIKI = Source(id="dummy_wiki", label="サンプルWiki", tools=[search_dummy_wiki], configured=True)
