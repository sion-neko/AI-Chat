import os
import unittest
from unittest import mock

from app import models


class ModelsTest(unittest.TestCase):
    def test_capabilities(self):
        self.assertTrue(models.capabilities("Qwen2.5-VL-72B-Instruct")["vision"])
        self.assertTrue(models.capabilities("google/gemma-3-27b-it")["vision"])
        self.assertTrue(models.capabilities("gpt-4o-mini")["vision"])
        self.assertFalse(models.capabilities("Qwen2.5-72B-Instruct")["vision"])
        self.assertFalse(models.capabilities("llama-3.3-70b")["vision"])

    def test_vision_follows_llm_model(self):
        with mock.patch.dict(os.environ, {"LLM_MODEL": "llava:13b"}):
            self.assertTrue(models.vision())
        with mock.patch.dict(os.environ, {"LLM_MODEL": "deepseek-r1"}):
            self.assertFalse(models.vision())

    def test_table_has_fallback_last(self):
        # 最後の行で、どれにも合わないモデルを「できない」にしている
        self.assertEqual(models.capabilities("どこにもないモデル"), {"vision": False})


if __name__ == "__main__":
    unittest.main()
