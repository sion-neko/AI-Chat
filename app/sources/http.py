"""ソースのAPIを呼ぶための小さなHTTPクライアント（標準ライブラリのみ）。"""

import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30


class ApiError(Exception):
    """APIがエラーを返した。メッセージはそのままLLMに渡る（ツールの結果になる）ので、何が悪いか分かるように書く。"""


def get_json(url: str, params: dict | None = None, headers: dict | None = None):
    """GETしてJSONを返す。値が None のパラメータは送らない。"""
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise ApiError(f"HTTP {e.code} {e.reason}: {detail}") from None
    except urllib.error.URLError as e:
        raise ApiError(f"接続できません: {e.reason}") from None


def clip(text: str | None, limit: int) -> str:
    """長い本文を切り詰める。ローカルLLMのコンテキストを使い切らないように。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…（省略）"
