from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.tools import BaseTool


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    tools: list[BaseTool]
    # 必要な環境変数（トークン等）がそろっているか
    configured: bool
    # 使うのに必要な設定。設定されていないときに画面で案内する
    requires: str = ""
    # 接続確認。軽いAPIを1回呼んで、誰としてつながったかを返す。失敗したら例外を投げる
    check: Callable[[], str] | None = None
