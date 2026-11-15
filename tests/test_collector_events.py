"""接线测试：collect_once 每轮结束时聚类并持久化事件快照。

不跑任何平台 adapter（全部 enabled=False），只验证：已入库的内容
在 collect_once 后出现在 events / event_snapshots 表里，第二轮
同一事件 id 不变、快照追加一行。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar.collector import collect_once


def _config(db_path: Path) -> dict:
    return {
        "scan_interval_minutes": 15,
        "db_path": str(db_path),
        "sources": {
            "rss": {"enabled": False},
            "gdelt": {"enabled": False},
            "reddit": {"enabled": False},
            "youtube": {"enabled": False},
            "x": {"enabled": False},
            "tiktok": {"enabled": False},
        },
        "burst": {},
    }


class TestCollectorPersistsEvents(unittest.TestCase):
    def test_events_persisted_and_accumulate_across_rounds(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            conn = dbmod.connect(db_path)
            dbmod.upsert_item(
                conn,
                {
                    "platform": "rss",
                    "title": "Apple unveils new iPhone chip",
                    "url": "https://example.com/apple-chip",
                    "published_at": "2026-11-15T06:00:00+00:00",
                    "first_seen": "2026-11-15T07:00:00+00:00",
                    "engagement": 0,
                    "source": "test",
                    "language": "en",
                    "summary_zh": "",
                },
                {"score": 5.0, "growth": 0, "acceleration": 0, "age_hours": 1},
            )
            conn.close()

            counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["saved"], 0)  # 没有 adapter 跑
            self.assertEqual(counts["events"], 1)
            self.assertEqual(counts["bursts"], 0)

            conn = dbmod.connect(db_path)
            events = conn.execute("SELECT event_id FROM events").fetchall()
            self.assertEqual(len(events), 1)
            event_id = events[0]["event_id"]
            snaps = conn.execute(
                "SELECT COUNT(*) AS c FROM event_snapshots"
            ).fetchone()["c"]
            self.assertEqual(snaps, 1)
            conn.close()

            # 第二轮：同一事件、同一 id，快照追加一行，仍无爆发。
            counts2 = collect_once(config=_config(db_path))
            self.assertEqual(counts2["events"], 1)
            self.assertEqual(counts2["bursts"], 0)
            conn = dbmod.connect(db_path)
            events2 = conn.execute("SELECT event_id FROM events").fetchall()
            self.assertEqual([e["event_id"] for e in events2], [event_id])
            snaps2 = conn.execute(
                "SELECT COUNT(*) AS c FROM event_snapshots"
            ).fetchone()["c"]
            self.assertEqual(snaps2, 2)
            conn.close()


if __name__ == "__main__":
    unittest.main()
