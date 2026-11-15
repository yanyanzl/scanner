"""GDELT 限流修复测试。

背景：2026-11-15 GDELT 连续 3 次失败——两次 HTTP 429，一次返回 200
但正文是纯文本限流提示 "Please limit requests to one every 5 seconds …"
而非 JSON（这正是 "Expecting value: line 1 column 1" 的原因）。
修复三件套：带 User-Agent；429 / 限流文本 / 5xx 时退避重试
（遵从 Retry-After 头，最多 3 次）；collector 把 GDELT 放宽到
每 60 分钟 1 次（GDELT 没有互动量数据，降频无损失）。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import collector
from trend_radar.adapters import gdelt

THROTTLE_TEXT = (
    "Please limit requests to one every 5 seconds or contact "
    "kalev.leetaru5@gmail.com for larger queries."
)


class _FakeResp:
    """requests.Response 的最小替身。"""

    def __init__(self, status_code=200, json_data=None, text="", headers=None, json_raises=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("No JSON object could be decoded")
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Error")
            err.response = self
            raise err


def _article(title="Tech headline", url="https://example.com/a"):
    return {
        "articles": [
            {
                "title": title,
                "url": url,
                "seendate": "20261115T100000Z",
                "domain": "example.com",
                "language": "English",
            }
        ]
    }


class TestGdeltRetry(unittest.TestCase):
    def test_success_sends_user_agent(self):
        with (
            mock.patch.object(gdelt.requests, "get") as get,
            mock.patch.object(gdelt.time, "sleep") as sleep,
        ):
            get.return_value = _FakeResp(json_data=_article())
            items = gdelt.fetch("technology")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["platform"], "gdelt")
        _, kwargs = get.call_args
        self.assertIn("GlobalTrendRadar", kwargs["headers"]["User-Agent"])
        sleep.assert_not_called()

    def test_429_then_success_honors_retry_after(self):
        resp429 = _FakeResp(status_code=429, headers={"Retry-After": "7"})
        with (
            mock.patch.object(gdelt.requests, "get") as get,
            mock.patch.object(gdelt.time, "sleep") as sleep,
        ):
            get.side_effect = [resp429, _FakeResp(json_data=_article())]
            items = gdelt.fetch("technology")
        self.assertEqual(len(items), 1)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(7)

    def test_throttle_text_then_success(self):
        throttle = _FakeResp(status_code=200, text=THROTTLE_TEXT, json_raises=True)
        with (
            mock.patch.object(gdelt.requests, "get") as get,
            mock.patch.object(gdelt.time, "sleep") as sleep,
        ):
            get.side_effect = [throttle, _FakeResp(json_data=_article())]
            items = gdelt.fetch("technology")
        self.assertEqual(len(items), 1)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(10)  # 第一次重试等待 10 秒

    def test_persistent_429_returns_empty_after_3_attempts(self):
        with (
            mock.patch.object(gdelt.requests, "get") as get,
            mock.patch.object(gdelt.time, "sleep") as sleep,
        ):
            get.return_value = _FakeResp(status_code=429)
            items = gdelt.fetch("technology")
        self.assertEqual(items, [])
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(10), mock.call(30)])

    def test_500_then_success(self):
        with (
            mock.patch.object(gdelt.requests, "get") as get,
            mock.patch.object(gdelt.time, "sleep") as sleep,
        ):
            get.side_effect = [_FakeResp(status_code=500), _FakeResp(json_data=_article())]
            items = gdelt.fetch("technology")
        self.assertEqual(len(items), 1)
        sleep.assert_called_once_with(10)

    def test_connection_error_no_retry(self):
        with mock.patch.object(gdelt.requests, "get") as get:
            get.side_effect = requests.ConnectionError("DNS failure")
            items = gdelt.fetch("technology")
        self.assertEqual(items, [])
        self.assertEqual(get.call_count, 1)

    def test_empty_query_returns_empty_without_request(self):
        with mock.patch.object(gdelt.requests, "get") as get:
            self.assertEqual(gdelt.fetch(""), [])
        get.assert_not_called()


def _config():
    return {
        "scan_interval_minutes": 15,
        "db_path": "data/trends.sqlite3",
        "sources": {
            "rss": {"enabled": False},
            "gdelt": {"enabled": True, "query": "technology", "interval_minutes": 60},
            "reddit": {"enabled": False},
            "youtube": {"enabled": False},
            "x": {"enabled": False},
            "tiktok": {"enabled": False},
        },
    }


class TestGdeltIntervalGate(unittest.TestCase):
    def test_hourly_gate_skips_second_round(self):
        """第一轮采 GDELT，紧接着第二轮跳过；61 分钟后第三轮再采。"""
        import tempfile

        item = {
            "platform": "gdelt",
            "title": "Tech headline",
            "url": "https://example.com/a",
            "published_at": "2026-11-15T10:00:00+00:00",
            "first_seen": "2026-11-15T10:00:00+00:00",
            "engagement": 0,
            "source": "example.com",
        }
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            fetch = mock.Mock(return_value=[item])
            with (
                mock.patch.object(collector, "BASE_DIR", tmp),
                mock.patch.object(collector.gdelt, "fetch", fetch),
            ):
                first = collector.collect_once(_config())
                second = collector.collect_once(_config())
                state = tmp / "data" / ".gdelt_last_run"
                state.write_text(
                    (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat(),
                    encoding="utf-8",
                )
                third = collector.collect_once(_config())
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(first["gdelt"], 1)
            self.assertEqual(second["gdelt"], 0)
            self.assertEqual(third["gdelt"], 1)
            self.assertTrue((tmp / "data" / ".gdelt_last_run").exists())


if __name__ == "__main__":
    unittest.main()
