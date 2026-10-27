"""Zulip（チャンネルのメッセージとDM）。

環境変数:
  ZULIP_URL      必須。ZulipのURL（例: https://zulip.example.com）
  ZULIP_EMAIL    必須。自分のアカウントのメールアドレス
  ZULIP_API_KEY  必須。個人のAPIキー（Zulipの「個人設定 → アカウントとプライバシー → APIキー」）
"""

import base64
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone

from langchain_core.tools import tool

from app.sources import http, scope
from app.sources.base import Source

# 古いセルフホスト版でも動くよう、チャンネルは "stream" と書く（新しい版でも通る）
# Zulipの検索は期間で絞れないので、多めに取ってからこちらで日付を絞る
FETCH_LIMIT = 100
SEARCH_LIMIT = 20
TOPIC_LIMIT = 50


def _base_url() -> str:
    return os.environ["ZULIP_URL"].rstrip("/")


def _get(path: str, params: dict):
    credentials = base64.b64encode(f"{os.environ['ZULIP_EMAIL']}:{os.environ['ZULIP_API_KEY']}".encode()).decode()
    return http.get_json(f"{_base_url()}/api/v1{path}", params, {"Authorization": f"Basic {credentials}"})


def _messages(narrow: list[dict], limit: int) -> list[dict]:
    """narrow に合うメッセージを新しいほうから limit 件取り、古い順で返す。"""
    data = _get(
        "/messages",
        {
            "anchor": "newest",
            "num_before": limit,
            "num_after": 0,
            "narrow": json.dumps(narrow, ensure_ascii=False),
            "apply_markdown": "false",
        },
    )
    return data["messages"]


def _hash(text: str) -> str:
    """ZulipのURLの # 以降で使う形に符号化する（Zulipの encodeHashComponent と同じ規則）。"""
    quoted = urllib.parse.quote(text, safe="!*'()~")
    return re.sub(r"[%().]", lambda m: {"%": ".", "(": ".28", ")": ".29", ".": ".2E"}[m[0]], quoted)


def _view(msg: dict, body_limit: int) -> dict:
    view = {
        "sender": msg["sender_full_name"],
        "date": datetime.fromtimestamp(msg["timestamp"], timezone.utc).isoformat(),
        "content": http.clip(msg["content"], body_limit),
    }
    if msg["type"] == "stream":
        view["channel"] = msg["display_recipient"]
        view["topic"] = msg["subject"]
        view["url"] = (
            f"{_base_url()}/#narrow/stream/{msg['stream_id']}-{_hash(msg['display_recipient'])}"
            f"/topic/{_hash(msg['subject'])}/near/{msg['id']}"
        )
    else:
        view["channel"] = "（DM）"
        view["url"] = f"{_base_url()}/#narrow/near/{msg['id']}"
    return view


@tool
def search_zulip(query: str, since: str | None = None, until: str | None = None, channel: str | None = None) -> list[dict]:
    """Zulipのメッセージを全文検索する（自分が見られるチャンネルとDM）。

    query: 検索語。空白区切りはAND。"..." で囲むとフレーズ検索
    since, until: 投稿日の範囲（YYYY-MM-DD）。省略可
    channel: チャンネル名で絞る。省略可（検索範囲が決められているときは、その中のチャンネルだけ）
    新しい順に最大20件。話の続きは get_zulip_topic でトピックごと読む。
    """
    since = scope.since(since)  # 続報を調べるときは、前回の調査より前を探さない
    # 画面でチャンネルを選んでいれば、その中だけを探す（Zulipの narrow はチャンネルを1つしか指定できないので、1つずつ）
    allowed = scope.get()
    if channel and allowed and channel not in allowed:
        raise http.ApiError(f"チャンネル {channel} は今回の検索範囲の外です。範囲: {', '.join(allowed)}")
    channels = [channel] if channel else allowed or [None]
    found = []
    for c in channels:
        narrow = [{"operator": "search", "operand": query}]
        if c:
            narrow.append({"operator": "stream", "operand": c})
        found += _messages(narrow, FETCH_LIMIT)
    found.sort(key=lambda m: m["timestamp"])
    results = []
    for msg in reversed(found):
        day = datetime.fromtimestamp(msg["timestamp"], timezone.utc).date().isoformat()
        if (since and day < since) or (until and day > until):
            continue
        results.append(_view(msg, 500))
        if len(results) == SEARCH_LIMIT:
            break
    return results


@tool
def get_zulip_topic(channel: str, topic: str) -> list[dict]:
    """Zulipのトピックを1つ、メッセージを古い順に読む（新しいほうから最大50件）。

    channel: チャンネル名
    topic: トピック名（search_zulip の結果の topic をそのまま使う）
    """
    narrow = [{"operator": "stream", "operand": channel}, {"operator": "topic", "operand": topic}]
    return [_view(msg, 800) for msg in _messages(narrow, TOPIC_LIMIT)]


def check() -> str:
    return f"{_get('/users/me', {})['full_name']} としてつながりました"


def scopes() -> list[str]:
    """自分が参加しているチャンネル。"""
    return sorted(s["name"] for s in _get("/users/me/subscriptions", {})["subscriptions"])


def fetch_image(url: str) -> tuple[bytes, str]:
    """メッセージに貼られた画像（/user_uploads/...）を取ってくる。

    アップロードされたファイルはログインしないと見られないので、APIで一時的なURLをもらってから取る。
    """
    path = url.removeprefix(_base_url())
    if not path.startswith("/user_uploads/"):
        raise http.ApiError("Zulipに貼られた画像（/user_uploads/... のURL）ではありません")
    temporary = _get(path, {})["url"]
    return http.get_image(f"{_base_url()}{temporary}")


ZULIP = Source(
    id="zulip",
    label="Zulip",
    tools=[search_zulip, get_zulip_topic],
    configured=all(os.environ.get(k) for k in ("ZULIP_URL", "ZULIP_EMAIL", "ZULIP_API_KEY")),
    requires="ZULIP_URL, ZULIP_EMAIL, ZULIP_API_KEY",
    fetch_image=fetch_image,
    check=check,
    scopes=scopes,
    scope_label="チャンネル",
)
