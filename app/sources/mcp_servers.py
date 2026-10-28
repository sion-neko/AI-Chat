"""MCPサーバーをソースにする。設定ファイルに書いたサーバー1つが、1人の所員の担当になる。

設定ファイルは MCP_CONFIG（既定は data/mcp.json）。Claude Desktop などと同じ mcpServers の形で書く。
画面（ソースと接続）から追加・編集・削除でき、そのときは再起動しなくても反映する。手で書き換えたときは再起動する。
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
import threading

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp.client.stdio import get_default_environment

from app.sources.base import Source

CONFIG_PATH = os.environ.get("MCP_CONFIG", "/work/data/mcp.json")
LABEL_LENGTH = 40

# 設定ファイルのキー → つないだソース（設定ファイルの順）。画面から変えたら入れ替える
_loaded: dict[str, Source] = {}
_lock = threading.Lock()


class McpConfigError(Exception):
    """画面から届いたMCPサーバーの設定が不正。メッセージはそのまま画面に出る。"""


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


def source_id(key: str) -> str:
    return "mcp_" + re.sub(r"\W", "_", key)


def _source(key: str, conf: dict) -> Source:
    client = MultiServerMCPClient({key: _connection(conf)})

    def check() -> str:
        tools = asyncio.run(client.get_tools())
        return f"ツール{len(tools)}個（{', '.join(t.name for t in tools)}）が使えます"

    common = {"id": source_id(key), "label": conf.get("label", key), "check": check}
    try:
        tools = asyncio.run(client.get_tools())
    except Exception as e:
        return Source(tools=[], configured=False, requires=f"つながりませんでした: {_error(e)}", **common)
    return Source(tools=[_sync(t) for t in tools], configured=True, requires=f"{CONFIG_PATH} の {key}", **common)


def read_config() -> dict[str, dict]:
    """設定ファイルの mcpServers。ファイルがなければ空。"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f).get("mcpServers", {})


def _write_config(servers: dict[str, dict]):
    """mcpServers 以外の項目は残したまま書き換える。途中で落ちても壊れないよう、別のファイルに書いてから置き換える。"""
    data = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    data["mcpServers"] = servers
    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    tmp = f"{CONFIG_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load() -> list[Source]:
    """設定ファイルに書かれたMCPサーバーにつなぎ、ソースにする（起動時）。"""
    with _lock:
        _loaded.clear()
        _loaded.update({key: _source(key, conf) for key, conf in read_config().items()})
        return list(_loaded.values())


def sources() -> list[Source]:
    return list(_loaded.values())


def _str_dict(value, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if not (isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())):
        raise McpConfigError(f"{what}の形が正しくありません")
    return {k.strip(): v for k, v in value.items() if k.strip()}


def normalize(conf: dict) -> dict:
    """画面から届いた設定を確かめ、設定ファイルに書く形にする。URLかコマンドのどちらか一方だけを持つ。"""
    label = str(conf.get("label", "")).strip()
    if not label:
        raise McpConfigError("表示名を入れてください")
    result = {"label": label[:LABEL_LENGTH]}
    url = str(conf.get("url") or "").strip()
    command = str(conf.get("command") or "").strip()
    if bool(url) == bool(command):
        raise McpConfigError("URLかコマンドのどちらか一方を入れてください")
    if url:
        if not re.match(r"https?://", url):
            raise McpConfigError("URLは http:// か https:// で始めてください")
        result["url"] = url
        if conf.get("type") == "sse":
            result["type"] = "sse"
        if headers := _str_dict(conf.get("headers"), "ヘッダー"):
            result["headers"] = headers
    else:
        args = conf.get("args") or []
        if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
            raise McpConfigError("引数の形が正しくありません")
        result["command"] = command
        if args:
            result["args"] = args
        if env := _str_dict(conf.get("env"), "環境変数"):
            result["env"] = env
    return result


def new_key(label: str) -> str:
    """表示名から、設定ファイルのキーを作る（英数字だけ。使われていれば番号を付ける）。"""
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "server"
    existing = read_config()
    key, n = base, 2
    while key in existing:
        key, n = f"{base}_{n}", n + 1
    return key


def save(key: str, conf: dict) -> Source:
    """1台分の設定を書き、つなぎ直す。キーがなければ足す。つながらなくても設定は残す（あとで直せるように）。"""
    conf = normalize(conf)
    with _lock:
        servers = read_config()
        servers[key] = conf
        _write_config(servers)
        source = _source(key, conf)
        # 設定ファイルの順に並べ直す（足したサーバーは最後に来る）
        loaded = {k: source if k == key else _loaded.get(k) for k in servers}
        _loaded.clear()
        _loaded.update({k: v for k, v in loaded.items() if v})
        return source


def delete(key: str):
    with _lock:
        servers = read_config()
        servers.pop(key, None)
        _write_config(servers)
        _loaded.pop(key, None)


def listing() -> list[dict]:
    """画面に出す一覧。設定と、つないだ結果（使えるツールか、つながらなかった理由）。"""
    result = []
    for key, conf in read_config().items():
        source = _loaded.get(key)
        result.append(
            {
                "key": key,
                "id": source_id(key),
                **conf,
                "connected": bool(source and source.configured),
                "tools": [t.name for t in source.tools] if source else [],
                "error": None if source and source.configured else (source.requires if source else "再起動すると読み込みます"),
            }
        )
    return result
