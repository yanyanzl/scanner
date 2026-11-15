"""免费新闻 API 测试：mock requests，绝不调真实接口（没有 key）。

覆盖：无 key 不发请求；mock 成功响应解析成归一化条目；
429/401/坏 JSON 返回 [] 不抛异常；NYT 两次调用之间隔 12 秒；
collector 限频门控（间隔内第二轮跳过、无 key 时不记时间）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar.adapters import guardian, newsapi, newsdata, nyt
from trend_radar.collector import collect_once


def _mock_response(payload, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _mock_error(status):
    resp = mock.Mock()
    resp.status_code = status
    resp.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status}")
    return resp


def _base_config(db_path):
    return {
        "scan_interval_minutes": 15,
        "db_path": str(db_path),
        "sources": {
            "rss": {"enabled": True, "feeds": []},
            "gdelt": {"enabled": False},
            "reddit": {"enabled": False},
            "youtube": {"enabled": False},
            "x": {"enabled": False},
            "tiktok": {"enabled": False},
        },
        "burst": {},
    }


class TestNewsApiNoKey(unittest.TestCase):
    def test_no_key_returns_empty_without_http(self):
        cases = [
            (newsapi, "NEWSAPI_KEY"),
            (guardian, "GUARDIAN_API_KEY"),
            (nyt, "NYT_API_KEY"),
            (newsdata, "NEWSDATA_API_KEY"),
        ]
        for module, key in cases:
            with self.subTest(api=key):
                with (
                    mock.patch.dict(os.environ, {key: ""}, clear=False),
                    mock.patch.object(module.requests, "get") as fake_get,
                ):
                    self.assertEqual(module.fetch(["breaking news"]), [])
                fake_get.assert_not_called()


class TestNewsApiSuccess(unittest.TestCase):
    def test_newsapi_parses_items(self):
        payload = {
            "articles": [
                {
                    "title": "Markets rally on rate cut hopes",
                    "url": "https://example.com/market",
                    "publishedAt": "2026-11-15T10:00:00Z",
                    "source": {"name": "Example News"},
                },
                {"title": "[Removed]", "url": "https://example.com/gone"},
                {"title": "", "url": "https://example.com/empty"},
            ]
        }
        with (
            mock.patch.dict(os.environ, {"NEWSAPI_KEY": "k"}),
            mock.patch.object(newsapi.requests, "get", return_value=_mock_response(payload)) as fake_get,
        ):
            items = newsapi.fetch(["breaking news"])
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["platform"], "newsapi")
        self.assertEqual(item["title"], "Markets rally on rate cut hopes")
        self.assertEqual(item["url"], "https://example.com/market")
        self.assertEqual(item["published_at"], "2026-11-15T10:00:00Z")
        self.assertEqual(item["engagement"], 0)
        fake_get.assert_called_once()
        params = fake_get.call_args.kwargs["params"]
        self.assertEqual(params["q"], "breaking news")
        self.assertEqual(params["sortBy"], "publishedAt")

    def test_guardian_parses_items(self):
        payload = {
            "response": {
                "results": [
                    {
                        "webTitle": "AI boom lifts chip stocks",
                        "webUrl": "https://example.com/ai",
                        "webPublicationDate": "2026-11-15T09:00:00Z",
                        "sectionName": "Technology",
                    }
                ]
            }
        }
        with (
            mock.patch.dict(os.environ, {"GUARDIAN_API_KEY": "k"}),
            mock.patch.object(guardian.requests, "get", return_value=_mock_response(payload)),
        ):
            items = guardian.fetch(["technology"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["platform"], "guardian")
        self.assertEqual(items[0]["title"], "AI boom lifts chip stocks")
        self.assertEqual(items[0]["source"], "Technology")
        self.assertEqual(items[0]["engagement"], 0)

    def test_nyt_parses_items(self):
        payload = {
            "response": {
                "docs": [
                    {
                        "headline": {"main": "Senate passes spending bill"},
                        "web_url": "https://example.com/senate",
                        "pub_date": "2026-11-15T08:00:00Z",
                    }
                ]
            }
        }
        with (
            mock.patch.dict(os.environ, {"NYT_API_KEY": "k"}),
            mock.patch.object(nyt.requests, "get", return_value=_mock_response(payload)),
            mock.patch.object(nyt.time, "sleep") as fake_sleep,
        ):
            items = nyt.fetch(["war OR geopolitics"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["platform"], "nyt")
        self.assertEqual(items[0]["title"], "Senate passes spending bill")
        self.assertEqual(items[0]["url"], "https://example.com/senate")
        fake_sleep.assert_not_called()  # 只有一个查询，不用等

    def test_newsdata_parses_items(self):
        payload = {
            "results": [
                {
                    "title": "Oil prices climb after OPEC decision",
                    "link": "https://example.com/oil",
                    "pubDate": "2026-11-15T07:00:00Z",
                    "source_name": "Example Wire",
                }
            ]
        }
        with (
            mock.patch.dict(os.environ, {"NEWSDATA_API_KEY": "k"}),
            mock.patch.object(newsdata.requests, "get", return_value=_mock_response(payload)),
        ):
            items = newsdata.fetch(["stock market OR inflation OR interest rates"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["platform"], "newsdata")
        self.assertEqual(items[0]["title"], "Oil prices climb after OPEC decision")
        self.assertEqual(items[0]["source"], "Example Wire")
        self.assertEqual(items[0]["engagement"], 0)


class TestNewsApiErrors(unittest.TestCase):
    def test_http_error_returns_empty(self):
        for module, key in [
            (newsapi, "NEWSAPI_KEY"),
            (guardian, "GUARDIAN_API_KEY"),
            (nyt, "NYT_API_KEY"),
            (newsdata, "NEWSDATA_API_KEY"),
        ]:
            with self.subTest(api=key):
                with (
                    mock.patch.dict(os.environ, {key: "k"}),
                    mock.patch.object(module.requests, "get", return_value=_mock_error(429)),
                    mock.patch.object(nyt.time, "sleep"),
                ):
                    self.assertEqual(module.fetch(["breaking news"]), [])

    def test_bad_json_returns_empty(self):
        resp = mock.Mock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("bad json")
        for module, key in [
            (newsapi, "NEWSAPI_KEY"),
            (guardian, "GUARDIAN_API_KEY"),
            (nyt, "NYT_API_KEY"),
            (newsdata, "NEWSDATA_API_KEY"),
        ]:
            with self.subTest(api=key):
                with (
                    mock.patch.dict(os.environ, {key: "k"}),
                    mock.patch.object(module.requests, "get", return_value=resp),
                    mock.patch.object(nyt.time, "sleep"),
                ):
                    self.assertEqual(module.fetch(["breaking news"]), [])

    def test_nyt_sleeps_between_queries(self):
        payload = {"response": {"docs": []}}
        with (
            mock.patch.dict(os.environ, {"NYT_API_KEY": "k"}),
            mock.patch.object(nyt.requests, "get", return_value=_mock_response(payload)),
            mock.patch.object(nyt.time, "sleep") as fake_sleep,
        ):
            nyt.fetch(["breaking news", "technology"])
        fake_sleep.assert_called_once_with(12)


class TestNewsApiWiring(unittest.TestCase):
    def test_interval_gating_skips_second_round(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            cfg = _base_config(db_path)
            cfg["sources"]["newsapi"] = {"enabled": True, "interval_minutes": 90}
            item = {
                "platform": "newsapi",
                "title": "Test headline",
                "url": "https://example.com/t",
                "published_at": "2026-11-15T10:00:00+00:00",
                "first_seen": "2026-11-15T10:00:00+00:00",
                "engagement": 0,
                "source": "newsapi",
            }
            with (
                mock.patch.dict(os.environ, {"NEWSAPI_KEY": "test-key"}),
                mock.patch(
                    "trend_radar.collector.rss.fetch",
                    mock.Mock(return_value=[]),
                ),
                mock.patch(
                    "trend_radar.collector.newsapi.fetch",
                    mock.Mock(return_value=[item]),
                ) as fake_fetch,
            ):
                first = collect_once(config=cfg)
                second = collect_once(config=cfg)
            self.assertEqual(first["newsapi"], 1)
            self.assertEqual(second["newsapi"], 0)
            fake_fetch.assert_called_once()
            self.assertTrue((Path(d) / ".newsapi_last_run").exists())

    def test_no_key_state_file_not_marked(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            cfg = _base_config(db_path)
            cfg["sources"]["guardian"] = {"enabled": True, "interval_minutes": 15}
            with (
                mock.patch.dict(os.environ, {"GUARDIAN_API_KEY": ""}),
                mock.patch(
                    "trend_radar.collector.rss.fetch",
                    mock.Mock(return_value=[]),
                ),
            ):
                counts = collect_once(config=cfg)
            self.assertEqual(counts["guardian"], 0)
            self.assertFalse((Path(d) / ".guardian_last_run").exists())


if __name__ == "__main__":
    unittest.main()
