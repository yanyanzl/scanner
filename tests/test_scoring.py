"""评分单元测试：A/B/C 增长示例 + parse_dt。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar.scoring import parse_dt, score_item


class TestScoring(unittest.TestCase):
    def test_abc_growth_example(self):
        """A 1000万播放/过去1小时新增1万；B 30万/新增20万；C 5万/新增4万。
        B 和 C 的增长速度应高于 A（增长优先于总量）。"""
        now = "2026-11-14T12:00:00+00:00"
        a = score_item(
            {"engagement": 10_000_000, "published_at": now}, previous_engagement=9_990_000, interval_hours=1
        )
        b = score_item(
            {"engagement": 300_000, "published_at": now}, previous_engagement=100_000, interval_hours=1
        )
        c = score_item(
            {"engagement": 50_000, "published_at": now}, previous_engagement=10_000, interval_hours=1
        )
        self.assertEqual(a["growth"], 10_000)
        self.assertEqual(b["growth"], 200_000)
        self.assertEqual(c["growth"], 40_000)
        self.assertGreater(b["growth"], a["growth"])
        self.assertGreater(c["growth"], a["growth"])

    def test_parse_dt(self):
        self.assertIsNotNone(parse_dt("2026-11-14T12:00:00Z"))
        self.assertIsNotNone(parse_dt("Fri, 14 Nov 2026 12:00:00 GMT"))
        self.assertIsNotNone(parse_dt(1794154440))
        self.assertIsNone(parse_dt(None))
        self.assertIsNone(parse_dt(""))
        self.assertIsNone(parse_dt("not-a-date"))


if __name__ == "__main__":
    unittest.main()
