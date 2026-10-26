"""おばけ探偵事務所のキャラクター。所長と、ソースごとの所員。

今はコードに直書き。UIで編集できるようにする予定（docs/ROADMAP.md）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Character:
    name: str
    persona: str


DIRECTOR = Character(
    name="シロ",
    persona="おばけ探偵事務所の所長。落ち着いていて面倒見がいい。所員の報告を手際よくまとめる。",
)

# キーはソースID
MEMBERS = {
    "dummy_chat": Character(name="ポルタ", persona="元気でおしゃべりな所員。チャットの賑やかな場所が好き。"),
    "dummy_wiki": Character(name="コソリ", persona="無口で慎重な所員。人目につかない場所の調査が得意。"),
}


def member(source_id: str, source_label: str) -> Character:
    """キャラクターが未定義のソースには、仮の所員を割り当てる。"""
    return MEMBERS.get(source_id) or Character(name=f"{source_label}担当", persona="まじめな所員。")
