"""検索ソースの一覧と、指定されたソースの検証。"""

from app.sources import dummy, github, gitlab, mcp_servers, zulip
from app.sources.base import Source

__all__ = ["Source", "SourceError", "all_sources", "check", "describe", "resolve", "resolve_scopes", "scope_choices", "status"]

BUILTIN: list[Source] = [dummy.CHAT, dummy.WIKI, github.GITHUB, gitlab.GITLAB, zulip.ZULIP]
mcp_servers.load()


def all_sources() -> list[Source]:
    """組み込みのソースと、MCPサーバーのソース。MCPサーバーは画面から足し引きできるので、毎回組み立てる。"""
    return [*BUILTIN, *mcp_servers.sources()]


class SourceError(Exception):
    """ソースの指定が不正、または今の設定では使えない。"""


def describe() -> list[dict]:
    """画面に出すソースの一覧。設定がそろっていないソースは出さない。scope_label は範囲を選べるソースだけ。"""
    return [{"id": s.id, "label": s.label, "scope_label": s.scope_label if s.scopes else ""} for s in all_sources() if s.configured]


def status() -> list[dict]:
    """接続確認の画面に出す一覧。設定されていないソースも、必要な設定と一緒に出す。"""
    return [
        {"id": s.id, "label": s.label, "configured": s.configured, "requires": s.requires, "mcp": s.id.startswith("mcp_")}
        for s in all_sources()
    ]


def check(source: Source) -> dict:
    """ソースに実際につないでみる。結果は {"state": "ok" / "error" / "unset", "message"}。"""
    if not source.configured and source.id.startswith("mcp_"):
        return {"state": "error", "message": source.requires}
    if not source.configured:
        return {"state": "unset", "message": f"{source.requires} を .env に書くと使えます"}
    if not source.check:
        return {"state": "ok", "message": "確認不要（外部につながない）"}
    try:
        return {"state": "ok", "message": source.check()}
    except Exception as e:
        return {"state": "error", "message": str(e)}


def scope_choices(source_id: str) -> list[str]:
    """ソースの検索範囲の候補。範囲を選べないソースなら SourceError。"""
    [source] = resolve([source_id])
    if not source.scopes:
        raise SourceError(f"範囲を選べないソースです: {source_id}")
    return source.scopes()


def resolve_scopes(selected: list[Source], scopes: dict) -> dict[str, list[str]]:
    """画面から届いた検索範囲を確かめる。選んでいないソースや、範囲を選べないソースの分は捨てる。"""
    result = {}
    for source in selected:
        names = scopes.get(source.id) or []
        if source.scopes and isinstance(names, list) and all(isinstance(n, str) for n in names) and names:
            result[source.id] = names
    return result


def resolve(source_ids: list[str]) -> list[Source]:
    """指定されたソースを返す。使えないソースが含まれていたら SourceError。"""
    if not source_ids:
        raise SourceError("ソースを1つ以上選んでください")
    by_id = {s.id: s for s in all_sources()}
    selected = []
    for source_id in dict.fromkeys(source_ids):  # 重複を除く
        source = by_id.get(source_id)
        if source is None or not source.configured:
            raise SourceError(f"使えないソースです: {source_id}")
        selected.append(source)
    return selected
