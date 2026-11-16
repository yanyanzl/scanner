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


def _burst(event_id="evt_abc123", reasons=("growth_spike", "score_surge"), name=None):
    return {
        "event_id": event_id,
        "name": name or "Kanak News 2PM Headlines",
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


class TestFormatBurstMessageTime(unittest.TestCase):
    """提醒正文里的人类可读时间行（2026-11-16 第二批改进）。

    标记第一行不动；时间行取 burst 的 detected_at（UTC），用
    timefmt 转本机当地时间。精确值由 helper 的测试覆盖，这里用
    mock 锁定“传了 detected_at、行存在、位置在原因行之后”。
    """

    def test_time_line_present(self):
        with mock.patch(
            "trend_radar.alerts.format_local_time", return_value="11-15 21:18"
        ) as fmt:
            text = format_burst_message(_burst())
        fmt.assert_called_once_with("2026-11-15T13:18:00+00:00")
        self.assertIn("时间：11-15 21:18（伦敦时间）", text)
        lines = text.splitlines()
        self.assertLess(
            lines.index("原因：增速突增、总分跃升"),
            lines.index("时间：11-15 21:18（伦敦时间）"),
        )

    def test_time_line_real_format(self):
        # 不 mock：只断言形状，系统本地时区是什么都成立。
        text = format_burst_message(_burst())
        self.assertRegex(text, r"时间：\d{2}-\d{2} \d{2}:\d{2}（伦敦时间）")

    def test_no_time_line_when_detected_at_missing(self):
        burst = _burst()
        del burst["detected_at"]
        self.assertNotIn("时间：", format_burst_message(burst))
        burst2 = _burst()
        burst2["detected_at"] = "not a date"
        self.assertNotIn("时间：", format_burst_message(burst2))


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
        # 三个不同名字的爆发：去重不应把它们并成一条。
        bursts = [
            _burst("evt_1", name="Event One"),
            _burst("evt_2", name="Event Two"),
            _burst("evt_3", name="Event Three"),
        ]
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


class TestSendBurstAlertsDedup(unittest.TestCase):
    """去重：同一轮里归一化后名字相同的爆发只发第一条（2026-11-15 实测：
    同一个马拉雅拉姆语小时直播标题在一轮里分成了 3 个事件、发了 3 条
    一模一样的提醒）。"""

    def _send(self, bursts):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True}
        env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("trend_radar.alerts.requests.post", return_value=resp) as post,
        ):
            result = send_burst_alerts(bursts)
        return result, post

    def test_same_name_three_bursts_one_alert(self):
        name = "Today Breaking News LIVE | Top Headlines Of The Hour | 15.11.2026"
        bursts = [
            _burst("evt_1", reasons=("new_multi_platform",), name=name),
            _burst("evt_2", reasons=("new_multi_platform",), name=name),
            _burst("evt_3", reasons=("new_multi_platform",), name=name),
        ]
        result, post = self._send(bursts)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 2)  # 去重掉的 2 条算 skipped
        self.assertEqual(result["errors"], [])
        # 发的是第一条（事件 ID 保留第一个）。
        self.assertIn("evt_1", post.call_args.kwargs["json"]["text"])

    def test_different_names_all_sent(self):
        bursts = [
            _burst("evt_1", name="Event One"),
            _burst("evt_2", name="Event Two"),
            _burst("evt_3", name="Event Three"),
        ]
        result, post = self._send(bursts)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(result["sent"], 3)
        self.assertEqual(result["skipped"], 0)

    def test_same_name_different_case_and_whitespace_one_alert(self):
        bursts = [
            _burst("evt_1", name="  Today Breaking News LIVE  "),
            _burst("evt_2", name="today   breaking news live"),
            _burst("evt_3", name="TODAY BREAKING NEWS LIVE"),
        ]
        result, post = self._send(bursts)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 2)

    def test_name_zh_takes_precedence_for_dedup(self):
        # 原名不同、中文名相同：显示用的中文名相同，仍只发一条。
        b1 = _burst("evt_1", name="Original Title A")
        b1["name_zh"] = "今日突发新闻"
        b2 = _burst("evt_2", name="Original Title B")
        b2["name_zh"] = "今日突发新闻"
        result, post = self._send([b1, b2])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["sent"], 1)

    def test_same_raw_name_split_by_translation_state_one_alert(self):
        """回归（2026-11-15 20:36 实测）：同名事件因为翻译状态不同拆成两条。

        6 个同名直播事件里，有的 burst 已有 name_zh（中文名），有的还是
        空——只按显示名（中文名优先）去重会把它们当成两个事件。去重键必须
        与翻译状态无关：原名相同就是同一条提醒。
        """
        name = "Today Breaking News LIVE | Top Headlines Of The Hour | 15.11.2026"
        b1 = _burst("evt_1", name=name)
        b1["name_zh"] = "今日突发新闻直播 | 本时头条新闻 | 本小时头条 | 15.11.2026"
        b2 = _burst("evt_2", name=name)
        b2["name_zh"] = None
        b3 = _burst("evt_3", name=name)
        b3["name_zh"] = None
        result, post = self._send([b1, b2, b3])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 2)
        # 留下的是第一条（中文名那条）。
        self.assertIn("evt_1", post.call_args.kwargs["json"]["text"])

    def test_same_raw_name_different_translations_one_alert(self):
        # 原名相同、中文名不同（两次翻译结果不一致）：仍是同一条提醒。
        name = "Today Breaking News LIVE | Top Headlines Of The Hour | 15.11.2026"
        b1 = _burst("evt_1", name=name)
        b1["name_zh"] = "今日突发新闻直播"
        b2 = _burst("evt_2", name=name)
        b2["name_zh"] = "今日突发新闻实况"
        result, post = self._send([b1, b2])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["sent"], 1)

    def test_dedup_keeps_first_occurrence_order(self):
        bursts = [
            _burst("evt_1", name="Shared Name"),
            _burst("evt_2", name="Other Event"),
            _burst("evt_3", name="Shared Name"),
        ]
        result, post = self._send(bursts)
        self.assertEqual(post.call_count, 2)
        texts = [c.kwargs["json"]["text"] for c in post.call_args_list]
        self.assertIn("evt_1", texts[0])
        self.assertIn("evt_2", texts[1])


if __name__ == "__main__":
    unittest.main()
