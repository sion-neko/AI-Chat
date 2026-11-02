"""MCPサーバーをソースにする。設定ファイルに書いたサーバー1つが、1人の所員の担当になる。

設定ファイルは MCP_CONFIG（既定は data/mcp.json）。Claude Desktop などと同じ mcpServers の形で書く。
  {
    "mcpServers": {
      "notes": {"label": "メモ", "command": "python", "args": ["/work/data/notes_server.py"]},
      "wiki":  {"label": "Wiki", "url": "http://host.docker.internal:9000/mcp", "headers": {"Authorization": "Bearer xxx"}}
    }
  }
  label   画面に出すソース名（省略するとキー名）
  command/args/env  手元でサーバーを起動する（stdio）。コンテナの中で動くコマンドであること
  url/headers       HTTPでつなぐ（Streamable HTTP。"type": "sse" ならSSE）
"""

import asyncio
import json
import os
import re

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp.client.stdio import get_default_environment

from app.sources.base import Source

CONFIG_PATH = os.environ.get("MCP_CONFIG", "/work/data/mcp.json")


def _connection(conf: dict) -> dict:
    if "url" in conf:
        transport = "sse" if (conf.get("type") or conf.get("transport")) == "sse" else "streamable_http"
        return {"transport": transport, "url": conf["url"], "headers": conf.get("headers") or {}}
    # env だけを渡すと PATH なども消えるので、最低限の環境変数に足す（アプリのトークン等は渡さない）
    env = {**get_default_environment(), **conf.get("env", {})}
    return {"transport": "stdio", "command": conf["command"], "args": conf.get("args", []), "env": env}


def _text(result) -> str:
    """MCPのツールの結果（コンテンツブロックの並び）を文字列にする。画像などは省く。"""
    if isinstance(result, str):
        return result
    texts = [b["text"] for b in result if isinstance(b, dict) and b.get("type") == "text"]
    if len(texts) < len(result):
        texts.append("（文字以外の結果は省略）")
    return "\n".join(texts)


def _sync(tool: BaseTool) -> BaseTool:
    """MCPのツールは非同期でしか呼べないので、所員のツールループ（同期）から呼べる形に包む。"""

    def call(**kwargs) -> str:
        return _text(asyncio.run(tool.ainvoke(kwargs)))

    return StructuredTool(name=tool.name, description=tool.description, args_schema=tool.args_schema, func=call)


def _error(e: BaseException) -> str:
    # 接続の失敗は ExceptionGroup に包まれてくるので、中身を出す
    while isinstance(e, BaseExceptionGroup):
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


def _source(name: str, conf: dict) -> Source:
    client = MultiServerMCPClient({name: _connection(conf)})

    def check() -> str:
        tools = asyncio.run(client.get_tools())
        return f"ツール{len(tools)}個（{', '.join(t.name for t in tools)}）が使えます"

    common = {"id": "mcp_" + re.sub(r"\W", "_", name), "label": conf.get("label", name), "check": check}
    try:
        tools = asyncio.run(client.get_tools())
    except Exception as e:
        return Source(tools=[], configured=False, requires=f"{CONFIG_PATH} の {name}（起動時につながりませんでした: {_error(e)}）", **common)
    return Source(tools=[_sync(t) for t in tools], configured=True, requires=f"{CONFIG_PATH} の {name}", **common)


def load() -> list[Source]:
    """設定ファイルに書かれたMCPサーバーにつなぎ、ソースにする。設定ファイルがなければ何もしない。"""
    if not os.path.exists(CONFIG_PATH):
        return []
    with open(CONFIG_PATH, encoding="utf-8") as f:
        servers = json.load(f).get("mcpServers", {})
    return [_source(name, conf) for name, conf in servers.items()]
