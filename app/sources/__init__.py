"""検索ソースの一覧と、指定されたソースの検証。"""

from app.sources import dummy, github, gitlab
from app.sources.base import Source

__all__ = ["ALL_SOURCES", "Source", "SourceError", "describe", "resolve"]

ALL_SOURCES: list[Source] = [dummy.CHAT, dummy.WIKI, github.GITHUB, gitlab.GITLAB]


class SourceError(Exception):
    """ソースの指定が不正、または今の設定では使えない。"""


def describe() -> list[dict]:
    """画面に出すソースの一覧。設定がそろっていないソースは出さない。"""
    return [{"id": s.id, "label": s.label} for s in ALL_SOURCES if s.configured]


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
