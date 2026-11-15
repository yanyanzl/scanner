"""Azure AI Translator 测试：requests.post 全部 mock，绝不调真实 Azure 接口。

覆盖：批量请求的形式（URL / api-version / to / 不传 from / key 与
region 请求头 / JSON 体 / 响应解析）、429/403 回退到 DeepL、没 key
时只走 DeepL、Azure 每轮都跑（15 分钟一轮就翻一次，没有每小时门控）、
每轮 600 字符硬上限（事件名优先、成员标题先被砍）、月度用量封顶
（data/.azure_usage.json 计数、跨月归零、损坏文件按 0 处理、失败的
请求不计用量、月度用完跳过 Azure 改走 DeepL）、key 绝不进日志；
以及 collect_once 的 Azure 轮次接线（只翻 Top 5 事件名 + 爆发事件名
+ 各事件最高分成员标题；翻译在爆发检测之后、发 Telegram 提醒之前，
所以提醒当轮就能用中文事件名）。DeepL 仍是每小时最多 1 次（见
test_translation_deepl.py 的 test_second_round_within_the_hour_skips_deepl）。
"""
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar import translate as translate_mod
from trend_radar.collector import collect_once

AZURE_KEY = "azure-test-key-123"
AZURE_REGION = "westeurope"
DEEPL_KEY = "deepl-test-key:fx"


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _azure_ok(texts):
    """把每个输入文本译成 'ZH:' + 原文，顺序一一对应。"""
    return _FakeResp(
        status_code=200,
        json_data=[
            {"translations": [{"text": f"ZH:{t}", "to": "zh-Hans"}]} for t in texts
        ],
    )


def _deepl_ok(translation="已翻译"):
    return _FakeResp(
        status_code=200,
        json_data={"translations": [{"text": translation, "detected_source_language": "EN"}]},
    )


def _item(platform, title, url, engagement=0, published_at=None):
    # 聚类按 platform 字段分组事件（见 trend_radar/clustering.py），
    # 只给 source 不给 platform 的内容聚不出事件。
    return {
        "platform": platform,
        "source": platform,
        "title": title,
        "url": url,
        "published_at": published_at or "2026-11-15T12:00:00+00:00",
        "engagement": engagement,
    }


def _config(db_path, azure_max_per_round=15, deepl_max_per_round=15,
              azure_max_chars_per_round=600, azure_monthly_cap_chars=1800000):
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
            "azure_max_per_round": azure_max_per_round,
            "deepl_max_per_round": deepl_max_per_round,
            "azure_max_chars_per_round": azure_max_chars_per_round,
            "azure_monthly_cap_chars": azure_monthly_cap_chars,
        },
    }


def _current_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _prev_month():
    first = datetime.now(timezone.utc).replace(day=1)
    from datetime import timedelta

    return (first - timedelta(days=1)).strftime("%Y-%m")


def _read_usage(d):
    return json.loads((Path(d) / ".azure_usage.json").read_text(encoding="utf-8"))


def _write_usage(d, month, chars):
    (Path(d) / ".azure_usage.json").write_text(
        json.dumps({"month": month, "chars": chars}), encoding="utf-8"
    )


def _env_with(azure=True, deepl=True):
    env = {k: v for k, v in os.environ.items() if k not in ("AZURE_TRANSLATOR_KEY", "AZURE_TRANSLATOR_REGION", "DEEPL_API_KEY")}
    if azure:
        env["AZURE_TRANSLATOR_KEY"] = AZURE_KEY
    if deepl:
        env["DEEPL_API_KEY"] = DEEPL_KEY
    return env


def _post_dispatcher(azure_resp=None, deepl_resp=None, telegram_resp=None):
    """按 URL 分派 requests.post：Azure / DeepL / Telegram 各走各的 mock。

    注意：alerts 与 translate 用的是同一个 requests 模块对象，所以
    Telegram 也必须走这一个分派器——再 patch 一次 requests.post 会把
    前一个 mock 顶掉，翻译请求会撞进 Telegram mock。
    """
    calls = {"azure": [], "deepl": [], "telegram": []}

    def _post(url, **kwargs):
        if "cognitive.microsofttranslator.com" in url:
            calls["azure"].append((url, kwargs))
            resp = azure_resp
            if callable(resp):
                return resp(url, kwargs)
            return resp
        if "deepl.com" in url:
            calls["deepl"].append((url, kwargs))
            resp = deepl_resp
            if callable(resp):
                return resp(url, kwargs)
            return resp
        if "api.telegram.org" in url:
            calls["telegram"].append((url, kwargs))
            resp = telegram_resp
            if callable(resp):
                return resp(url, kwargs)
            return resp
        raise AssertionError(f"unexpected POST to {url}")

    post = mock.Mock(side_effect=_post)
    post.calls = calls
    return post


