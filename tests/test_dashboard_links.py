"""Top 10 事件链接测试：load_top_events 透传 link_url，AppTest 渲染可点链接。

链接选择规则（见 trend_radar/dashboard_data.py）：
  分数最高成员的 URL → 该成员没有 URL 时取事件里最早采集到的成员 URL
  → 都没有时为 ""（面板显示纯文本，不给死链接）。
"""
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar.dashboard_data import _event_link_url, load_top_events

_STREAMLIT_SPEC = importlib.util.find_spec("streamlit")


def _item(platform, title, url, score=0.5):
    return {
        "platform": platform,
        "title": title,
        "url": url,
        "published_at": "2026-11-15T11:00:00+00:00",
        "first_seen": "2026-11-15T11:00:00+00:00",
        "engagement": 0,
        "source": "test",
        "language": "en",
        "summary_zh": "",
    }


def _score(score, growth=0):
    return {"score": score, "growth": growth, "acceleration": 0, "age_hours": 1}


def _seed_db(db_path, rows):
    """rows: [(item_dict, score_result)]；返回打开的连接（调用方负责关）。"""
    conn = dbmod.connect(db_path)
    for item, score in rows:
        dbmod.upsert_item(conn, item, score)
    conn.commit()
    return conn


class TestEventLinkUrlHelper(unittest.TestCase):
    def test_top_member_url(self):
        ev = {
            "items": [
                {"title": "A", "url": "https://example.com/top", "score": 0.9},
                {"title": "B", "url": "https://example.com/other", "score": 0.5},
            ]
        }
        self.assertEqual(_event_link_url(None, ev), "https://example.com/top")

    def test_top_member_no_url_falls_back_to_next_member(self):
        ev = {
            "items": [
                {"title": "A", "url": "", "score": 0.9},
                {"title": "B", "url": "https://example.com/other", "score": 0.5},
            ]
        }
        self.assertEqual(_event_link_url(None, ev), "https://example.com/other")

    def test_no_url_at_all_returns_empty(self):
        ev = {
            "items": [
                {"title": "A", "url": "", "score": 0.9},
                {"title": "B", "url": "  ", "score": 0.5},
            ]
        }
        self.assertEqual(_event_link_url(None, ev), "")
        self.assertEqual(_event_link_url(None, {"items": []}), "")
        self.assertEqual(_event_link_url(None, {}), "")

    def test_earliest_collected_member_url_from_db(self):
        """分数最高成员没有 URL 时，取事件里最早采集到的成员 URL。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            now = datetime.now(timezone.utc)
            conn = _seed_db(
                db_path,
                [
                    (_item("rss", "Seed one", "https://example.com/late"), _score(0.5)),
                    (_item("rss", "Seed two", "https://example.com/early"), _score(0.4)),
                ],
            )
            # upsert 统一写 collected_at=now；这里改成一早一晚，验证取最早的。
            conn.execute(
                "UPDATE items SET collected_at = ? WHERE url = ?",
                ((now - timedelta(hours=1)).isoformat(), "https://example.com/late"),
            )
            conn.execute(
                "UPDATE items SET collected_at = ? WHERE url = ?",
                ((now - timedelta(hours=2)).isoformat(), "https://example.com/early"),
            )
            conn.commit()
            ev = {
                "items": [
                    {"title": "No link", "url": "", "score": 0.9},
                    {"title": "Late", "url": "https://example.com/late", "score": 0.5},
                    {"title": "Early", "url": "https://example.com/early", "score": 0.4},
                ]
            }
            self.assertEqual(_event_link_url(conn, ev), "https://example.com/early")
            conn.close()

    def test_db_fallback_miss_falls_back_to_member_order(self):
        """成员 URL 不在库里时，退回按分数顺序取第一个有 URL 的成员。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            conn = dbmod.connect(db_path)
            conn.close()
            ev = {
                "items": [
                    {"title": "A", "url": "", "score": 0.9},
                    {"title": "B", "url": "https://example.com/b", "score": 0.5},
                ]
            }
            self.assertEqual(_event_link_url(conn, ev), "https://example.com/b")


