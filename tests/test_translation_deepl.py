"""DeepL 翻译测试：requests.post 全部 mock，绝不调真实 DeepL 接口。

gtx 端点已删除（2026-11-15 深挖结论：修不好），DeepL 现在是 Azure
的备用：没配 Azure key 时 DeepL 是主力，Azure 429/403 或请求失败时
才走 DeepL；同样每小时最多 1 次、只翻优先内容（Top 5 事件名 +
爆发事件名 + 各事件最高分成员标题，见 docs/TRANSLATION.md）。
同时验证：额度用完（456）停掉本轮、403 时标题保持英文、key 后缀
选 host、key 绝不进日志；以及 collect_once 的 DeepL 轮次接线
（每小时最多 1 次、去重提醒端到端）。
"""
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar import translate as translate_mod
from trend_radar.collector import collect_once
from trend_radar.translate import (
    DeepLQuotaExhausted,
    deepl_host,
    deepl_target_lang,
    deepl_translate,
    deepl_translate_many,
)

FREE_KEY = "11111111-2222-3333-4444-555555555555:fx"
PRO_KEY = "11111111-2222-3333-4444-555555555555"


class _FakeResp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


def _deepl_ok(text="你好世界"):
    return _FakeResp(json_data={"translations": [{"detected_source_language": "EN", "text": text}]})


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class TestDeepLHostAndLang(unittest.TestCase):
    def test_fx_key_uses_free_host(self):
        self.assertEqual(deepl_host(FREE_KEY), "api-free.deepl.com")
        self.assertEqual(deepl_host("  " + FREE_KEY + "  "), "api-free.deepl.com")

    def test_non_fx_key_uses_pro_host(self):
        self.assertEqual(deepl_host(PRO_KEY), "api.deepl.com")

    def test_target_lang_mapping(self):
        self.assertEqual(deepl_target_lang("zh-CN"), "ZH")
        self.assertEqual(deepl_target_lang("zh"), "ZH")
        self.assertEqual(deepl_target_lang("en-US"), "EN")
        self.assertEqual(deepl_target_lang(""), "ZH")


