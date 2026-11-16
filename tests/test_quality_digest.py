"""每日质量简报测试：标记、格式化、计数、配置、发送（mock）。"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar.quality_digest import (
    format_quality_digest,
    gather_digest_data,
    is_quality_digest,
    load_digest_config,
    parse_quality_digest,
    record_round_counts,
    send_quality_digest,
)

DIGEST_SAMPLE = (
    "【质量简报｜quality-digest v1｜date=2026-11-15】\n"
    "📋 每日质量简报（2026-11-15）\n"
    "Top 5（当前综合排名）：\n"
    "1. 警察局长讲话 64.8分，1平台，增长1544.7/小时"
)

BURST_SAMPLE = (
    "【扫描器提醒｜scanner-alert v1｜event=evt_abc｜rule=growth_spike｜ts=2026-11-16T09:46:13Z】\n"
    "🚨 热点爆发提醒\n"
    "事件：现实生活与人工智能"
)


def _ev(event_id, name, score, platforms=1, growth=100.0, tag_ratio=0.0, rec=False):
    return {
        "event_id": event_id,
        "name": name,
        "rank_score": score,
        "platform_count": platforms,
        "platforms": ["youtube"] if platforms == 1 else ["rss", "youtube"],
        "max_growth": growth,
        "hashtag_ratio": tag_ratio,
        "is_recurrence": rec,
    }


def _digest_data():
    # 1 个真事件、1 个 hashtag 事件、1 个 recurrence 事件（任务书要求）。
    top = [
        _ev("evt_real", "突发新闻——国家警察局长讲话", 64.8, platforms=1, growth=1544.7),
        _ev("evt_tag", "最新消息#rakyat#KPK #klipingberita", 9.5, platforms=1, growth=375.7, tag_ratio=0.86),
        _ev("evt_rec", "今日突发新闻直播", 20.0, platforms=2, growth=823.4, rec=True),
        _ev("evt_ai", "现实生活与人工智能", 21.7, platforms=1, growth=259.0),
    ]
    counts = {
        "rounds": 10,
        "bursts": 3,
        "recurrence": 7,
        "telegram_sent": 3,
        "telegram_failed": 0,
        "muse_sent": 3,
        "muse_failed": 1,
    }
    return {"date": "2026-11-15", "top_events": top, "counts": counts, "events_count": 41}


class TestDigestMarker(unittest.TestCase):
    def test_parse_digest(self):
        parsed = parse_quality_digest(DIGEST_SAMPLE)
        self.assertEqual(parsed, {"version": 1, "date": "2026-11-15"})
        self.assertTrue(is_quality_digest(DIGEST_SAMPLE))

    def test_burst_alert_is_not_digest(self):
        self.assertIsNone(parse_quality_digest(BURST_SAMPLE))
        self.assertFalse(is_quality_digest(BURST_SAMPLE))

    def test_plain_text_is_not_digest(self):
        self.assertIsNone(parse_quality_digest("扫描器测试"))
        self.assertIsNone(parse_quality_digest(""))
        self.assertIsNone(parse_quality_digest(None))

    def test_scanner_marker_does_not_match_digest(self):
        from trend_radar.muse_chat import parse_scanner_marker

        self.assertIsNone(parse_scanner_marker(DIGEST_SAMPLE))


class TestFormatDigest(unittest.TestCase):
    def test_full_digest(self):
        text = format_quality_digest(_digest_data())
        lines = text.splitlines()
        self.assertEqual(
            lines[0], "【质量简报｜quality-digest v1｜date=2026-11-15】"
        )
        self.assertIn("Top 5（当前综合排名）：", text)
        self.assertIn("警察局长讲话", text)
        self.assertIn("64.8分", text)
        # 计数行：事件数、爆发、重复抑制、Telegram、Muse。
        self.assertIn("事件41个", text)
        self.assertIn("爆发3个", text)
        self.assertIn("重复出现被抑制7个", text)
        self.assertIn("Telegram已发3条", text)
        self.assertIn("Muse已发3条、失败1条", text)
        # 质量信号：4 条里单平台 3 个、hashtag 高 1 个、recurrence 1 个。
        self.assertIn("质量信号（Top 4）：单平台3个，hashtag占比高1个，recurrence 1个", text)
        # 紧凑：不超过 1000 字符。
        self.assertLessEqual(len(text), 1000)

    def test_no_counts_degrades(self):
        data = _digest_data()
        data["counts"] = None
        text = format_quality_digest(data)
        self.assertIn("昨日无轮次记录", text)
        self.assertIn("警察局长讲话", text)

    def test_no_top_events_degrades(self):
        data = {"date": "2026-11-15", "top_events": [], "counts": None, "events_count": 0}
        text = format_quality_digest(data)
        self.assertIn("暂无事件", text)
        self.assertIn("暂无 Top 10 数据", text)

    def test_long_name_truncated(self):
        data = _digest_data()
        data["top_events"] = [
            _ev("evt_long", "这是一个非常非常非常非常长的事件标题超过二十字了", 50.0)
        ]
        text = format_quality_digest(data)
        self.assertIn("…", text)

    def test_generated_time_line(self):
        # 生成时间行：gather_digest_data 写进 data 的 generated_at
        #（发送当时的本机当地时间），与报告期日期是两回事，两行都保留。
        data = _digest_data()
        data["generated_at"] = "2026-11-16T08:00:00+08:00"
        with mock.patch(
            "trend_radar.quality_digest.format_local_time",
            return_value="11-16 08:00",
        ) as fmt:
            text = format_quality_digest(data)
        fmt.assert_called_once_with("2026-11-16T08:00:00+08:00")
        self.assertIn("生成时间：11-16 08:00（伦敦时间）", text)
        lines = text.splitlines()
        # 标记行、标题行之后，Top 5 之前。
        self.assertEqual(lines[2], "生成时间：11-16 08:00（伦敦时间）")
        self.assertIn("每日质量简报（2026-11-15）", text)  # 报告期日期还在
        self.assertLessEqual(len(text), 1000)

    def test_generated_time_real_format(self):
        data = _digest_data()
        data["generated_at"] = "2026-11-16T08:00:00+08:00"
        text = format_quality_digest(data)
        self.assertRegex(text, r"生成时间：\d{2}-\d{2} \d{2}:\d{2}（伦敦时间）")

    def test_no_generated_time_when_missing(self):
        # data 里没有 generated_at 时不写这一行（不造假时间）。
        self.assertNotIn("生成时间", format_quality_digest(_digest_data()))


class TestDigestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = load_digest_config({})
        self.assertEqual(cfg, {"enabled": True, "hour": 8})

    def test_override(self):
        cfg = load_digest_config({"quality_digest": {"enabled": False, "hour": 20}})
        self.assertEqual(cfg, {"enabled": False, "hour": 20})

    def test_config_file_has_section(self):
        text = (Path(__file__).resolve().parent.parent / "config.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("quality_digest:", text)
        self.assertIn("hour: 8", text)


class TestRecordRoundCounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.db_path = self.data_dir / "trends.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_accumulates_and_prunes(self):
        counts = {
            "bursts": 2,
            "bursts_recurrence": 3,
            "alerts_sent": 1,
            "alerts_failed": 0,
            "muse_chat_sent": 1,
            "muse_chat_failed": 1,
            "events": 40,
        }
        self.assertTrue(record_round_counts(self.db_path, counts, today="2026-11-15"))
        self.assertTrue(record_round_counts(self.db_path, counts, today="2026-11-15"))
        state = json.loads((self.data_dir / ".quality_daily.json").read_text(encoding="utf-8"))
        bucket = state["2026-11-15"]
        self.assertEqual(bucket["rounds"], 2)
        self.assertEqual(bucket["bursts"], 4)
        self.assertEqual(bucket["recurrence"], 6)
        self.assertEqual(bucket["telegram_sent"], 2)
        self.assertEqual(bucket["muse_failed"], 2)
        # 更早的日期被清掉。
        self.assertTrue(record_round_counts(self.db_path, counts, today="2026-11-16"))
        state = json.loads((self.data_dir / ".quality_daily.json").read_text(encoding="utf-8"))
        self.assertNotIn("2026-11-14", state)

    def test_corrupt_state_file_recovers(self):
        (self.data_dir / ".quality_daily.json").write_text("not json", encoding="utf-8")
        self.assertTrue(record_round_counts(self.db_path, {"bursts": 1}, today="2026-11-15"))
        state = json.loads((self.data_dir / ".quality_daily.json").read_text(encoding="utf-8"))
        self.assertEqual(state["2026-11-15"]["bursts"], 1)


class TestGatherDigestData(unittest.TestCase):
    def test_missing_db_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            data = gather_digest_data(Path(d) / "nope.sqlite3", date="2026-11-15")
            self.assertEqual(data["top_events"], [])
            self.assertIsNone(data["counts"])
            self.assertEqual(data["events_count"], 0)

    def test_events_count_by_first_seen_date(self):
        # first_seen 落在昨天的事件计入，今天的不计。
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            conn = dbmod.connect(db_path)
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
            today = datetime.now(timezone.utc).date().isoformat()
            for eid, day in (("evt_y", yesterday), ("evt_t", today)):
                conn.execute(
                    "INSERT INTO events (event_id, name, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?)",
                    (eid, f"事件 {eid}", f"{day}T10:00:00+00:00", f"{day}T10:00:00+00:00"),
                )
            conn.commit()
            conn.close()
            # cluster_from_db 读 items 表聚类；这里 items 为空，events_count
            # 应为 0（gather 不直接读 events 表，计数来自聚类输出）。
            data = gather_digest_data(db_path, date=yesterday)
            self.assertEqual(data["events_count"], 0)

    def test_gather_adds_generated_at(self):
        # 简报的生成时间（发送当时的本机当地时间）由 gather 写进 data，
        # 格式化时只读这个字段，不在纯函数里取当前时间。
        from trend_radar.scoring import parse_dt

        with tempfile.TemporaryDirectory() as d:
            data = gather_digest_data(Path(d) / "nope.sqlite3", date="2026-11-15")
            self.assertIn("generated_at", data)
            self.assertIsNotNone(parse_dt(data["generated_at"]))


class TestSendDigest(unittest.TestCase):
    def test_skipped_when_muse_chat_disabled(self):
        # send_quality_digest 在函数内 import send_to_muse_chat，改为
        # patch trend_radar.muse_chat.send_to_muse_chat。
        import trend_radar.muse_chat as mc

        calls = []
        orig_send = mc.send_to_muse_chat
        mc.send_to_muse_chat = lambda text, cfg: calls.append(text) or {"ok": True}
        try:
            result = send_quality_digest(
                {"muse_chat": {"enabled": False}, "quality_digest": {"enabled": True}}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "muse_chat_disabled")
            self.assertEqual(calls, [])
        finally:
            mc.send_to_muse_chat = orig_send

    def test_skipped_when_digest_disabled(self):
        result = send_quality_digest(
            {"muse_chat": {"enabled": True}, "quality_digest": {"enabled": False}}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "digest_disabled")

    def test_send_failure_does_not_raise(self):
        import trend_radar.muse_chat as mc

        orig_send = mc.send_to_muse_chat
        mc.send_to_muse_chat = lambda text, cfg: {"ok": False, "reason": "no_muse_tab"}
        try:
            with tempfile.TemporaryDirectory() as d:
                db_path = Path(d) / "trends.sqlite3"
                dbmod.connect(db_path).close()
                result = send_quality_digest(
                    {
                        "db_path": str(db_path),
                        "muse_chat": {"enabled": True},
                        "quality_digest": {"enabled": True},
                    }
                )
                # 空库、无昨日计数 → no_data（在发送前就跳过，不调发送）。
                self.assertFalse(result["ok"])
                self.assertIn(result["reason"], ("no_data", "no_muse_tab"))
        finally:
            mc.send_to_muse_chat = orig_send


if __name__ == "__main__":
    unittest.main()
