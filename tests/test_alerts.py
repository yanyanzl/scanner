"""P3 Telegram 提醒测试：requests.post 全部 mock，不联网。

同时验证 token 安全：日志和返回的 errors 里绝不出现 token。
"""
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from trend_radar.alerts import format_burst_message, send_burst_alerts

TOKEN = "123456:TEST-TOKEN-abcdef"
CHAT_ID = "987654321"


def _burst(event_id="evt_abc123", reasons=("growth_spike", "score_surge")):
    return {
        "event_id": event_id,
        "name": "Kanak News 2PM Headlines",
        "reasons": list(reasons),
        "current": {
            "collected_at": "2026-11-15T13:18:00+00:00",
            "item_count": 3,
            "platform_count": 2,
            "total_score": 12.0,
            "max_score": 5.0,
            "max_growth": 704.0,
            "platforms": "rss,youtube",
        },
        "previous": {
            "collected_at": "2026-11-15T12:48:00+00:00",
            "item_count": 2,
            "platform_count": 1,
            "total_score": 4.0,
            "max_score": 3.0,
            "max_growth": 100.0,
            "platforms": "rss",
        },
        "detected_at": "2026-11-15T13:18:00+00:00",
    }


class _LogCapture(logging.Handler):
    """抓下指定 logger 的所有记录文本，供检查 token 是否泄漏。"""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class TestFormatBurstMessage(unittest.TestCase):
    def test_contains_name_reasons_and_values(self):
        text = format_burst_message(_burst())
        self.assertIn("Kanak News 2PM Headlines", text)
        self.assertIn("增速突增", text)
        self.assertIn("总分跃升", text)
        self.assertIn("100.0 → 704.0/小时", text)
        self.assertIn("4.0 → 12.0", text)
        self.assertIn("rss,youtube", text)
        self.assertIn("evt_abc123", text)

    def test_new_multi_platform_without_previous(self):
        burst = _burst(reasons=("new_multi_platform",))
        burst["previous"] = None  # 首次出现，没有上一轮可比
        text = format_burst_message(burst)
        self.assertIn("新事件首次跨平台", text)
        self.assertNotIn("→", text)  # 没有上一轮，不写对比箭头
        self.assertIn("增长：704.0/小时", text)

    def test_long_name_is_truncated(self):
        burst = _burst()
        burst["name"] = "很长的标题" * 200
        self.assertLessEqual(len(format_burst_message(burst)), 500)


class TestSendBurstAlerts(unittest.TestCase):
    def setUp(self):
        self.capture = _LogCapture()
        self.logger = logging.getLogger("trend_radar.alerts")
        self.logger.addHandler(self.capture)
        self.logger.setLevel(logging.INFO)

    def tearDown(self):
        self.logger.removeHandler(self.capture)

    def _assert_no_token_leak(self):
        for message in self.capture.messages:
            self.assertNotIn(TOKEN, message)

    def test_no_credentials_no_http_call(self):
        bursts = [_burst(), _burst("evt_def456")]
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("trend_radar.alerts.requests.post") as post,
        ):
            result = send_burst_alerts(bursts)
        post.assert_not_called()
        self.assertEqual(result, {"sent": 0, "skipped": 2, "errors": []})
        self._assert_no_token_leak()

    def test_only_one_of_token_or_chat_missing(self):
        with (
            mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}, clear=True),
            mock.patch("trend_radar.alerts.requests.post") as post,
        ):
            result = send_burst_alerts([_burst()])
        post.assert_not_called()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_explicit_args_override_missing_env(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True}
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("trend_radar.alerts.requests.post", return_value=resp) as post,
        ):
            result = send_burst_alerts([_burst()], bot_token=TOKEN, chat_id=CHAT_ID)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(post.call_count, 1)
        args, kwargs = post.call_args
        self.assertEqual(args[0], f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(kwargs["json"]["chat_id"], CHAT_ID)
        self.assertIn("Kanak News 2PM Headlines", kwargs["json"]["text"])
        self.assertIn("增速突增", kwargs["json"]["text"])
        self._assert_no_token_leak()

    def test_env_credentials_used(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True}
        env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("trend_radar.alerts.requests.post", return_value=resp) as post,
        ):
            result = send_burst_alerts([_burst()])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(post.call_args[0][0], f"https://api.telegram.org/bot{TOKEN}/sendMessage")

    def test_failure_does_not_stop_others(self):
        ok_resp = mock.Mock(status_code=200)
        ok_resp.json.return_value = {"ok": True}
        bad_resp = mock.Mock(status_code=403)
        bursts = [_burst("evt_1"), _burst("evt_2"), _burst("evt_3")]
        with mock.patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID}, clear=True
        ), mock.patch(
            "trend_radar.alerts.requests.post",
            side_effect=[ok_resp, requests.RequestException("connection reset"), bad_resp],
        ) as post:
            result = send_burst_alerts(bursts)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual([e["event_id"] for e in result["errors"]], ["evt_2", "evt_3"])
        self.assertIn("403", result["errors"][1]["error"])
        for error in result["errors"]:
            self.assertNotIn(TOKEN, error["error"])
        self._assert_no_token_leak()

    def test_empty_bursts_is_noop(self):
        with mock.patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID}, clear=True
        ), mock.patch("trend_radar.alerts.requests.post") as post:
            result = send_burst_alerts([])
        post.assert_not_called()
        self.assertEqual(result, {"sent": 0, "skipped": 0, "errors": []})


if __name__ == "__main__":
    unittest.main()
