"""ソースのAPIを呼ぶための小さなHTTPクライアント（標準ライブラリのみ）。"""

import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
# 取ってくる画像の大きさの上限
IMAGE_LIMIT = 10 * 1024 * 1024


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


def get_image(url: str, headers: dict | None = None) -> tuple[bytes, str]:
    """画像を取ってくる。返り値は (バイト列, MIMEタイプ)。

    認証ヘッダーは転送先（S3などの保存先）には送らない。署名付きURLに余計な認証を付けると断られるうえ、
    トークンを外に渡すことになるため。
    """
    req = urllib.request.Request(url)
    for key, value in (headers or {}).items():
        req.add_unredirected_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            mime = res.headers.get_content_type()
            data = res.read(IMAGE_LIMIT + 1)
    except urllib.error.HTTPError as e:
        raise ApiError(f"画像を取れません: HTTP {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise ApiError(f"画像を取れません: {e.reason}") from None
    if not mime.startswith("image/"):
        raise ApiError(f"画像ではありません（{mime}）")
    if len(data) > IMAGE_LIMIT:
        raise ApiError(f"画像が大きすぎます（{IMAGE_LIMIT // 1024 // 1024}MBまで）")
    return data, mime


def clip(text: str | None, limit: int) -> str:
    """長い本文を切り詰める。ローカルLLMのコンテキストを使い切らないように。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…（省略）"
