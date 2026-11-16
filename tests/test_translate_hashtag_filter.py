"""纯 hashtag / URL / @提及标题排除出翻译目标的测试。

动机：2026-11-16 10:03 轮实测，Azure 对纯 hashtag 串原样返回
（HTTP 200，译文与原文相同），代码算成“没有可用译文”还占预算。
这类文本不应进翻译目标集、不调接口、不占预算、不触发 warning。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar import translate as translate_mod
from trend_radar.collector import _collect_priority_texts, _translate_priority_texts

HASHTAG_ONLY = (
    "#generalknowledge #fact #technology #science #machine "
    "#marathiculture#maharashtra#marathisciencewala"
)
CLOWN_TITLE = "Clown's Artificial Intelligence#cosplay #shorts #SpiderMan#kpop"

AZURE_KEY = "azure-test-key-123"
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


def _azure_ok(texts):
    """把每个输入文本译成 'ZH:' + 原文，顺序一一对应。"""
    return _FakeResp(
        status_code=200,
        json_data=[
            {"translations": [{"text": f"ZH:{t}", "to": "zh-Hans"}]} for t in texts
        ],
    )


def _recording_post(calls):
    """记录所有 requests.post 调用；Azure / DeepL 各返回成功译文。"""

    def _post(url, **kwargs):
        calls.append((url, kwargs))
        if "cognitive.microsofttranslator.com" in url:
            body = kwargs.get("json") or []
            return _azure_ok([item["Text"] for item in body])
        if "deepl.com" in url:
            return _FakeResp(
                status_code=200,
                json_data={
                    "translations": [
                        {"text": "已翻译", "detected_source_language": "EN"}
                    ]
                },
            )
        raise AssertionError(f"unexpected POST to {url}")

    return mock.Mock(side_effect=_post)


def _event(event_id, name, member_title, member_score=8.0):
    return {
        "event_id": event_id,
        "name": name,
        "items": [{"title": member_title, "score": member_score}],
    }


def _tr_cfg():
    return {
        "enabled": True,
        "target_lang": "zh-CN",
        "azure_max_per_round": 15,
        "deepl_max_per_round": 15,
        "azure_max_chars_per_round": 600,
        "azure_monthly_cap_chars": 1800000,
    }


class TestCollectPriorityTextsFilter(unittest.TestCase):
    def test_hashtag_only_excluded_real_kept(self):
        # 混进一条纯 hashtag 标题：输出里没有它，其他标题都在。
        evt_hash = _event("evt_hash", HASHTAG_ONLY, HASHTAG_ONLY, 9.0)
        evt_real = _event("evt_real", "Breaking news", "Real title here", 8.0)
        events_by_id = {"evt_hash": evt_hash, "evt_real": evt_real}
        entries = _collect_priority_texts(events_by_id, [evt_hash, evt_real], [])
        texts = [e["text"] for e in entries]
        self.assertEqual(texts, ["Breaking news", "Real title here"])
        self.assertNotIn(HASHTAG_ONLY, texts)

    def test_hashtag_burst_name_excluded(self):
        # 不在本轮聚类输出里的爆发事件名同样过滤。
        evt_real = _event("evt_real", "Breaking news", "Real title here", 8.0)
        events_by_id = {"evt_real": evt_real}
        bursts = [{"event_id": "evt_ghost", "name": HASHTAG_ONLY}]
        entries = _collect_priority_texts(events_by_id, [evt_real], bursts)
        texts = [e["text"] for e in entries]
        self.assertEqual(texts, ["Breaking news", "Real title here"])

    def test_mixed_title_kept(self):
        # 带正文 + hashtag 的标题照常进目标集。
        evt = _event("evt_clown", CLOWN_TITLE, CLOWN_TITLE, 8.0)
        entries = _collect_priority_texts({"evt_clown": evt}, [evt], [])
        texts = [e["text"] for e in entries]
        self.assertIn(CLOWN_TITLE, texts)


class TestTranslatePriorityTextsEmptyBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "trends.sqlite3"
        self.conn = dbmod.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_all_filtered_no_api_call(self):
        # 优先文本全被过滤掉：不调 Azure/DeepL，不发空请求。
        # 两个 key 都配上——证明即使两个提供方都可用也不调。
        evt_hash = _event("evt_hash", HASHTAG_ONLY, HASHTAG_ONLY, 9.0)
        calls = []
        with mock.patch.object(
            translate_mod.requests, "post", _recording_post(calls)
        ):
            azure_count, deepl_count = _translate_priority_texts(
                self.conn,
                [evt_hash],
                [evt_hash],
                [],
                _tr_cfg(),
                AZURE_KEY,
                "",
                DEEPL_KEY,
                self.db_path,
            )
        self.assertEqual((azure_count, deepl_count), (0, 0))
        self.assertEqual(calls, [])

    def test_mixed_only_real_texts_sent(self):
        # 混合时只有真实文本进请求体，hashtag 串不占预算。
        evt_hash = _event("evt_hash", HASHTAG_ONLY, HASHTAG_ONLY, 9.0)
        evt_real = _event("evt_real", "Breaking news", "Real title here", 8.0)
        self.conn.execute(
            "INSERT INTO events (event_id, name) VALUES (?, ?)",
            ("evt_real", "Breaking news"),
        )
        self.conn.commit()
        calls = []
        with mock.patch.object(
            translate_mod.requests, "post", _recording_post(calls)
        ):
            azure_count, deepl_count = _translate_priority_texts(
                self.conn,
                [evt_hash, evt_real],
                [evt_hash, evt_real],
                [],
                _tr_cfg(),
                AZURE_KEY,
                "",
                "",
                self.db_path,
            )
        self.assertEqual((azure_count, deepl_count), (2, 0))
        self.assertEqual(len(calls), 1)
        body = calls[0][1]["json"]
        self.assertEqual(
            [item["Text"] for item in body],
            ["Breaking news", "Real title here"],
        )
        row = self.conn.execute(
            "SELECT name_zh FROM events WHERE event_id = ?", ("evt_real",)
        ).fetchone()
        self.assertEqual(row["name_zh"], "ZH:Breaking news")


if __name__ == "__main__":
    unittest.main()