class TestDeeplTranslate(unittest.TestCase):
    def test_success_posts_form_with_header_auth(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            post.return_value = _deepl_ok("你好世界")
            result = deepl_translate("Hello world", FREE_KEY)
        self.assertEqual(result, "你好世界")
        url, kwargs = post.call_args.args[0], post.call_args.kwargs
        self.assertEqual(url, "https://api-free.deepl.com/v2/translate")
        self.assertEqual(kwargs["headers"], {"Authorization": f"DeepL-Auth-Key {FREE_KEY}"})
        self.assertEqual(kwargs["data"], {"text": "Hello world", "target_lang": "ZH"})
        # auth_key 不应出现在 URL 或请求体里（2025-11 起已废弃，用它会 403）。
        self.assertNotIn("auth_key", url)
        self.assertNotIn("auth_key", str(kwargs.get("data")))

    def test_pro_key_uses_pro_host(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            post.return_value = _deepl_ok("你好")
            deepl_translate("Hello", PRO_KEY)
        self.assertTrue(post.call_args.args[0].startswith("https://api.deepl.com/"))

    def test_456_raises_quota_exhausted(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            post.return_value = _FakeResp(status_code=456, json_data={"message": "quota"})
            with self.assertRaises(DeepLQuotaExhausted):
                deepl_translate("Hello world", FREE_KEY)

    def test_403_returns_empty_string(self):
        # 403（如 key 发错 host）不抛异常，调用方把标题留到下一轮再试。
        with mock.patch.object(translate_mod.requests, "post") as post:
            post.return_value = _FakeResp(status_code=403, json_data={"message": "forbidden"})
            self.assertEqual(deepl_translate("Hello world", FREE_KEY), "")

    def test_429_returns_empty_string(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            post.return_value = _FakeResp(status_code=429, json_data={"message": "slow down"})
            self.assertEqual(deepl_translate("Hello world", FREE_KEY), "")

    def test_bad_json_returns_empty_string(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            resp = mock.Mock(status_code=200)
            resp.json.side_effect = ValueError("bad json")
            post.return_value = resp
            self.assertEqual(deepl_translate("Hello world", FREE_KEY), "")

    def test_key_never_in_logs(self):
        capture = _LogCapture()
        logger = logging.getLogger("trend_radar.translate")
        logger.addHandler(capture)
        logger.setLevel(logging.WARNING)
        try:
            with mock.patch.object(translate_mod.requests, "post") as post:
                post.return_value = _FakeResp(status_code=403, json_data={})
                deepl_translate("Hello world", FREE_KEY)
                post.side_effect = ConnectionError("boom")
                deepl_translate("Hello again", FREE_KEY)
        finally:
            logger.removeHandler(capture)
        for message in capture.messages:
            self.assertNotIn(FREE_KEY, message)
            self.assertNotIn("11111111-2222", message)

    def test_empty_text_or_key_no_request(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            self.assertEqual(deepl_translate("", FREE_KEY), "")
            self.assertEqual(deepl_translate("Hello", ""), "")
        post.assert_not_called()


class TestDeeplTranslateMany(unittest.TestCase):
    def test_respects_max_texts(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            post.return_value = _deepl_ok("译文")
            results, chars, exhausted = deepl_translate_many(
                ["One", "Two", "Three"], FREE_KEY, max_texts=2, max_chars=1000
            )
        self.assertEqual(post.call_count, 2)
        self.assertEqual(len(results), 2)
        self.assertFalse(exhausted)
        self.assertEqual(chars, len("One") + len("Two"))

    def test_respects_max_chars(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            post.return_value = _deepl_ok("译文")
            results, chars, exhausted = deepl_translate_many(
                ["A" * 100, "B" * 100], FREE_KEY, max_texts=10, max_chars=150
            )
        # 第一条 100 字已占预算，第二条再加就超 150，停掉不调接口。
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(chars, 100)
        self.assertFalse(exhausted)

    def test_456_stops_batch_and_warns_once(self):
        capture = _LogCapture()
        logger = logging.getLogger("trend_radar.translate")
        logger.addHandler(capture)
        logger.setLevel(logging.WARNING)
        try:
            with mock.patch.object(translate_mod.requests, "post") as post:
                post.side_effect = [
                    _deepl_ok("译文一"),
                    _FakeResp(status_code=456, json_data={}),
                    _deepl_ok("译文三"),  # 456 之后不应再调
                ]
                results, chars, exhausted = deepl_translate_many(
                    ["One", "Two", "Three"], FREE_KEY, max_texts=10, max_chars=1000
                )
        finally:
            logger.removeHandler(capture)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(results, {"One": "译文一"})
        self.assertTrue(exhausted)
        quota_warnings = [m for m in capture.messages if "456" in m]
        self.assertEqual(len(quota_warnings), 1)

    def test_failed_text_absent_from_results(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            post.side_effect = [
                _FakeResp(status_code=403, json_data={}),
                _deepl_ok("译文二"),
            ]
            results, _chars, exhausted = deepl_translate_many(
                ["One", "Two"], FREE_KEY, max_texts=10, max_chars=1000
            )
        self.assertEqual(results, {"Two": "译文二"})
        self.assertFalse(exhausted)

    def test_duplicate_text_translated_once(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            post.return_value = _deepl_ok("译文")
            results, _chars, _exhausted = deepl_translate_many(
                ["Same", "Same", "Same"], FREE_KEY, max_texts=10, max_chars=1000
            )
        self.assertEqual(post.call_count, 1)
        self.assertEqual(results, {"Same": "译文"})


def _item(platform, title, url, engagement=0):
    return {
        "platform": platform,
        "title": title,
        "url": url,
        "published_at": "2026-11-15T10:00:00+00:00",
        "first_seen": "2026-11-15T10:00:00+00:00",
        "engagement": engagement,
        "source": "test",
    }


def _config(db_path, deepl_max_per_round=15):
    return {
        "scan_interval_minutes": 15,
        "db_path": str(db_path),
        "sources": {
            "rss": {"enabled": True, "feeds": []},
            "gdelt": {"enabled": False},
            "reddit": {"enabled": False},
            "youtube": {"enabled": False, "queries": ["breaking news"], "interval_minutes": 30},
            "x": {"enabled": False},
            "tiktok": {"enabled": False},
        },
        "burst": {},
        "translation": {
            "enabled": True,
            "target_lang": "zh-CN",
            "azure_max_per_round": 15,
            "deepl_max_per_round": deepl_max_per_round,
        },
    }


def _deepl_post_mock(translation="已翻译"):
    """requests.post 的分派 mock：只允许 DeepL 调用，其他 URL 直接报错。"""

    def _post(url, **kwargs):
        if "deepl.com" not in url:
            raise AssertionError(f"unexpected POST to {url}")
        return _deepl_ok(translation)

    return mock.Mock(side_effect=_post)


class TestDeeplWiring(unittest.TestCase):
    def test_deepl_round_translates_top_events_no_azure_key(self):
        """没配 Azure key 时 DeepL 是主力：Top 5 事件名 + 成员标题都翻。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            # 高互动量标题分数更高；两个不同标题是两个事件，都在 Top 5 里。
            high = _item("rss", "Top story with huge engagement", "https://example.com/a", engagement=10000)
            low = _item("rss", "Quiet story nobody reads", "https://example.com/b", engagement=0)
            env = {"DEEPL_API_KEY": FREE_KEY}
            env = {k: v for k, v in os.environ.items() if k != "AZURE_TRANSLATOR_KEY"}
            env["DEEPL_API_KEY"] = FREE_KEY
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[high, low])),
                mock.patch.object(translate_mod.requests, "post", _deepl_post_mock()),
            ):
                counts = collect_once(config=_config(db_path))
            # 两个事件名（同时也是各自最高分成员标题，去重后 2 条文本）。
            self.assertEqual(counts["deepl_translated"], 2)
            self.assertEqual(counts["azure_translated"], 0)
            self.assertEqual(counts["saved"], 2)
            conn = dbmod.connect(db_path)
            row_a = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/a",)
            ).fetchone()
            row_b = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/b",)
            ).fetchone()
            conn.close()
            self.assertEqual(row_a["title_zh"], "已翻译")
            self.assertEqual(row_b["title_zh"], "已翻译")
            # DeepL 轮次已记时间。
            self.assertTrue((Path(d) / ".deepl_last_run").exists())

    def test_second_round_within_the_hour_skips_deepl(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            first = _item("rss", "First round story", "https://example.com/a")
            second = _item("rss", "Fresh story an hour later", "https://example.com/c")
            env = {k: v for k, v in os.environ.items() if k != "AZURE_TRANSLATOR_KEY"}
            env["DEEPL_API_KEY"] = FREE_KEY
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[first])),
                mock.patch.object(
                    translate_mod.requests, "post", _deepl_post_mock()
                ) as post,
            ):
                collect_once(config=_config(db_path))
                first_calls = post.call_count
                self.assertGreater(first_calls, 0)
                # 第二轮换一条新内容：间隔未满 1 小时，DeepL 不应再调。
                with mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=[second])
                ):
                    counts = collect_once(config=_config(db_path))
            self.assertEqual(post.call_count, first_calls)
            self.assertEqual(counts["deepl_translated"], 0)
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/c",)
            ).fetchone()
            conn.close()
            self.assertIsNone(row["title_zh"])

    def test_no_key_no_translation_call(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("DEEPL_API_KEY", "AZURE_TRANSLATOR_KEY")
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                mock.patch.object(translate_mod.requests, "post") as post,
            ):
                counts = collect_once(config=_config(db_path))
            post.assert_not_called()
            self.assertEqual(counts["deepl_translated"], 0)
            self.assertEqual(counts["azure_translated"], 0)
            self.assertFalse((Path(d) / ".deepl_last_run").exists())
            self.assertFalse((Path(d) / ".azure_last_run").exists())

    def test_456_keeps_round_alive_and_titles_english(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            capture = _LogCapture()
            logger = logging.getLogger("trend_radar.translate")
            logger.addHandler(capture)
            logger.setLevel(logging.WARNING)
            try:
                env = {k: v for k, v in os.environ.items() if k != "AZURE_TRANSLATOR_KEY"}
                env["DEEPL_API_KEY"] = FREE_KEY
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch(
                        "trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])
                    ),
                    mock.patch.object(translate_mod.requests, "post") as post,
                ):
                    post.return_value = _FakeResp(status_code=456, json_data={})
                    counts = collect_once(config=_config(db_path))
            finally:
                logger.removeHandler(capture)
            # 轮次照常完成、内容照常入库，只是没译出来。
            self.assertEqual(counts["saved"], 1)
            self.assertEqual(counts["deepl_translated"], 0)
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/a",)
            ).fetchone()
            conn.close()
            self.assertIsNone(row["title_zh"])
            # 额度用完的 warning 只记一次，且尝试仍被记时间（下轮不再撞）。
            self.assertEqual(len([m for m in capture.messages if "456" in m]), 1)
            self.assertTrue((Path(d) / ".deepl_last_run").exists())

    def test_deepl_403_title_stays_english_no_fallback(self):
        """DeepL 403 时该标题保持英文——gtx 已删除，没有兜底可回退。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            env = {k: v for k, v in os.environ.items() if k != "AZURE_TRANSLATOR_KEY"}
            env["DEEPL_API_KEY"] = FREE_KEY
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                mock.patch.object(translate_mod.requests, "post") as post,
            ):
                post.return_value = _FakeResp(status_code=403, json_data={})
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["saved"], 1)
            self.assertEqual(counts["deepl_translated"], 0)
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/a",)
            ).fetchone()
            conn.close()
            self.assertIsNone(row["title_zh"])


class TestDedupEndToEnd(unittest.TestCase):
    def test_three_same_name_bursts_one_telegram_post(self):
        """3 个同名爆发走 collect_once：Telegram 只收到 1 条。"""
        from trend_radar import collector

        name = "Today Breaking News LIVE | Top Headlines Of The Hour | 15.11.2026"
        bursts = [
            {
                "event_id": f"evt_{i}",
                "name": name,
                "name_zh": None,
                "reasons": ["new_multi_platform"],
                "current": {
                    "item_count": 3,
                    "platform_count": 2,
                    "total_score": 12.0,
                    "max_score": 5.0,
                    "max_growth": 0.0,
                    "platforms": "rss,youtube",
                },
                "previous": None,
                "detected_at": "2026-11-15T18:00:00+00:00",
            }
            for i in (1, 2, 3)
        ]
        item = _item("rss", "Test Event", "https://example.com/a")
        cfg = {
            "scan_interval_minutes": 15,
            "db_path": "data/trends.sqlite3",
            "telegram": {"enabled": True},
            "sources": {
                "rss": {"enabled": True, "feeds": []},
                "gdelt": {"enabled": False},
                "reddit": {"enabled": False},
                "youtube": {"enabled": False},
                "x": {"enabled": False},
                "tiktok": {"enabled": False},
            },
        }
        telegram_resp = mock.Mock(status_code=200)
        telegram_resp.json.return_value = {"ok": True}

        def _post(url, **kwargs):
            if "api.telegram.org" not in url:
                raise AssertionError(f"unexpected POST to {url}")
            return telegram_resp

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            env = {
                "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
                "TELEGRAM_CHAT_ID": "987654321",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(collector, "BASE_DIR", tmp),
                mock.patch.object(collector.rss, "fetch", mock.Mock(return_value=[item])),
                mock.patch(
                    "trend_radar.clustering.cluster_from_db",
                    mock.Mock(return_value=[{"event_id": "evt_1"}]),
                ),
                mock.patch("trend_radar.clustering.persist_events", mock.Mock(return_value=None)),
                mock.patch("trend_radar.burst.detect_bursts", mock.Mock(return_value=bursts)),
                mock.patch("trend_radar.alerts.requests.post", mock.Mock(side_effect=_post)) as post,
            ):
                counts = collector.collect_once(cfg)
        telegram_calls = [
            c for c in post.call_args_list if "api.telegram.org" in c.args[0]
        ]
        self.assertEqual(len(telegram_calls), 1)
        self.assertEqual(counts["alerts_sent"], 1)
        self.assertEqual(counts["bursts"], 3)


if __name__ == "__main__":
    unittest.main()
