"""搜索主题测试：topics 拍扁/去重、聚合 feed URL 生成、GDELT 查询拼接、
主题流入 adapter（mock）与 RSS feed 生成。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import topics as topics_mod
from trend_radar.collector import collect_once


class TestLoadTopics(unittest.TestCase):
    def test_defaults_when_section_missing(self):
        self.assertEqual(
            topics_mod.load_topics({}),
            [
                "breaking news",
                "stock market OR inflation OR interest rates",
                "technology",
                "artificial intelligence OR AI",
                "war OR geopolitics",
            ],
        )

    def test_custom_topics_flattened_deduped(self):
        cfg = {
            "topics": {
                "breaking": ["breaking news"],
                "custom": ["climate summit", "breaking news", "  "],
                "single": "space exploration",
            }
        }
        self.assertEqual(
            topics_mod.load_topics(cfg),
            ["breaking news", "climate summit", "space exploration"],
        )

    def test_empty_section_falls_back_to_defaults(self):
        self.assertEqual(
            topics_mod.load_topics({"topics": {}}), topics_mod.default_topics()
        )
        self.assertEqual(
            topics_mod.load_topics({"topics": "not a dict"}),
            topics_mod.default_topics(),
        )


class TestAggregatorUrls(unittest.TestCase):
    def test_google_news_urls(self):
        urls = topics_mod.google_news_feed_urls(
            ["breaking news", "stock market OR inflation OR interest rates"]
        )
        self.assertEqual(urls[0], topics_mod.GOOGLE_NEWS_TOP_STORIES_URL)
        self.assertEqual(len(urls), 3)
        parsed = urlparse(urls[1])
        self.assertEqual(parsed.netloc, "news.google.com")
        self.assertEqual(parsed.path, "/rss/search")
        self.assertEqual(parse_qs(parsed.query)["q"], ["breaking news"])
        parsed2 = urlparse(urls[2])
        self.assertEqual(
            parse_qs(parsed2.query)["q"],
            ["stock market OR inflation OR interest rates"],
        )
        # URL 里不能有裸空格（必须百分号/加号编码）。
        self.assertNotIn(" ", urls[1])
        self.assertNotIn(" ", urls[2])

    def test_bing_news_urls(self):
        urls = topics_mod.bing_news_feed_urls(["technology"])
        self.assertEqual(len(urls), 1)
        parsed = urlparse(urls[0])
        self.assertEqual(parsed.netloc, "www.bing.com")
        self.assertEqual(parsed.path, "/news/search")
        query = parse_qs(parsed.query)
        self.assertEqual(query["q"], ["technology"])
        self.assertEqual(query["format"], ["rss"])

    def test_gdelt_query(self):
        self.assertEqual(
            topics_mod.gdelt_query(["breaking news", "war OR geopolitics"]),
            "breaking news OR (war OR geopolitics)",
        )


class TestTopicsWiring(unittest.TestCase):
    def _config(self, db_path, topics):
        return {
            "scan_interval_minutes": 15,
            "db_path": str(db_path),
            "topics": topics,
            "sources": {
                "rss": {"enabled": True, "feeds": []},
                "gdelt": {"enabled": False},
                "reddit": {"enabled": False},
                "youtube": {"enabled": False},
                "x": {"enabled": False},
                "tiktok": {"enabled": False},
                "newsapi": {"enabled": True, "interval_minutes": 90},
            },
            "burst": {},
        }

    def test_topics_flow_into_api_adapter(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            topics = {"custom": ["climate summit", "space exploration"]}
            with (
                mock.patch(
                    "trend_radar.collector.newsapi.fetch",
                    mock.Mock(return_value=[]),
                ) as fake_fetch,
                mock.patch(
                    "trend_radar.collector.rss.fetch",
                    mock.Mock(return_value=[]),
                ),
            ):
                collect_once(config=self._config(db_path, topics))
            fake_fetch.assert_called_once_with(["climate summit", "space exploration"])

    def test_topics_flow_into_generated_rss_feeds(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            topics = {"custom": ["climate summit"]}
            with (
                mock.patch(
                    "trend_radar.collector.newsapi.fetch",
                    mock.Mock(return_value=[]),
                ),
                mock.patch(
                    "trend_radar.collector.rss.fetch",
                    mock.Mock(return_value=[]),
                ) as fake_rss,
            ):
                collect_once(config=self._config(db_path, topics))
            feeds = fake_rss.call_args.args[0]
            self.assertIn(topics_mod.GOOGLE_NEWS_TOP_STORIES_URL, feeds)
            google_search = [f for f in feeds if "news.google.com/rss/search" in f]
            self.assertEqual(len(google_search), 1)
            self.assertEqual(
                parse_qs(urlparse(google_search[0]).query)["q"], ["climate summit"]
            )
            bing = [f for f in feeds if "bing.com/news/search" in f]
            self.assertEqual(len(bing), 1)
            self.assertEqual(
                parse_qs(urlparse(bing[0]).query)["q"], ["climate summit"]
            )

    def test_topic_change_needs_no_code_change(self):
        # 同一份代码，换 topics 配置就换查询词：两次运行拿到不同的查询词。
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            calls = []
            with (
                mock.patch(
                    "trend_radar.collector.newsapi.fetch",
                    mock.Mock(side_effect=lambda q: calls.append(list(q)) or []),
                ),
                mock.patch(
                    "trend_radar.collector.rss.fetch",
                    mock.Mock(return_value=[]),
                ),
            ):
                collect_once(
                    config=self._config(db_path, {"a": ["topic one"]})
                )
                # 第二轮在间隔内会跳过 newsapi：删 state 文件再跑。
                (Path(d) / ".newsapi_last_run").unlink(missing_ok=True)
                collect_once(
                    config=self._config(db_path, {"b": ["topic two"]})
                )
            self.assertEqual(calls, [["topic one"], ["topic two"]])


if __name__ == "__main__":
    unittest.main()