class TestAzureTranslateMany(unittest.TestCase):
    def test_success_batched_single_request(self):
        post = mock.Mock(side_effect=lambda url, **kw: _azure_ok([b["Text"] for b in kw["json"]]))
        with mock.patch.object(translate_mod.requests, "post", post):
            results, chars, exhausted = translate_mod.azure_translate_many(
                ["Hello world", "Breaking news"], AZURE_KEY
            )
        self.assertEqual(post.call_count, 1)
        url, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, "https://api.cognitive.microsofttranslator.com/translate")
        self.assertEqual(kwargs["params"], {"api-version": "3.0", "to": "zh-Hans"})
        # 不传 from：让 Azure 自动检测源语言（标题里有多种语言）。
        self.assertNotIn("from", kwargs["params"])
        self.assertEqual(
            kwargs["headers"], {"Ocp-Apim-Subscription-Key": AZURE_KEY}
        )
        self.assertEqual(
            kwargs["json"], [{"Text": "Hello world"}, {"Text": "Breaking news"}]
        )
        self.assertEqual(
            results, {"Hello world": "ZH:Hello world", "Breaking news": "ZH:Breaking news"}
        )
        self.assertEqual(chars, len("Hello world") + len("Breaking news"))
        self.assertFalse(exhausted)

    def test_region_header_sent_when_set(self):
        post = mock.Mock(side_effect=lambda url, **kw: _azure_ok(["Hello world"]))
        with mock.patch.object(translate_mod.requests, "post", post):
            translate_mod.azure_translate_many(["Hello world"], AZURE_KEY, region=AZURE_REGION)
        headers = post.call_args[1]["headers"]
        self.assertEqual(headers["Ocp-Apim-Subscription-Region"], AZURE_REGION)
        self.assertEqual(headers["Ocp-Apim-Subscription-Key"], AZURE_KEY)

    def test_429_raises_azure_unavailable(self):
        with mock.patch.object(
            translate_mod.requests, "post",
            mock.Mock(return_value=_FakeResp(status_code=429, text="quota")),
        ):
            with self.assertRaises(translate_mod.AzureUnavailable):
                translate_mod.azure_translate_many(["Hello"], AZURE_KEY)

    def test_403_raises_azure_unavailable(self):
        with mock.patch.object(
            translate_mod.requests, "post",
            mock.Mock(return_value=_FakeResp(status_code=403, text="forbidden")),
        ):
            with self.assertRaises(translate_mod.AzureUnavailable):
                translate_mod.azure_translate_many(["Hello"], AZURE_KEY)

    def test_401_raises_azure_unavailable(self):
        with mock.patch.object(
            translate_mod.requests, "post",
            mock.Mock(return_value=_FakeResp(status_code=401, text="unauthorized")),
        ):
            with self.assertRaises(translate_mod.AzureUnavailable):
                translate_mod.azure_translate_many(["Hello"], AZURE_KEY)

    def test_network_error_raises_azure_unavailable(self):
        with mock.patch.object(
            translate_mod.requests, "post",
            mock.Mock(side_effect=translate_mod.requests.exceptions.ConnectionError("boom")),
        ):
            with self.assertRaises(translate_mod.AzureUnavailable):
                translate_mod.azure_translate_many(["Hello"], AZURE_KEY)

    def test_bad_json_raises_azure_unavailable(self):
        with mock.patch.object(
            translate_mod.requests, "post",
            mock.Mock(return_value=_FakeResp(status_code=200, json_data=ValueError("no json"))),
        ):
            with self.assertRaises(translate_mod.AzureUnavailable):
                translate_mod.azure_translate_many(["Hello"], AZURE_KEY)

    def test_char_budget_trims_batch_never_over_budget(self):
        """max_chars 参数截断批次：超出的文本不发，只发预算内的。"""
        texts = ["a" * 500, "b" * 500, "c" * 500]  # 1500 字符 > 1200
        post = mock.Mock(side_effect=lambda url, **kw: _azure_ok([b["Text"] for b in kw["json"]]))
        with mock.patch.object(translate_mod.requests, "post", post):
            results, chars, exhausted = translate_mod.azure_translate_many(
                texts, AZURE_KEY, max_chars=1200
            )
        self.assertEqual(post.call_count, 1)
        sent = post.call_args[1]["json"]
        self.assertEqual(len(sent), 2)  # 只发了前两条（1000 字符）
        self.assertLessEqual(sum(len(b["Text"]) for b in sent), 1200)
        self.assertEqual(chars, 1000)
        self.assertTrue(exhausted)
        self.assertEqual(len(results), 2)
        self.assertNotIn("c" * 500, results)

    def test_default_per_round_cap_is_600(self):
        """默认每轮 600 字符硬上限：3 条 300 字文本只发前 2 条（600 字）。

        600 × 96 轮/天 × 31 天 = 1,785,600 < 1,800,000（月度上限），
        见 docs/TRANSLATION.md 的算术。
        """
        texts = ["a" * 300, "b" * 300, "c" * 300]  # 900 字符 > 600
        post = mock.Mock(side_effect=lambda url, **kw: _azure_ok([b["Text"] for b in kw["json"]]))
        with mock.patch.object(translate_mod.requests, "post", post):
            results, chars, exhausted = translate_mod.azure_translate_many(texts, AZURE_KEY)
        self.assertEqual(post.call_count, 1)
        sent = post.call_args[1]["json"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(chars, 600)
        self.assertTrue(exhausted)
        self.assertNotIn("c" * 300, results)

    def test_usage_file_helpers(self):
        """月度用量文件：缺失按 0 处理、损坏按 0 处理并记 warning、跨月归零。"""
        with tempfile.TemporaryDirectory() as d:
            usage_path = Path(d) / ".azure_usage.json"
            month, used = translate_mod.load_azure_usage(usage_path)
            self.assertEqual((month, used), (_current_month(), 0))
            total = translate_mod.record_azure_usage(usage_path, 123)
            self.assertEqual(total, 123)
            self.assertEqual(
                _read_usage(d), {"month": _current_month(), "chars": 123}
            )
            # 再记一次：累加，不覆盖。
            self.assertEqual(translate_mod.record_azure_usage(usage_path, 77), 200)
            # 上个月的文件：读出来当月用量算 0。
            _write_usage(d, _prev_month(), 1799999)
            month, used = translate_mod.load_azure_usage(usage_path)
            self.assertEqual((month, used), (_current_month(), 0))
            # 损坏的文件：按 0 处理并记一条 warning，之后能重建。
            usage_path.write_text("this is not json {{{", encoding="utf-8")
            capture = _LogCapture()
            logger = logging.getLogger("trend_radar.translate")
            logger.addHandler(capture)
            logger.setLevel(logging.WARNING)
            try:
                month, used = translate_mod.load_azure_usage(usage_path)
            finally:
                logger.removeHandler(capture)
            self.assertEqual((month, used), (_current_month(), 0))
            self.assertTrue(any("用量文件损坏" in m for m in capture.messages))
            self.assertEqual(translate_mod.record_azure_usage(usage_path, 50), 50)
            self.assertEqual(
                _read_usage(d), {"month": _current_month(), "chars": 50}
            )

    def test_empty_texts_no_call(self):
        with mock.patch.object(translate_mod.requests, "post") as post:
            results, chars, exhausted = translate_mod.azure_translate_many([], AZURE_KEY)
        post.assert_not_called()
        self.assertEqual((results, chars, exhausted), ({}, 0, False))

    def test_key_never_in_exception_or_logs(self):
        """请求异常里即使夹带 key，抛出的 AzureUnavailable 也不含 key。"""
        err = translate_mod.requests.exceptions.ConnectionError(f"boom {AZURE_KEY}")
        capture = _LogCapture()
        logger = logging.getLogger("trend_radar.translate")
        logger.addHandler(capture)
        logger.setLevel(logging.WARNING)
        try:
            with mock.patch.object(
                translate_mod.requests, "post", mock.Mock(side_effect=err)
            ):
                with self.assertRaises(translate_mod.AzureUnavailable) as ctx:
                    translate_mod.azure_translate_many(["Hello"], AZURE_KEY)
        finally:
            logger.removeHandler(capture)
        self.assertNotIn(AZURE_KEY, str(ctx.exception))
        self.assertTrue(all(AZURE_KEY not in m for m in capture.messages))


class TestAzureWiring(unittest.TestCase):
    # 6 条标题：两两之间没有 ≥2 个共享词，不会聚成一个事件；
    # 互动量递减，排名顺序与之相同。
    SIX_TITLES = [
        ("Quantum zebra alpha report", "https://example.com/1", 60000),
        ("Mango orbital bravo briefing", "https://example.com/2", 50000),
        ("Titanium falcon charlie update", "https://example.com/3", 40000),
        ("Velvet comet delta analysis", "https://example.com/4", 30000),
        ("Copper nebula echo dispatch", "https://example.com/5", 20000),
        ("Silver vortex foxtrot bulletin", "https://example.com/6", 10000),
    ]

    def _six_items(self):
        return [
            _item("rss", title, url, engagement=eng)
            for title, url, eng in self.SIX_TITLES
        ]

    def test_azure_round_translates_top5_only_and_records_usage(self):
        """Azure 轮次只翻 Top 5 事件（名 + 成员标题去重后 5 条），第 6 个不翻；
        成功请求的字符记进 data/.azure_usage.json（不再用 .azure_last_run）。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            post = _post_dispatcher(
                azure_resp=lambda url, kw: _azure_ok([b["Text"] for b in kw["json"]]),
                deepl_resp=_deepl_ok(),
            )
            capture = _LogCapture()
            logger = logging.getLogger("trend_radar.collector")
            logger.addHandler(capture)
            logger.setLevel(logging.INFO)
            try:
                with (
                    mock.patch.dict(os.environ, _env_with(), clear=True),
                    mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=self._six_items())),
                    mock.patch.object(translate_mod.requests, "post", post),
                ):
                    counts = collect_once(config=_config(db_path))
            finally:
                logger.removeHandler(capture)
            self.assertEqual(counts["azure_translated"], 5)
            self.assertEqual(counts["deepl_translated"], 0)
            self.assertEqual(counts["saved"], 6)
            # 6 条文本只发了 1 个批量请求，且 DeepL 没被调。
            self.assertEqual(len(post.calls["azure"]), 1)
            self.assertEqual(post.calls["deepl"], [])
            sent = post.calls["azure"][0][1]["json"]
            self.assertEqual(len(sent), 5)
            # 不再写 .azure_last_run；用量文件里是 5 条标题的字符数。
            self.assertFalse((Path(d) / ".azure_last_run").exists())
            expected_chars = sum(len(t) for t, _u, _e in self.SIX_TITLES[:5])
            self.assertEqual(
                _read_usage(d), {"month": _current_month(), "chars": expected_chars}
            )
            self.assertTrue(
                any(
                    f"本月累计 {expected_chars}/1800000" in m for m in capture.messages
                )
            )
            conn = dbmod.connect(db_path)
            for title, url, _eng in self.SIX_TITLES[:5]:
                row = conn.execute(
                    "SELECT title_zh FROM items WHERE url = ?", (url,)
                ).fetchone()
                self.assertEqual(row["title_zh"], f"ZH:{title}", title)
            row6 = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/6",)
            ).fetchone()
            self.assertIsNone(row6["title_zh"])
            conn.close()

    def test_second_round_calls_azure_again_every_round(self):
        """Azure 每轮都跑：两轮（现实里相隔 15 分钟，这里连着跑）都调 Azure，
        不再有每小时门控；用量文件两轮累加。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            first = _item("rss", "First round story", "https://example.com/a")
            second = _item("rss", "Fresh story an hour later", "https://example.com/c")
            post = _post_dispatcher(
                azure_resp=lambda url, kw: _azure_ok([b["Text"] for b in kw["json"]]),
                deepl_resp=_deepl_ok(),
            )
            with (
                mock.patch.dict(os.environ, _env_with(), clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[first])),
                mock.patch.object(translate_mod.requests, "post", post),
            ):
                counts1 = collect_once(config=_config(db_path))
                self.assertEqual(len(post.calls["azure"]), 1)
                self.assertEqual(counts1["azure_translated"], 1)
                with mock.patch(
                    "trend_radar.collector.rss.fetch", mock.Mock(return_value=[second])
                ):
                    counts2 = collect_once(config=_config(db_path))
            self.assertEqual(len(post.calls["azure"]), 2)  # 第二轮也调了 Azure
            self.assertEqual(counts2["azure_translated"], 1)
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/c",)
            ).fetchone()
            conn.close()
            self.assertEqual(row["title_zh"], "ZH:Fresh story an hour later")
            expected = len("First round story") + len("Fresh story an hour later")
            self.assertEqual(
                _read_usage(d), {"month": _current_month(), "chars": expected}
            )

    def test_azure_429_falls_back_to_deepl_same_round(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            post = _post_dispatcher(
                azure_resp=_FakeResp(status_code=429, text="quota exceeded"),
                deepl_resp=_deepl_ok("苹果发布新款 iPhone 芯片"),
            )
            capture = _LogCapture()
            logger = logging.getLogger("trend_radar.translate")
            logger.addHandler(capture)
            logger.setLevel(logging.WARNING)
            try:
                with (
                    mock.patch.dict(os.environ, _env_with(), clear=True),
                    mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                    mock.patch.object(translate_mod.requests, "post", post),
                ):
                    counts = collect_once(config=_config(db_path))
            finally:
                logger.removeHandler(capture)
            # 同一轮：Azure 429 后 DeepL 补上，标题翻出来了。
            self.assertEqual(counts["azure_translated"], 0)
            self.assertEqual(counts["deepl_translated"], 1)
            self.assertEqual(len(post.calls["azure"]), 1)
            self.assertEqual(len(post.calls["deepl"]), 1)
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/a",)
            ).fetchone()
            conn.close()
            self.assertEqual(row["title_zh"], "苹果发布新款 iPhone 芯片")
            # 429 的 warning 只记一条，且不把“额度/限流”说死。
            warnings = [m for m in capture.messages if "429" in m]
            self.assertEqual(len(warnings), 1)
            # 失败的请求不计用量：用量文件根本没建；DeepL 补上后记了时间。
            self.assertFalse((Path(d) / ".azure_usage.json").exists())
            self.assertTrue((Path(d) / ".deepl_last_run").exists())

    def test_azure_403_falls_back_to_deepl_same_round(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            post = _post_dispatcher(
                azure_resp=_FakeResp(status_code=403, text="forbidden"),
                deepl_resp=_deepl_ok("苹果发布新款 iPhone 芯片"),
            )
            with (
                mock.patch.dict(os.environ, _env_with(), clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                mock.patch.object(translate_mod.requests, "post", post),
            ):
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["azure_translated"], 0)
            self.assertEqual(counts["deepl_translated"], 1)
            conn = dbmod.connect(db_path)
            row = conn.execute(
                "SELECT title_zh FROM items WHERE url = ?", ("https://example.com/a",)
            ).fetchone()
            conn.close()
            self.assertEqual(row["title_zh"], "苹果发布新款 iPhone 芯片")

    def test_no_azure_key_deepl_only(self):
        """没配 Azure key：Azure 一次都不调，DeepL 直接做主力。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            post = _post_dispatcher(
                azure_resp=lambda url, kw: (_ for _ in ()).throw(
                    AssertionError("Azure must not be called without a key")
                ),
                deepl_resp=_deepl_ok("苹果发布新款 iPhone 芯片"),
            )
            with (
                mock.patch.dict(os.environ, _env_with(azure=False), clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                mock.patch.object(translate_mod.requests, "post", post),
            ):
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["azure_translated"], 0)
            self.assertEqual(counts["deepl_translated"], 1)
            self.assertEqual(post.calls["azure"], [])
            self.assertEqual(len(post.calls["deepl"]), 1)
            self.assertFalse((Path(d) / ".azure_usage.json").exists())
            self.assertTrue((Path(d) / ".deepl_last_run").exists())

    def test_burst_alert_uses_chinese_name_same_round(self):
        """顺序约束：翻译在爆发检测之后、发提醒之前——提醒当轮就带中文事件名。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "trends.sqlite3"
            item = _item("rss", "Market rally continues", "https://example.com/a")
            event = {
                "event_id": "evt_1",
                "name": "Market rally continues",
                "member_count": 1,
                "platform_count": 2,
                "platforms": ["rss", "youtube"],
                "max_score": 10.0,
                "total_score": 10.0,
                "max_growth": 0.0,
                "first_seen": "2026-11-15T12:00:00+00:00",
                "member_urls": ["https://example.com/a"],
                "member_titles": ["Market rally continues"],
                "member_scores": [10.0],
            }
            burst = {
                "event_id": "evt_1",
                "name": "Market rally continues",
                "kind": "new_multi_platform",
                "reasons": ["new_multi_platform"],
                "current": {
                    "platforms": "rss,youtube",
                    "platform_count": 2,
                    "max_growth": 0.0,
                    "total_score": 10.0,
                    "item_count": 1,
                },
                "previous": None,
            }
            sent_texts = []

            def _tg_post(url, kwargs):
                sent_texts.append(kwargs["json"]["text"])
                return _FakeResp(status_code=200, json_data={"ok": True, "result": {}})

            post = _post_dispatcher(
                azure_resp=lambda url, kw: _azure_ok([b["Text"] for b in kw["json"]]),
                deepl_resp=_deepl_ok(),
                telegram_resp=_tg_post,
            )
            env = _env_with()
            env["TELEGRAM_BOT_TOKEN"] = "tg-token"
            env["TELEGRAM_CHAT_ID"] = "123"
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                mock.patch("trend_radar.clustering.cluster_from_db", mock.Mock(return_value=[event])),
                mock.patch("trend_radar.clustering.persist_events", mock.Mock(return_value=None)),
                mock.patch("trend_radar.burst.detect_bursts", mock.Mock(return_value=[burst])),
                mock.patch.object(translate_mod.requests, "post", post),
            ):
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["azure_translated"], 1)
            self.assertEqual(counts["alerts_sent"], 1)
            self.assertEqual(len(sent_texts), 1)
            self.assertIn("ZH:Market rally continues", sent_texts[0])
            # 内存里的 burst 也被写进了 name_zh（提醒格式化用的就是它）。
            self.assertEqual(burst["name_zh"], "ZH:Market rally continues")

    def test_round_char_budget_enforced(self):
        """一轮 600 字符硬上限：5 条 300 字标题只翻前 2 条，第 3 条起留英文。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            titles = [
                ("Alpha " * 50, "https://example.com/a", 50000),
                ("Beta " * 50, "https://example.com/b", 40000),
                ("Gamma " * 50, "https://example.com/c", 30000),
                ("Delta " * 50, "https://example.com/d", 20000),
                ("Epsilon " * 50, "https://example.com/e", 10000),
            ]
            items = [
                _item("rss", title.strip(), url, engagement=eng)
                for title, url, eng in titles
            ]
            post = _post_dispatcher(
                azure_resp=lambda url, kw: _azure_ok([b["Text"] for b in kw["json"]]),
                deepl_resp=_deepl_ok(),
            )
            with (
                mock.patch.dict(os.environ, _env_with(), clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=items)),
                mock.patch.object(translate_mod.requests, "post", post),
            ):
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["azure_translated"], 2)
            sent = post.calls["azure"][0][1]["json"]
            self.assertEqual(len(sent), 2)
            self.assertLessEqual(sum(len(b["Text"]) for b in sent), 600)
            conn = dbmod.connect(db_path)
            for url in ("https://example.com/c", "https://example.com/d", "https://example.com/e"):
                row = conn.execute(
                    "SELECT title_zh FROM items WHERE url = ?", (url,)
                ).fetchone()
                self.assertIsNone(row["title_zh"], url)
            conn.close()

    def test_member_titles_trimmed_before_event_names(self):
        """优先级：事件名在前、成员标题在后——600 字符预算先砍成员标题。

        两个事件，事件名与成员标题各不相同、每条 300 字：Top 5 事件名
        600 字已占满预算，成员标题一条都翻不到。
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "trends.sqlite3"

            def _event(event_id, name, member_title, score):
                return {
                    "event_id": event_id,
                    "name": name,
                    "member_count": 1,
                    "platform_count": 1,
                    "platforms": ["rss"],
                    "max_score": score,
                    "total_score": score,
                    "max_growth": 0.0,
                    "first_seen": "2026-11-15T12:00:00+00:00",
                    "member_urls": ["https://example.com/a"],
                    "member_titles": [member_title],
                    "member_scores": [score],
                    "items": [
                        {
                            "title": member_title,
                            "score": score,
                            "url": "https://example.com/a",
                            "platform": "rss",
                        }
                    ],
                }

            events = [
                _event("evt_a", "N" * 300, "M" * 300, 10.0),
                _event("evt_b", "O" * 300, "P" * 300, 9.0),
            ]
            items = [
                _item("rss", "M" * 300, "https://example.com/a", engagement=50000),
                _item("rss", "P" * 300, "https://example.com/b", engagement=40000),
            ]
            post = _post_dispatcher(
                azure_resp=lambda url, kw: _azure_ok([b["Text"] for b in kw["json"]]),
                deepl_resp=_deepl_ok(),
            )
            with (
                mock.patch.dict(os.environ, _env_with(), clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=items)),
                mock.patch("trend_radar.clustering.cluster_from_db", mock.Mock(return_value=events)),
                mock.patch("trend_radar.clustering.persist_events", mock.Mock(return_value=None)),
                mock.patch("trend_radar.burst.detect_bursts", mock.Mock(return_value=[])),
                mock.patch.object(translate_mod.requests, "post", post),
            ):
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["azure_translated"], 2)
            self.assertEqual(len(post.calls["azure"]), 1)
            sent = [b["Text"] for b in post.calls["azure"][0][1]["json"]]
            self.assertEqual(sent, ["N" * 300, "O" * 300])  # 只发了两个事件名
            conn = dbmod.connect(db_path)
            for url in ("https://example.com/a", "https://example.com/b"):
                row = conn.execute(
                    "SELECT title_zh FROM items WHERE url = ?", (url,)
                ).fetchone()
                self.assertIsNone(row["title_zh"], url)  # 成员标题没翻
            conn.close()

    def test_monthly_budget_trims_batch_to_remaining(self):
        """月度用量接近上限：本轮只翻剩余额度内的文本，超出部分留到下月。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            _write_usage(d, _current_month(), 1799500)  # 还剩 500 字
            titles = [
                ("Alpha " * 50, "https://example.com/a", 50000),
                ("Beta " * 50, "https://example.com/b", 40000),
            ]
            items = [
                _item("rss", title.strip(), url, engagement=eng)
                for title, url, eng in titles
            ]
            post = _post_dispatcher(
                azure_resp=lambda url, kw: _azure_ok([b["Text"] for b in kw["json"]]),
                deepl_resp=_deepl_ok(),
            )
            with (
                mock.patch.dict(os.environ, _env_with(), clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=items)),
                mock.patch.object(translate_mod.requests, "post", post),
            ):
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["azure_translated"], 1)
            sent = post.calls["azure"][0][1]["json"]
            self.assertEqual(len(sent), 1)  # 只发了第一条（299 字 ≤ 剩 500 字）
            self.assertEqual(sum(len(b["Text"]) for b in sent), 299)
            self.assertEqual(
                _read_usage(d), {"month": _current_month(), "chars": 1799799}
            )

    def test_monthly_budget_exhausted_skips_azure_deepl_runs(self):
        """月度用量用完：本轮整轮跳过 Azure（一次都不调），DeepL 按自己的
        每小时门控补上；用量文件不动。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            _write_usage(d, _current_month(), 1800000)
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            post = _post_dispatcher(
                azure_resp=lambda url, kw: (_ for _ in ()).throw(
                    AssertionError("Azure must not be called when monthly budget is exhausted")
                ),
                deepl_resp=_deepl_ok("苹果发布新款 iPhone 芯片"),
            )
            capture = _LogCapture()
            logger = logging.getLogger("trend_radar.collector")
            logger.addHandler(capture)
            logger.setLevel(logging.INFO)
            try:
                with (
                    mock.patch.dict(os.environ, _env_with(), clear=True),
                    mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                    mock.patch.object(translate_mod.requests, "post", post),
                ):
                    counts = collect_once(config=_config(db_path))
            finally:
                logger.removeHandler(capture)
            self.assertEqual(counts["azure_translated"], 0)
            self.assertEqual(counts["deepl_translated"], 1)
            self.assertEqual(post.calls["azure"], [])
            self.assertEqual(len(post.calls["deepl"]), 1)
            self.assertEqual(
                _read_usage(d), {"month": _current_month(), "chars": 1800000}
            )
            self.assertTrue(any("本月用量已达上限" in m for m in capture.messages))

    def test_month_rollover_resets_usage(self):
        """上个月的用量文件：当月用量按 0 算，整批都翻，文件写成当月。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            _write_usage(d, _prev_month(), 1799999)
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            post = _post_dispatcher(
                azure_resp=lambda url, kw: _azure_ok([b["Text"] for b in kw["json"]]),
                deepl_resp=_deepl_ok(),
            )
            with (
                mock.patch.dict(os.environ, _env_with(), clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                mock.patch.object(translate_mod.requests, "post", post),
            ):
                counts = collect_once(config=_config(db_path))
            self.assertEqual(counts["azure_translated"], 1)
            self.assertEqual(
                _read_usage(d),
                {"month": _current_month(), "chars": len("Apple unveils new iPhone chip")},
            )

    def test_corrupt_usage_file_treated_as_zero(self):
        """用量文件损坏：按 0 字处理（记一条 warning），翻译照常跑，
        文件被重建为正确内容。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            (Path(d) / ".azure_usage.json").write_text("not json {{{", encoding="utf-8")
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            post = _post_dispatcher(
                azure_resp=lambda url, kw: _azure_ok([b["Text"] for b in kw["json"]]),
                deepl_resp=_deepl_ok(),
            )
            capture = _LogCapture()
            logger = logging.getLogger("trend_radar.translate")
            logger.addHandler(capture)
            logger.setLevel(logging.WARNING)
            try:
                with (
                    mock.patch.dict(os.environ, _env_with(), clear=True),
                    mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                    mock.patch.object(translate_mod.requests, "post", post),
                ):
                    counts = collect_once(config=_config(db_path))
            finally:
                logger.removeHandler(capture)
            self.assertEqual(counts["azure_translated"], 1)
            self.assertTrue(any("用量文件损坏" in m for m in capture.messages))
            self.assertEqual(
                _read_usage(d),
                {"month": _current_month(), "chars": len("Apple unveils new iPhone chip")},
            )

    def test_config_monthly_cap_honored(self):
        """config.yaml 的 azure_monthly_cap_chars 生效：上限 500 字时，
        本轮只翻 500 字内的文本。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            titles = [
                ("Alpha " * 50, "https://example.com/a", 50000),
                ("Beta " * 50, "https://example.com/b", 40000),
            ]
            items = [
                _item("rss", title.strip(), url, engagement=eng)
                for title, url, eng in titles
            ]
            post = _post_dispatcher(
                azure_resp=lambda url, kw: _azure_ok([b["Text"] for b in kw["json"]]),
                deepl_resp=_deepl_ok(),
            )
            with (
                mock.patch.dict(os.environ, _env_with(), clear=True),
                mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=items)),
                mock.patch.object(translate_mod.requests, "post", post),
            ):
                counts = collect_once(
                    config=_config(db_path, azure_monthly_cap_chars=500)
                )
            self.assertEqual(counts["azure_translated"], 1)
            sent = post.calls["azure"][0][1]["json"]
            self.assertEqual(sum(len(b["Text"]) for b in sent), 299)
            self.assertEqual(
                _read_usage(d), {"month": _current_month(), "chars": 299}
            )

    def test_azure_failure_key_never_in_logs(self):
        """Azure 请求异常里夹带 key：轮次日志里也不出现 key。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            item = _item("rss", "Apple unveils new iPhone chip", "https://example.com/a")
            err = translate_mod.requests.exceptions.ConnectionError(f"boom {AZURE_KEY}")
            post = _post_dispatcher(
                azure_resp=lambda url, kw: (_ for _ in ()).throw(err),
                deepl_resp=_deepl_ok(),
            )
            capture = _LogCapture()
            for name in ("trend_radar.translate", "trend_radar.collector"):
                logging.getLogger(name).addHandler(capture)
                logging.getLogger(name).setLevel(logging.WARNING)
            try:
                with (
                    mock.patch.dict(os.environ, _env_with(), clear=True),
                    mock.patch("trend_radar.collector.rss.fetch", mock.Mock(return_value=[item])),
                    mock.patch.object(translate_mod.requests, "post", post),
                ):
                    counts = collect_once(config=_config(db_path))
            finally:
                for name in ("trend_radar.translate", "trend_radar.collector"):
                    logging.getLogger(name).removeHandler(capture)
            # Azure 失败后 DeepL 补上，轮次完成；日志里没有 key。
            self.assertEqual(counts["deepl_translated"], 1)
            self.assertTrue(all(AZURE_KEY not in m for m in capture.messages))


if __name__ == "__main__":
    unittest.main()
