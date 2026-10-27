"""モデルごとにできること（画像を読めるか等）。

対応表は app/models.json に書く。設定（.env）で切り替えるのではなく、使っているモデル（LLM_MODEL）から決める。
"""

import json
import os
from fnmatch import fnmatchcase
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("models.json")


def capabilities(model: str) -> dict:
    """モデル名に最初に合った行の内容（match 以外）。どれにも合わなければ空。"""
    table = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["models"]
    for row in table:
        if fnmatchcase(model.lower(), row["match"].lower()):
            return {k: v for k, v in row.items() if k != "match"}
    return {}


def vision() -> bool:
    """今のモデルが画像を読めるか。"""
    return bool(capabilities(os.environ.get("LLM_MODEL", "")).get("vision"))
