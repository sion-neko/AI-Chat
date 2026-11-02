"""検索ソースの一覧と、指定されたソースの検証。"""

from app.sources import dummy, github, gitlab, mcp_servers, zulip
from app.sources.base import Source

__all__ = ["ALL_SOURCES", "Source", "SourceError", "check", "describe", "resolve", "status"]

ALL_SOURCES: list[Source] = [dummy.CHAT, dummy.WIKI, github.GITHUB, gitlab.GITLAB, zulip.ZULIP, *mcp_servers.load()]


class SourceError(Exception):
    """ソースの指定が不正、または今の設定では使えない。"""


def describe() -> list[dict]:
    """画面に出すソースの一覧。設定がそろっていないソースは出さない。"""
    return [{"id": s.id, "label": s.label} for s in ALL_SOURCES if s.configured]


def status() -> list[dict]:
    """接続確認の画面に出す一覧。設定されていないソースも、必要な設定と一緒に出す。"""
    return [{"id": s.id, "label": s.label, "configured": s.configured, "requires": s.requires} for s in ALL_SOURCES]


def check(source: Source) -> dict:
    """ソースに実際につないでみる。結果は {"state": "ok" / "error" / "unset", "message"}。"""
    if not source.configured:
        return {"state": "unset", "message": f"{source.requires} を .env に書くと使えます"}
    if not source.check:
        return {"state": "ok", "message": "確認不要（外部につながない）"}
    try:
        return {"state": "ok", "message": source.check()}
    except Exception as e:
        return {"state": "error", "message": str(e)}


def resolve(source_ids: list[str]) -> list[Source]:
    """指定されたソースを返す。使えないソースが含まれていたら SourceError。"""
    if not source_ids:
        raise SourceError("ソースを1つ以上選んでください")
    by_id = {s.id: s for s in ALL_SOURCES}
    selected = []
    for source_id in dict.fromkeys(source_ids):  # 重複を除く
        source = by_id.get(source_id)
        if source is None or not source.configured:
            raise SourceError(f"使えないソースです: {source_id}")
        selected.append(source)
    return selected
