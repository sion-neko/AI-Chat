"""質問ごとに選んだ検索範囲（リポジトリ・プロジェクト・チャンネル）。

所員のスレッドで set し、検索ツールが get して使う。ツールの引数にしないのは、
LLMに範囲の外を検索させないため。LangChain・LangGraph はツールを別スレッドで
動かすときも contextvars を引き継ぐので、所員ごとの範囲がそのまま届く。
"""

from contextvars import ContextVar

_selected: ContextVar[list[str] | None] = ContextVar("selected_scope", default=None)


def set(scope: list[str] | None):
    _selected.set(scope or None)


def get() -> list[str] | None:
    """選ばれた範囲。None なら絞らない（.env の設定どおりに検索する）。"""
    return _selected.get()


# 続報を調べるときの「この日以降」（YYYY-MM-DD）。範囲と同じく、LLMに外させないためツールの引数にしない
_since: ContextVar[str | None] = ContextVar("since", default=None)


def set_since(day: str | None):
    _since.set(day)


def since(asked: str | None) -> str | None:
    """LLMが指定した since と続報の日付の、遅いほう。"""
    floor = _since.get()
    return max(filter(None, [asked, floor]), default=None)
