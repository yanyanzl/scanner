"""Muse 对话提醒接线测试：collect_once 检出爆发后调用 muse_chat 发送。

send_alerts_to_muse_chat 本身的测试在 tests/test_muse_chat.py；这里只测
接线：与 Telegram 发同一批 alert_bursts（已过质量门槛、去重、重复出现
过滤），计数进 muse_chat_sent / muse_chat_failed；muse_chat.enabled=false
时不调用；发送异常不影响采集结果；与 telegram.enabled 无关。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import collector


def _config(muse_enabled=True, telegram_enabled=True):
    return {
        "scan_interval_minutes": 15,
        "db_path": "data/trends.sqlite3",
        "telegram": {"enabled": telegram_enabled},
        "muse_chat": {"enabled": muse_enabled},
        "sources": {
            "rss": {"enabled": True, "feeds": []},
            "gdelt": {"enabled": False},
            "reddit": {"enabled": False},
            "youtube": {"enabled": False},
            "x": {"enabled": False},
            "tiktok": {"enabled": False},
        },
    }


_BURST = {
    "event_id": "evt_test123",
    "name": "Test Event",
    "reasons": ["new_multi_platform"],
    "is_recurrence": False,
    "current": {
        "item_count": 2,
        "platform_count": 2,
        "total_score": 12.0,
        "max_score": 8.0,
        "max_growth": 704.0,
        "platforms": "rss,youtube",
    },
    "previous": None,
    "detected_at": "2026-11-15T13:00:00+00:00",
}

_RECURRENCE_BURST = dict(
    _BURST,
    event_id="evt_recur1",
    name="Recurring Live Stream",
    is_recurrence=True,
)


class TestCollectorMuseChat(unittest.TestCase):
    def _run(self, muse_enabled=True, telegram_enabled=True, bursts=None, muse_side_effect=None):
        item = {
            "platform": "rss",
            "title": "Test Event",
            "url": "https://example.com/a",
            "published_at": "2026-11-15T10:00:00+00:00",
            "first_seen": "2026-11-15T10:00:00+00:00",
            "engagement": 0,
            "source": "example.com",
        }
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            telegram_send = mock.Mock(return_value={"sent": 1, "skipped": 0, "errors": []})
            muse_send = mock.Mock(return_value={"sent": 1, "failed": []})
            if muse_side_effect is not None:
                muse_send.side_effect = muse_side_effect
            with (
                mock.patch.object(collector, "BASE_DIR", tmp),
                mock.patch.object(collector.rss, "fetch", mock.Mock(return_value=[item])),
                mock.patch(
                    "trend_radar.clustering.cluster_from_db",
                    mock.Mock(return_value=[{"event_id": "evt_test123"}]),
                ),
                mock.patch(
                    "trend_radar.clustering.persist_events", mock.Mock(return_value=None)
                ),
                mock.patch(
                    "trend_radar.burst.detect_bursts",
                    mock.Mock(return_value=list(bursts) if bursts is not None else [_BURST]),
                ),
                mock.patch("trend_radar.alerts.send_burst_alerts", telegram_send),
                mock.patch(
                    "trend_radar.muse_chat.send_alerts_to_muse_chat", muse_send
                ),
            ):
                counts = collector.collect_once(_config(muse_enabled, telegram_enabled))
            return counts, telegram_send, muse_send

    def test_muse_chat_called_with_same_bursts_as_telegram(self):
        counts, telegram_send, muse_send = self._run()
        telegram_send.assert_called_once()
        muse_send.assert_called_once()
        self.assertEqual(muse_send.call_args.args[0], [_BURST])
        self.assertEqual(telegram_send.call_args.args[0], [_BURST])
        self.assertEqual(counts["muse_chat_sent"], 1)
        self.assertEqual(counts["muse_chat_failed"], 0)

    def test_muse_chat_disabled_skips(self):
        counts, _, muse_send = self._run(muse_enabled=False)
        muse_send.assert_not_called()
        self.assertEqual(counts["muse_chat_sent"], 0)
        self.assertEqual(counts["muse_chat_failed"], 0)

    def test_muse_chat_independent_of_telegram_enabled(self):
        """telegram 关了，Muse 对话提醒照样发（两者开关独立）。"""
        counts, telegram_send, muse_send = self._run(telegram_enabled=False)
        telegram_send.assert_not_called()
        muse_send.assert_called_once()
        self.assertEqual(counts["muse_chat_sent"], 1)

    def test_muse_chat_exception_does_not_break_round(self):
        counts, _, muse_send = self._run(muse_side_effect=RuntimeError("boom"))
        muse_send.assert_called_once()
        self.assertEqual(counts["muse_chat_sent"], 0)
        self.assertEqual(counts["muse_chat_failed"], 1)
        self.assertEqual(counts["saved"], 1)

    def test_muse_chat_failed_result_counted_via_return(self):
        def _fail(bursts, cfg):
            return {"sent": 0, "failed": [{"event_id": "evt_test123", "error": "no_muse_tab"}]}

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            item = {
                "platform": "rss",
                "title": "Test Event",
                "url": "https://example.com/a",
                "published_at": "2026-11-15T10:00:00+00:00",
                "first_seen": "2026-11-15T10:00:00+00:00",
                "engagement": 0,
                "source": "example.com",
            }
            with (
                mock.patch.object(collector, "BASE_DIR", tmp),
                mock.patch.object(collector.rss, "fetch", mock.Mock(return_value=[item])),
                mock.patch(
                    "trend_radar.clustering.cluster_from_db",
                    mock.Mock(return_value=[{"event_id": "evt_test123"}]),
                ),
                mock.patch(
                    "trend_radar.clustering.persist_events", mock.Mock(return_value=None)
                ),
                mock.patch(
                    "trend_radar.burst.detect_bursts", mock.Mock(return_value=[_BURST])
                ),
                mock.patch(
                    "trend_radar.alerts.send_burst_alerts",
                    mock.Mock(return_value={"sent": 1, "skipped": 0, "errors": []}),
                ),
                mock.patch(
                    "trend_radar.muse_chat.send_alerts_to_muse_chat",
                    mock.Mock(side_effect=_fail),
                ),
            ):
                counts = collector.collect_once(_config(True, True))
        self.assertEqual(counts["muse_chat_sent"], 0)
        self.assertEqual(counts["muse_chat_failed"], 1)

    def test_recurrence_burst_excluded_from_muse_chat(self):
        counts, _, muse_send = self._run(bursts=[_RECURRENCE_BURST])
        muse_send.assert_not_called()
        self.assertEqual(counts["bursts_recurrence"], 1)
        self.assertEqual(counts["muse_chat_sent"], 0)
        self.assertEqual(counts["muse_chat_failed"], 0)


if __name__ == "__main__":
    unittest.main()
