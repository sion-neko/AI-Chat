import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import characters, db


class CharactersTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(db, "DB_PATH", str(Path(tmp.name) / "chat.db"))
        patch.start()
        self.addCleanup(patch.stop)
        db.init()

    def test_defaults(self):
        self.assertEqual(characters.director(), characters.DEFAULT_DIRECTOR)
        self.assertEqual(characters.member("github", "GitHub").name, "GitHub担当")

    def test_save_overrides_and_reset_restores(self):
        characters.save("github", "  ギット  ", "几帳面な所員。")
        self.assertEqual(characters.member("github", "GitHub"), characters.Character("ギット", "几帳面な所員。"))
        characters.reset("github")
        self.assertEqual(characters.member("github", "GitHub").name, "GitHub担当")

    def test_validation(self):
        with self.assertRaisesRegex(characters.CharacterError, "名前を入れて"):
            characters.save("director", " ", "")
        with self.assertRaisesRegex(characters.CharacterError, "20文字"):
            characters.save("director", "あ" * 21, "")
        self.assertEqual(characters.director(), characters.DEFAULT_DIRECTOR)


if __name__ == "__main__":
    unittest.main()
