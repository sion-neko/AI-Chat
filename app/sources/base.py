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
    # 質問ごとに選べる検索範囲（リポジトリやチャンネルの名前の一覧）を返す。選べないソースは None
    scopes: Callable[[], list[str]] | None = None
    # 検索範囲の呼び名（「リポジトリ」など）
    scope_label: str = ""
    # 検索結果に出てくる画像のURLから、画像を取ってくる（バイト列, MIMEタイプ）。取れないなら ApiError
    # 画像を読めるモデルのときだけ、所員の view_image ツールが使う
    fetch_image: Callable[[str], tuple[bytes, str]] | None = None
