"""时间显示 helper 测试：format_local_time。

只测这一个函数：库里的 ISO 时间串 → 本机当地时间 "MM-DD HH:MM"。
固定时区断言精确输出；默认（不传 tz）只断言形状，不依赖测试机
的系统时区。
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from trend_radar.timefmt import format_local_time

TZ8 = timezone(timedelta(hours=8))
TZ_LONDON_WINTER = timezone(timedelta(hours=0))


class TestFormatLocalTime(unittest.TestCase):
    def test_zulu_to_fixed_tz(self):
        self.assertEqual(
            format_local_time("2026-11-15T13:18:00Z", tz=TZ8), "11-15 21:18"
        )

    def test_offset_input(self):
        self.assertEqual(
            format_local_time("2026-11-15T13:18:00+00:00", tz=TZ8), "11-15 21:18"
        )

    def test_offset_conversion_across_midnight(self):
        # 11-16 01:18 +02:00 = 11-15 23:18 UTC = 11-16 07:18 +08:00。
        self.assertEqual(
            format_local_time("2026-11-16T01:18:00+02:00", tz=TZ8), "11-16 07:18"
        )

    def test_naive_treated_as_utc(self):
        # 无时区的时间串视为 UTC（库里老数据常见），再转目标时区。
        self.assertEqual(
            format_local_time("2026-11-15T13:18:00", tz=TZ8), "11-15 21:18"
        )

    def test_datetime_input(self):
        self.assertEqual(
            format_local_time(
                datetime(2026, 11, 15, 13, 18, tzinfo=timezone.utc), tz=TZ8
            ),
            "11-15 21:18",
        )

    def test_numeric_timestamp(self):
        ts = datetime(2026, 11, 15, 13, 18, tzinfo=timezone.utc).timestamp()
        self.assertEqual(format_local_time(ts, tz=TZ8), "11-15 21:18")

    def test_rfc822_input(self):
        self.assertEqual(
            format_local_time("15 Nov 2026 13:18:00 +0000", tz=TZ8), "11-15 21:18"
        )

    def test_london_winter_is_utc(self):
        # 11 月伦敦是 GMT（UTC+0），与 UTC 同值；验证时区参数真的生效。
        self.assertEqual(
            format_local_time("2026-11-15T13:18:00Z", tz=TZ_LONDON_WINTER),
            "11-15 13:18",
        )

    def test_empty_none_and_garbage_return_empty(self):
        for bad in ("", "   ", None, "not a date", "2026-13-45T99:99:99Z", ["x"]):
            with self.subTest(bad=bad):
                self.assertEqual(format_local_time(bad, tz=TZ8), "")

    def test_default_tz_is_local_shape_only(self):
        # 不传 tz 用系统本地时区：只断言形状，不依赖测试机时区。
        self.assertRegex(
            format_local_time("2026-11-15T13:18:00Z"), r"^\d{2}-\d{2} \d{2}:\d{2}$"
        )


if __name__ == "__main__":
    unittest.main()
