from dataclasses import dataclass

from langchain_core.tools import BaseTool


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    tools: list[BaseTool]
    # 必要な環境変数（トークン等）がそろっているか
    configured: bool