class TestLoadTopEventsLink(unittest.TestCase):
    def test_top_member_url_carried_through(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            conn = _seed_db(
                db_path,
                [
                    (_item("rss", "Apple unveils new iPhone chip", "https://example.com/a1"), _score(0.6)),
                    (_item("youtube", "Apple unveils new iPhone chip Pro", "https://example.com/a2"), _score(0.8, growth=500)),
                    (_item("rss", "Heat pumps how much heat does your home need", "https://example.com/b1"), _score(0.4)),
                ],
            )
            conn.close()
            top = load_top_events(db_path)
            self.assertEqual(len(top), 2)
            # 跨平台事件排第一，分数最高成员是 youtube 那条（a2）。
            self.assertEqual(top[0]["link_url"], "https://example.com/a2")
            self.assertEqual(top[1]["link_url"], "https://example.com/b1")

    def test_fallback_to_earliest_collected_member(self):
        """分数最高成员没有 URL 时，链接取事件里最早采集到的成员 URL。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            now = datetime.now(timezone.utc)
            conn = _seed_db(
                db_path,
                [
                    (_item("rss", "Mars rover finds water ice", ""), _score(0.9)),
                    (_item("youtube", "Mars rover finds water ice in crater", "https://example.com/early"), _score(0.5)),
                    (_item("rss", "Mars rover finds water ice signs", "https://example.com/late"), _score(0.4)),
                ],
            )
            conn.execute(
                "UPDATE items SET collected_at = ? WHERE url = ?",
                ((now - timedelta(hours=1)).isoformat(), "https://example.com/late"),
            )
            conn.execute(
                "UPDATE items SET collected_at = ? WHERE url = ?",
                ((now - timedelta(hours=2)).isoformat(), "https://example.com/early"),
            )
            conn.commit()
            conn.close()
            top = load_top_events(db_path)
            self.assertEqual(len(top), 1)
            self.assertEqual(top[0]["link_url"], "https://example.com/early")

    def test_no_url_event_gets_empty_link(self):
        """事件成员都没有 URL 时 link_url 为 ""（面板显示纯文本）。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            conn = _seed_db(
                db_path,
                [(_item("rss", "Only item without any link here today", ""), _score(0.5))],
            )
            conn.close()
            top = load_top_events(db_path)
            self.assertEqual(len(top), 1)
            self.assertEqual(top[0]["link_url"], "")


@unittest.skipUnless(_STREAMLIT_SPEC is not None, "streamlit 未安装，跳过 AppTest")
class TestDashboardLinksAppTest(unittest.TestCase):
    def test_top10_renders_link_column(self):
        """AppTest 渲染面板：Top 10 有“链接”列，有 URL 的行是链接，
        没有 URL 的行是空（事件名纯文本）；LinkColumn 配置真实挂在列上。"""
        import pandas as pd
        from streamlit.testing.v1 import AppTest

        import trend_radar.dashboard_data as dd

        top_events = [
            {
                "name": "事件一",
                "rank_score": 90.0,
                "platform_count": 2,
                "platforms": ["rss", "youtube"],
                "max_growth": 10.0,
                "rank_reasons": ["新事件首次跨平台"],
                "link_url": "https://example.com/event1",
            },
            {
                "name": "事件二",
                "rank_score": 50.0,
                "platform_count": 1,
                "platforms": ["rss"],
                "max_growth": 0,
                "rank_reasons": ["最高分 1.00"],
                "link_url": "",
            },
        ]
        items = [
            {
                "title": "标题一",
                "platform": "rss",
                "score": 1.0,
                "growth": 0,
                "acceleration": 0,
                "age_hours": 1,
                "url": "https://example.com/i1",
            }
        ]
        app_path = str(Path(__file__).resolve().parent.parent / "app.py")
        with (
            mock.patch.object(dd, "load_items", return_value=items),
            mock.patch.object(dd, "load_top_events", return_value=top_events),
            mock.patch.object(dd, "load_fastest", return_value=[]),
            mock.patch.object(dd, "load_history", return_value=([], {})),
            mock.patch.object(dd, "load_bursts", return_value=[]),
        ):
            at = AppTest.from_file(app_path)
            at.run()
            self.assertFalse(at.exception, f"AppTest 运行出错：{at.exception}")
            self.assertGreaterEqual(len(at.dataframe), 1)
            df = at.dataframe[0].value
            self.assertIn("链接", df.columns)
            # 现有列顺序不变，链接列追加在最后。
            self.assertEqual(list(df.columns)[-1], "链接")
            self.assertEqual(df["链接"].iloc[0], "https://example.com/event1")
            second = df["链接"].iloc[1]
            self.assertTrue(
                second is None or second == "" or (isinstance(second, float) and pd.isna(second)),
                f"无 URL 的行链接格应为空，实际为 {second!r}",
            )
            # LinkColumn 配置：渲染出的列配置里链接列是 link 类型，显示“查看”。
            link_cfg = _link_column_config(at)
            self.assertIsNotNone(link_cfg, "Top 10 表格没有挂上 LinkColumn 配置")
            type_config = link_cfg.get("type_config") or {}
            self.assertEqual(type_config.get("type"), "link")
            self.assertEqual(type_config.get("display_text"), "查看")


def _link_column_config(at) -> dict | None:
    """从 AppTest 渲染出的 Top 10 表格 proto 里读列配置，返回“链接”列的配置。

    streamlit 1.67 的 Dataframe proto 把列配置存在 `columns` 字段里
    （JSON 字符串），LinkColumn 对应 type_config.type == "link"。
    """
    import json

    proto = getattr(at.dataframe[0], "proto", None)
    if proto is None:
        return None
    try:
        columns = json.loads(proto.columns or "{}")
    except (ValueError, TypeError):
        return None
    cfg = columns.get("链接")
    return cfg if isinstance(cfg, dict) else None


if __name__ == "__main__":
    unittest.main()
