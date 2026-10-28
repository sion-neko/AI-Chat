"""おばけ探偵事務所のキャラクター。所長と、ソースごとの所員。

既定値はここに書き、画面で編集した内容はDBに保存して既定値より優先する。
"""

from dataclasses import dataclass

from app import db

NAME_LENGTH = 20
PERSONA_LENGTH = 300


@dataclass(frozen=True)
class Character:
    name: str
    persona: str


DIRECTOR_KEY = "director"

DEFAULT_DIRECTOR = Character(
    name="シロ",
    persona="おばけ探偵事務所の所長。落ち着いていて面倒見がいい。所員の報告を手際よくまとめる。",
)

# キーはソースID
DEFAULT_MEMBERS = {
    "dummy_chat": Character(name="ポルタ", persona="元気でおしゃべりな所員。チャットの賑やかな場所が好き。"),
    "dummy_wiki": Character(name="コソリ", persona="無口で慎重な所員。人目につかない場所の調査が得意。"),
}


class CharacterError(Exception):
    """キャラクターの編集内容が不正。"""


def default(key: str, source_label: str = "") -> Character:
    """既定のキャラクター。既定がないソースには、仮の所員を割り当てる。"""
    if key == DIRECTOR_KEY:
        return DEFAULT_DIRECTOR
    return DEFAULT_MEMBERS.get(key) or Character(name=f"{source_label}担当", persona="まじめな所員。")


def _get(key: str, source_label: str = "") -> Character:
    saved = db.load_characters().get(key)
    return Character(**saved) if saved else default(key, source_label)


def director() -> Character:
    return _get(DIRECTOR_KEY)


def member(source_id: str, source_label: str) -> Character:
    return _get(source_id, source_label)


def save(key: str, name: str, persona: str):
    name, persona = name.strip(), persona.strip()
    if not name:
        raise CharacterError("名前を入れてください")
    if len(name) > NAME_LENGTH:
        raise CharacterError(f"名前は{NAME_LENGTH}文字以内にしてください")
    if len(persona) > PERSONA_LENGTH:
        raise CharacterError(f"性格は{PERSONA_LENGTH}文字以内にしてください")
    db.save_character(key, name, persona)


def reset(key: str):
    db.delete_character(key)
