"""标题文本小工具测试：strip_untranslatable_tokens 与 hashtag_ratio。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar.text_utils import hashtag_ratio, strip_untranslatable_tokens


class TestStripUntranslatableTokens(unittest.TestCase):
    def test_strips_hashtags(self):
        self.assertEqual(
            strip_untranslatable_tokens("#generalknowledge #fact #technology"),
            "  ",
        )
        # 无空格连写的 #a#b#c 是一整个词，一次去干净。
        self.assertEqual(
            strip_untranslatable_tokens("#marathiculture#maharashtra#marathisciencewala"),
            "",
        )

    def test_strips_urls_and_mentions(self):
        self.assertEqual(
            strip_untranslatable_tokens("看这里 https://example.com/page#section 好"),
            "看这里  好",
        )
        self.assertEqual(strip_untranslatable_tokens("@someone 说"), " 说")

    def test_keeps_real_words(self):
        self.assertEqual(
            strip_untranslatable_tokens("Clown's Artificial Intelligence#cosplay #shorts"),
            "Clown's Artificial Intelligence ",
        )
        self.assertEqual(strip_untranslatable_tokens("Breaking news"), "Breaking news")


class TestHashtagRatio(unittest.TestCase):
    def test_pure_hashtag_is_one(self):
        self.assertAlmostEqual(hashtag_ratio("#shorts"), 1.0)
        self.assertAlmostEqual(
            hashtag_ratio("#generalknowledge #fact #technology #science"),
            1.0,
            places=2,
        )

    def test_no_hashtag_is_zero(self):
        self.assertEqual(hashtag_ratio("Breaking news"), 0.0)
        self.assertEqual(hashtag_ratio("人工智能"), 0.0)
        self.assertEqual(hashtag_ratio(""), 0.0)
        self.assertEqual(hashtag_ratio("   "), 0.0)

    def test_mixed_ratio(self):
        # 小丑标题：hashtag 占约一半（2026-11-16 20:28 截图 Top 10 #3）。
        ratio = hashtag_ratio("Clown's Artificial Intelligence#cosplay #shorts #SpiderMan#kpop")
        self.assertGreater(ratio, 0.4)
        self.assertLess(ratio, 0.6)
        # 纯 hashtag 印尼标题：占比约 0.93（同批截图 Top 10 #2）。
        ratio = hashtag_ratio(
            "最新消息#rakyat#KPK #klipingberita #politikindonesia #fayruseh #dpr"
        )
        self.assertGreater(ratio, 0.8)


if __name__ == "__main__":
    unittest.main()
