"""标题翻译测试：只 mock requests.get，绝不调真实翻译接口。

gtx 端点返回形如 [[["你好世界","Hello world",null,null,3]],null,"en"]，
data[0] 是 [译文, 原文, ...] 的分段列表。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import translate as translate_mod
from trend_radar.translate import needs_translation, translate_text, translate_titles

MALAYALAM_TITLE = (
    "Today Breaking News LIVE | ഇന്നത്തെ പ്രധാന വാർത്ത | "
    "Top Headlines Of The Hour | 15.11.2026"
)


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, json_raises=False):
        self.status_code = status_code
        self._json_data = json_data
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("No JSON object could be decoded")
        return self._json_data


def _gtx(*segments):
    """造一段 gtx 响应：每段是 (译文, 原文)。"""
    return [[list(seg) + [None, None, 3] for seg in segments], None, "en"]


class TestNeedsTranslation(unittest.TestCase):
    def test_pure_chinese_false(self):
        self.assertFalse(needs_translation("苹果发布新款芯片"))
        self.assertFalse(needs_translation("今日头条：科技新闻"))

    def test_english_true(self):
        self.assertTrue(needs_translation("Apple unveils new iPhone chip"))
        self.assertTrue(needs_translation("Breaking news"))

    def test_mixed_true(self):
        # 中英混合、且中文占比不到 90% 时仍需翻译。
        self.assertTrue(needs_translation(MALAYALAM_TITLE))
        self.assertTrue(needs_translation("iPhone 17 发布"))

    def test_pure_non_latin_script_true(self):
        # 纯小语种标题（一个拉丁/CJK 字母都没有）也要翻译，不能误判为“没有字母”。
        self.assertTrue(needs_translation("ഇന്നത്തെ പ്രധാന വാർത്ത"))
        self.assertTrue(needs_translation("Новости дня"))

    def test_mostly_chinese_false(self):
        # 中文占 90% 以上时不翻译（夹一个英文字母不算需要翻译）。
        self.assertFalse(needs_translation("苹果发布新款芯片手机 i"))

    def test_empty_false(self):
        self.assertFalse(needs_translation(""))
        self.assertFalse(needs_translation("   "))

    def test_digits_punctuation_only_false(self):
        # 没有字母：数字、标点、日期不计入分母，直接判为不需要翻译。
        self.assertFalse(needs_translation("15.11.2026"))
        self.assertFalse(needs_translation("!!!"))
        self.assertFalse(needs_translation("12345"))


class TestTranslateText(unittest.TestCase):
    def test_gtx_success_joins_segments(self):
        with mock.patch.object(translate_mod.requests, "get") as get:
            get.return_value = _FakeResp(
                json_data=_gtx(("你好", "Hello "), ("世界", "world"))
            )
            self.assertEqual(translate_text("Hello world"), "你好世界")
        _, kwargs = get.call_args
        params = kwargs["params"]
        self.assertEqual(params["client"], "gtx")
        self.assertEqual(params["sl"], "auto")
        self.assertEqual(params["tl"], "zh-CN")
        self.assertEqual(params["dt"], "t")
        self.assertEqual(params["q"], "Hello world")

    def test_malayalam_title(self):
        with mock.patch.object(translate_mod.requests, "get") as get:
            get.return_value = _FakeResp(
                json_data=_gtx(
                    (
                        "今日突发新闻直播 | 今天的主要新闻 | 每小时头条 | 15.11.2026",
                        MALAYALAM_TITLE,
                    )
                )
            )
            self.assertEqual(
                translate_text(MALAYALAM_TITLE),
                "今日突发新闻直播 | 今天的主要新闻 | 每小时头条 | 15.11.2026",
            )

    def test_target_lang_passed_through(self):
        with mock.patch.object(translate_mod.requests, "get") as get:
            get.return_value = _FakeResp(json_data=_gtx(("Hola", "Hello")))
            self.assertEqual(translate_text("Hello", target_lang="es"), "Hola")
        self.assertEqual(get.call_args.kwargs["params"]["tl"], "es")

    def test_non_200_returns_original(self):
        with mock.patch.object(translate_mod.requests, "get") as get:
            get.return_value = _FakeResp(status_code=429, json_data=None)
            self.assertEqual(translate_text("Hello world"), "Hello world")

    def test_bad_json_returns_original(self):
        with mock.patch.object(translate_mod.requests, "get") as get:
            get.return_value = _FakeResp(json_raises=True)
            self.assertEqual(translate_text("Hello world"), "Hello world")

    def test_empty_result_returns_original(self):
        with mock.patch.object(translate_mod.requests, "get") as get:
            get.return_value = _FakeResp(json_data={})
            self.assertEqual(translate_text("Hello world"), "Hello world")
        with mock.patch.object(translate_mod.requests, "get") as get:
            get.return_value = _FakeResp(json_data=[[], None, "en"])
            self.assertEqual(translate_text("Hello world"), "Hello world")

    def test_connection_error_returns_original(self):
        with mock.patch.object(translate_mod.requests, "get") as get:
            get.side_effect = requests.ConnectionError("DNS failure")
            self.assertEqual(translate_text("Hello world"), "Hello world")

    def test_empty_text_no_request(self):
        with mock.patch.object(translate_mod.requests, "get") as get:
            self.assertEqual(translate_text(""), "")
            self.assertEqual(translate_text("   "), "   ")
        get.assert_not_called()


class TestTranslateTitles(unittest.TestCase):
    def test_chinese_skipped_without_http_call(self):
        with mock.patch.object(translate_mod.requests, "get") as get:
            get.return_value = _FakeResp(json_data=_gtx(("你好世界", "Hello world")))
            result = translate_titles(["苹果发布新款芯片", "Hello world", "苹果发布新款芯片"])
        self.assertEqual(
            result, {"苹果发布新款芯片": "苹果发布新款芯片", "Hello world": "你好世界"}
        )
        # 相同标题只翻一次；中文标题不调接口。
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
