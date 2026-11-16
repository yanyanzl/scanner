"""Dashboard 表格行构造测试：trend_radar/dashboard_rows。

行构造器是纯函数（mock dict 进、显示 dict 出），时间列在这里用
timefmt 格式化成本机当地时间；app.py 只负责把行列表交给 Streamlit。
固定时区断言精确输出。
"""
import sys
import unittest
from datetime import timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar.dashboard_rows import (
    burst_row,
    cross_platform_row,
    fastest_item_row,
    hot_item_row,
    top_event_row,
)

TZ8 = timezone(timedelta(hours=8))


def _ev(**kw):
    ev = {
        "event_id": "evt_test",
        "name": "Test Event",
        "name_zh": "",
        "first_seen": "2026-11-15T07:00:00+00:00",
        "rank_score": 64.8,
        "platform_count": 1,
        "platforms": ["youtube"],
        "max_growth": 1544.7,
        "rank_reasons": ["增长最快"],
        "link_url": "https://example.com/a",
        "item_count": 3,
        "total_score": 12.0,
        "max_score": 5.0,
        "trend": "上升",
    }
    ev.update(kw)
    return ev


def _item(**kw):
    it = {
        "title": "Test Video",
        "platform": "youtube",
        "score": 0.45,
        "growth": 704.0,
        "acceleration": 100.0,
        "age_hours": 1.5,
        "url": "https://example.com/v",
        "published_at": "2026-11-15T06:00:00+00:00",
        "first_seen": "2026-11-15T07:00:00+00:00",
    }
    it.update(kw)
    return it


def _burst(**kw):
    b = {
        "event_id": "evt_test",
        "name": "Test Event",
        "reasons": ["growth_spike"],
        "current": {
            "item_count": 3,
            "platform_count": 2,
            "total_score": 12.0,
            "max_score": 5.0,
            "max_growth": 704.0,
            "platforms": "rss,youtube",
        },
        "previous": {
            "item_count": 2,
            "platform_count": 1,
            "total_score": 4.0,
            "max_score": 3.0,
            "max_growth": 100.0,
            "platforms": "rss",
        },
        "detected_at": "2026-11-15T13:18:00+00:00",
    }
    b.update(kw)
    return b


class TestTopEventRow(unittest.TestCase):
    def test_first_seen_formatted(self):
        row = top_event_row(_ev(), 1, tz=TZ8)
        self.assertEqual(row["首次出现"], "11-15 15:00")
        self.assertEqual(row["排名"], 1)
        self.assertEqual(row["事件"], "Test Event")
        self.assertEqual(row["综合分"], 64.8)
        self.assertEqual(row["链接"], "https://example.com/a")

    def test_missing_first_seen_is_empty(self):
        row = top_event_row(_ev(first_seen=None), 1, tz=TZ8)
        self.assertEqual(row["首次出现"], "")


class TestHotItemRow(unittest.TestCase):
    def test_published_at_formatted(self):
        row = hot_item_row(_item(), tz=TZ8)
        self.assertEqual(row["发布时间"], "11-15 14:00")
        self.assertEqual(row["年龄(小时)"], 1.5)  # 相对年龄列保留

    def test_falls_back_to_first_seen(self):
        row = hot_item_row(_item(published_at=None), tz=TZ8)
        self.assertEqual(row["发布时间"], "11-15 15:00")

    def test_both_missing_is_empty(self):
        row = hot_item_row(_item(published_at=None, first_seen=None), tz=TZ8)
        self.assertEqual(row["发布时间"], "")


class TestFastestItemRow(unittest.TestCase):
    def test_published_at_formatted(self):
        row = fastest_item_row(_item(), tz=TZ8)
        self.assertEqual(row["发布时间"], "11-15 14:00")

    def test_falls_back_to_first_seen(self):
        row = fastest_item_row(_item(published_at=""), tz=TZ8)
        self.assertEqual(row["发布时间"], "11-15 15:00")


class TestBurstRow(unittest.TestCase):
    def test_detected_at_formatted(self):
        row = burst_row(_burst(), tz=TZ8)
        self.assertEqual(row["检测时间"], "11-15 21:18")
        self.assertEqual(row["增长/小时"], "100.0 → 704.0")
        self.assertEqual(row["总分"], "4.0 → 12.0")

    def test_no_previous_uses_dash(self):
        row = burst_row(_burst(previous=None), tz=TZ8)
        self.assertEqual(row["增长/小时"], "— → 704.0")
        self.assertEqual(row["检测时间"], "11-15 21:18")

    def test_missing_detected_at_is_empty(self):
        row = burst_row(_burst(detected_at=None), tz=TZ8)
        self.assertEqual(row["检测时间"], "")


class TestCrossPlatformRow(unittest.TestCase):
    def test_first_seen_formatted(self):
        row = cross_platform_row(_ev(), tz=TZ8)
        self.assertEqual(row["首次出现"], "11-15 15:00")
        self.assertEqual(row["事件"], "Test Event")

    def test_missing_rank_score_is_dash(self):
        row = cross_platform_row(_ev(rank_score=None), tz=TZ8)
        self.assertEqual(row["综合分"], "—")
        ev = _ev()
        del ev["rank_score"]
        self.assertEqual(cross_platform_row(ev, tz=TZ8)["综合分"], "—")

    def test_zero_score_stays_zero(self):
        # 0.0 分是真值，不能被当成缺失显示成 "—"。
        row = cross_platform_row(_ev(rank_score=0.0), tz=TZ8)
        self.assertEqual(row["综合分"], 0.0)


if __name__ == "__main__":
    unittest.main()
