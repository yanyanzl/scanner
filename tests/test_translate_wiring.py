"""翻译接线测试：collect_once 入库后翻译标题/事件名并写回数据库。

翻译接口本身用 mock（绝不调真实接口）；这里只验证接线：
入库后翻译、中文标题不调接口、每轮预算、翻译异常不中断采集、
事件名中文进 events.name_zh。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar import translate as translate_mod
from trend_radar.collector import collect_once


def _item(platform, title, url):
    return {
        "platform": platform,
        "title": title,
        "url": url,
        "published_at": "2026-11-15T10:00:00+00:00",
        "first_seen": "2026-11-15T10:00:00+00:00",
        "engagement": 0,
        "source": "test",
    }


def _config(db_path, max_per_round=30, translation=True, youtube=False):
    cfg = {
        "scan_interval_minutes": 15,
        "db_path": str(db_path),
        "sources": {
            "rss": {"enabled": True, "feeds": []},
            "gdelt": {"enabled": False},
            "reddit": {"enabled": False},
            "youtube": {
                "enabled": youtube,
                "queries": ["breaking news"],
                "interval_minutes": 30,
            },
            "x": {"enabled": False},
            "tiktok": {"enabled": False},
        },
        "burst": {},
    }
    if translation:
        cfg["translation"] = {
            "enabled": True,
            "target_lang": "zh-CN",
            "max_per_round": max_per_round,
            "delay_seconds": 0,  # 测试里不真实等待
        }
    return cfg


class TestTranslateWiring(unittest.TestCase):
    def test_new_non_chinese_item_gets_title_zh(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            with (
                mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])
                ),
                mock.patch.object(
                    translate_mod,
                    "translate_text",
                    mock.Mock(return_value="苹果发布新款 iPhone 芯片"),
                ),
            ):
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["saved"], 1)
            self.assertEqual(counts["translated"], 1)
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title, title_zh FROM items WHERE url = ?",
                ("https://example.com/a",),
            ).fetchone()
            conn.close()
            self.assertEqual(row["title"], "Apple unveils new iPhone chip")
            self.assertEqual(row["title_zh"], "苹果发布新款 iPhone 芯片")

    def test_chinese_title_stored_without_any_http(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "苹果发布新款芯片", "https://example.com/a")
            with (
                mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])
                ),
                mock.patch.object(translate_mod, "translate_text") as fake_translate,
                mock.patch.object(translate_mod.requests, "get") as fake_get,
            ):
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["saved"], 1)
            self.assertEqual(counts["translated"], 0)
            fake_translate.assert_not_called()
            fake_get.assert_not_called()
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/a",)
            ).fetchone()
            conn.close()
            self.assertEqual(row["title_zh"], "苹果发布新款芯片")

    def test_max_per_round_respected(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            items = [
                _item("rss", "Breaking news one", "https://example.com/1"),
                _item("rss", "Breaking news two", "https://example.com/2"),
                _item("rss", "Breaking news three", "https://example.com/3"),
            ]
            with (
                mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=items)
                ),
                mock.patch.object(
                    translate_mod, "translate_text", mock.Mock(return_value="译文")
                ),
            ):
                counts = collect_once(config=_config(db_path, max_per_round=2))
            self.assertEqual(counts["saved"], 3)
            # counts["translated"] 只记标题翻译一步的接口调用（事件名翻译
            # 另有自己的 10 次预算，不算在这里）。
            self.assertEqual(counts["translated"], 2)
            conn = dbmod.connect(db_path)
            filled = conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE title_zh IS NOT NULL AND title_zh != ''"
            ).fetchone()["c"]
            unfilled = conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE title_zh IS NULL OR title_zh = ''"
            ).fetchone()["c"]
            conn.close()
            self.assertEqual(filled, 2)
            self.assertEqual(unfilled, 1)

    def test_failed_translation_not_stored_and_retried_next_round(self):
        """接口失败回退返回原标题时不存 title_zh，下一轮会再试；
        否则一次 429 会让该标题永久失去翻译机会。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            fake_translate = mock.Mock(
                side_effect=[
                    "Apple unveils new iPhone chip",  # 第 1 轮标题：失败回退，原样返回
                    "苹果发布新款 iPhone 芯片",  # 第 1 轮事件名：成功
                    "苹果发布新款 iPhone 芯片",  # 第 2 轮标题：成功
                ]
            )
            with (
                mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])
                ),
                mock.patch.object(translate_mod, "translate_text", fake_translate),
            ):
                first = collect_once(config=_config(db_path))
                conn = dbmod.connect(db_path)
                row = conn.execute(
                    "SELECT title_zh FROM items WHERE url = ?",
                    ("https://example.com/a",),
                ).fetchone()
                conn.close()
                self.assertEqual(first["translated"], 1)
                self.assertIsNone(row["title_zh"])
                second = collect_once(config=_config(db_path))
            self.assertEqual(second["translated"], 1)
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/a",)
            ).fetchone()
            conn.close()
            self.assertEqual(row["title_zh"], "苹果发布新款 iPhone 芯片")

    def test_delay_between_api_calls(self):
        """delay_seconds > 0 时每次接口调用后等待一次。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            items = [
                _item("rss", "Breaking news one", "https://example.com/1"),
                _item("rss", "Breaking news two", "https://example.com/2"),
            ]
            cfg = _config(db_path)
            cfg["translation"]["delay_seconds"] = 0.5
            with (
                mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=items)
                ),
                mock.patch.object(
                    translate_mod, "translate_text", mock.Mock(return_value="译文")
                ),
                mock.patch("time.sleep") as fake_sleep,
            ):
                counts = collect_once(config=cfg)
            self.assertEqual(counts["translated"], 2)
            fake_sleep.assert_has_calls([mock.call(0.5), mock.call(0.5)])

    def test_translation_exception_does_not_break_round(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            with (
                mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])
                ),
                mock.patch.object(
                    translate_mod,
                    "translate_text",
                    mock.Mock(side_effect=RuntimeError("boom")),
                ),
            ):
                counts = collect_once(config=_config(db_path))
            # 翻译炸了，本轮照常返回、内容照常入库，只是没译出来。
            self.assertEqual(counts["saved"], 1)
            self.assertEqual(counts["translated"], 0)
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/a",)
            ).fetchone()
            conn.close()
            self.assertIsNone(row["title_zh"])

    def test_translation_disabled_by_default_without_section(self):
        """配置里没有 translation 一节时默认不翻译（旧的内联配置不突然联网）。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            with (
                mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])
                ),
                mock.patch.object(translate_mod, "translate_text") as fake_translate,
                mock.patch.object(translate_mod.requests, "get") as fake_get,
            ):
                counts = collect_once(config=_config(db_path, translation=False))
            self.assertEqual(counts["saved"], 1)
            fake_translate.assert_not_called()
            fake_get.assert_not_called()

    def test_event_name_translated(self):
        """同一标题出现在 rss + youtube → 一个事件，name_zh 写进 events 表，
        爆发检测返回的 burst 里也带 name_zh。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            title = "Breaking news test event"
            rss_item = _item("rss", title, "https://example.com/a")
            yt_item = _item("youtube", title, "https://example.com/b")
            with (
                mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=[rss_item])
                ),
                mock.patch(
                    "trend_radar.collector.youtube.fetch",
                    mock.Mock(return_value=[yt_item]),
                ),
                mock.patch.object(
                    translate_mod, "translate_text", mock.Mock(return_value="突发新闻测试事件")
                ),
            ):
                counts = collect_once(config=_config(db_path, youtube=True))
            self.assertEqual(counts["events"], 1)
            self.assertEqual(counts["bursts"], 1)
            conn = dbmod.connect(db_path)
            row = conn.execute("SELECT name, name_zh FROM events").fetchone()
            conn.close()
            self.assertEqual(row["name"], title)
            self.assertEqual(row["name_zh"], "突发新闻测试事件")


if __name__ == "__main__":
    unittest.main()
