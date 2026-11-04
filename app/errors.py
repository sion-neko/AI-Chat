"""画面に返すエラー。web.py が受けて、400 / 404 として返す。"""


class InputError(Exception):
    """リクエストの中身が不正、または今はできない操作。メッセージはそのまま画面に出る。"""


class NotFound(Exception):
    """指定された会話やフォルダなどがない。"""
