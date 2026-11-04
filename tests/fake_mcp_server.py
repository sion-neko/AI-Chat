"""テスト用のMCPサーバー（stdio）。架空のメモを検索するツールだけを持つ。"""

from mcp.server.fastmcp import FastMCP

server = FastMCP("fake-notes", log_level="WARNING")

NOTES = [
    {"date": "2026-10-03", "text": "バックアップ先のNASを入れ替えた", "url": "https://example.invalid/notes/1"},
    {"date": "2026-10-12", "text": "NASの容量アラートを80%に設定", "url": "https://example.invalid/notes/2"},
]


@server.tool()
def search_notes(query: str) -> list[dict]:
    """メモをキーワード検索する。queryは空白区切りでOR検索。"""
    words = query.split()
    return [n for n in NOTES if any(w in n["text"] for w in words)]


@server.tool()
def broken(query: str) -> str:
    """いつも失敗するツール。"""
    raise ValueError("壊れています")


if __name__ == "__main__":
    server.run()
