"""P3 接线测试：collect_once 检出爆发后调用 Telegram 提醒。

send_burst_alerts 本身的测试在 tests/test_alerts.py；这里只测接线：
有爆发时调一次并把 sent/failed 写进 counts；telegram.enabled=false
时不调用；提醒异常不影响采集结果。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import collector


def _config(telegram_enabled=True):
    return {
        "scan_interval_minutes": 15,
        "db_path": "data/trends.sqlite3",
        "telegram": {"enabled": telegram_enabled},
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


class TestCollectorAlerts(unittest.TestCase):
    def _run(self, telegram_enabled=True, alert_side_effect=None):
        import tempfile

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
            send = mock.Mock(return_value={"sent": 1, "skipped": 0, "errors": []})
            if alert_side_effect is not None:
                send.side_effect = alert_side_effect
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
                mock.patch("trend_radar.alerts.send_burst_alerts", send),
            ):
                counts = collector.collect_once(_config(telegram_enabled))
            return counts, send

    def test_burst_triggers_alert_and_counts(self):
        counts, send = self._run()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], [_BURST])
        self.assertEqual(counts["bursts"], 1)
        self.assertEqual(counts["alerts_sent"], 1)
        self.assertEqual(counts["alerts_failed"], 0)

    def test_telegram_disabled_skips_alert(self):
        counts, send = self._run(telegram_enabled=False)
        send.assert_not_called()
        self.assertEqual(counts["alerts_sent"], 0)
        self.assertEqual(counts["alerts_failed"], 0)

    def test_alert_exception_does_not_break_round(self):
        counts, send = self._run(alert_side_effect=RuntimeError("boom"))
        self.assertEqual(counts["alerts_sent"], 0)
        self.assertEqual(counts["alerts_failed"], 1)
        self.assertEqual(counts["saved"], 1)


if __name__ == "__main__":
    unittest.main()
