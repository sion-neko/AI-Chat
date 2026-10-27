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
