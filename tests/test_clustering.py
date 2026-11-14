"""P1 v0.1 事件聚类测试：TF-IDF + 余弦相似度 + 并查集。

注意：这里的多平台标题是人工构造的，只证明算法逻辑；
真实多平台聚类效果尚未验证（Reddit / YouTube 尚未启用）。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar.clustering import cluster_from_db, cluster_items, tokenize


def _item(title, platform, url, score=0.5, growth=0.0):
    return {
        "title": title,
        "platform": platform,
        "url": url,
        "score": score,
        "growth": growth,
        "first_seen": "2026-11-14T12:00:00+00:00",
        "published_at": "2026-11-14T12:00:00+00:00",
    }


class TestClustering(unittest.TestCase):
    def test_same_event_different_wording_clusters_together(self):
        """同一事件、不同措辞、不同平台 -> 一个事件。"""
        items = [
            _item("Apple unveils new iPhone with AI chip", "rss", "https://a.example/1", 0.8),
            _item("Apple launches new iPhone featuring AI chip", "gdelt", "https://b.example/2", 0.7),
            _item("New iPhone with AI chip unveiled by Apple", "reddit", "https://c.example/3", 0.6),
        ]
        events = cluster_items(items)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["item_count"], 3)
        self.assertEqual(events[0]["platform_count"], 3)
        self.assertIn("iPhone", events[0]["name"])

    def test_unrelated_titles_stay_separate(self):
        """不相关的标题 -> 各自成事件。"""
        items = [
            _item("Local football team wins championship final", "rss", "https://a.example/1"),
            _item("Scientists discover new deep sea species", "gdelt", "https://b.example/2"),
            _item("Stock market rises after interest rate cut", "rss", "https://c.example/3"),
        ]
        events = cluster_items(items)
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["item_count"] == 1 for e in events))

    def test_same_title_cross_platform_counts(self):
        """同一标题出现在 2+ 平台 -> 一个事件，platform_count 正确。"""
        items = [
            _item("Global chip shortage hits car makers", "rss", "https://a.example/1", 0.9),
            _item("Global chip shortage hits car makers", "gdelt", "https://b.example/2", 0.8),
            _item("Global chip shortage hits car makers", "youtube", "https://c.example/3", 0.7),
        ]
        events = cluster_items(items)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["platform_count"], 3)
        self.assertEqual(events[0]["platforms"], ["gdelt", "rss", "youtube"])
        self.assertAlmostEqual(events[0]["total_score"], 2.4)
        self.assertAlmostEqual(events[0]["max_score"], 0.9)

    def test_empty_input_returns_empty(self):
        self.assertEqual(cluster_items([]), [])
        self.assertEqual(cluster_items([{"title": "", "platform": "rss", "url": ""}]), [])

    def test_chinese_titles_cluster(self):
        """中文标题：二字 token 应把同一事件聚到一起。"""
        items = [
            _item("苹果发布新款 iPhone 手机", "rss", "https://a.example/1", 0.8),
            _item("苹果推出搭载 AI 芯片的新 iPhone", "gdelt", "https://b.example/2", 0.7),
        ]
        events = cluster_items(items)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["platform_count"], 2)

    def test_min_platforms_filters_single_platform_events(self):
        items = [
            _item("Global chip shortage hits car makers", "rss", "https://a.example/1"),
            _item("Local football team wins championship final", "rss", "https://b.example/2"),
        ]
        self.assertEqual(len(cluster_items(items, min_platforms=2)), 0)
        self.assertEqual(len(cluster_items(items, min_platforms=1)), 2)

    def test_tokenize_drops_stopwords(self):
        tokens = tokenize("The Apple iPhone and the AI chip")
        self.assertIn("apple", tokens)
        self.assertIn("iphone", tokens)
        self.assertNotIn("the", tokens)
        self.assertNotIn("and", tokens)

    def test_cluster_from_db_missing_db_returns_empty(self):
        self.assertEqual(cluster_from_db("/tmp/does-not-exist-trends.sqlite3"), [])


if __name__ == "__main__":
    unittest.main()
