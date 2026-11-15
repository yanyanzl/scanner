"""标题翻译测试：needs_translation 的判定逻辑。

gtx 端点已删除（2026-11-15 深挖结论：修不好），translate_text /
translate_titles 一并删除；Azure 与 DeepL 的翻译函数见
tests/test_translation_azure.py 与 tests/test_translation_deepl.py。
这里只测“需不需要翻”的判定：纯中文、空、无字母的不翻，
其他（英文、小语种、中英混合）都翻。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar.translate import needs_translation

MALAYALAM_TITLE = (
    "Today Breaking News LIVE | ഇന്നത്തെ പ്രധാന വാർത്ത | "
    "Top Headlines Of The Hour | 15.11.2026"
)


class TestNeedsTranslation(unittest.TestCase):
    def test_pure_chinese_false(self):
        self.assertFalse(needs_translation("苹果发布新款芯片"))
        self.assertFalse(needs_translation("今日头条：科技新闻"))

    def test_english_true(self):
        self.assertTrue(needs_translation("Apple unveils new iPhone chip"))
        self.assertTrue(needs_translation("Breaking news"))

    def test_mixed_true(self):
        # 中英混合、且中文占比不到 90% 时仍需翻译。
        self.assertTrue(needs_translation(MALAYALAM_TITLE))
        self.assertTrue(needs_translation("iPhone 17 发布"))

    def test_pure_non_latin_script_true(self):
        # 纯小语种标题（一个拉丁/CJK 字母都没有）也要翻译，不能误判为“没有字母”。
        self.assertTrue(needs_translation("ഇന്നത്തെ പ്രധാന വാർത്ത"))
        self.assertTrue(needs_translation("Новости дня"))

    def test_mostly_chinese_false(self):
        # 中文占 90% 以上时不翻译（夹一个英文字母不算需要翻译）。
        self.assertFalse(needs_translation("苹果发布新款芯片手机 i"))

    def test_empty_false(self):
        self.assertFalse(needs_translation(""))
        self.assertFalse(needs_translation("   "))

    def test_digits_punctuation_only_false(self):
        # 没有字母：数字、标点、日期不计入分母，直接判为不需要翻译。
        self.assertFalse(needs_translation("15.11.2026"))
        self.assertFalse(needs_translation("!!!"))
        self.assertFalse(needs_translation("12345"))


if __name__ == "__main__":
    unittest.main()
