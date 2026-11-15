"""首次见到基线测试：新内容第一轮增速必须记 0，不能把历史总量当增量。

背景（2026-11-15 发现的 bug）：没有上轮快照时 previous_engagement
默认为 0，YouTube 视频第一次被采到时，整个历史互动量会被除以
0.5 小时——12 万播放的视频算出 30,800/小时，实际第二轮只涨了
260 互动（520/小时），虚高约 59 倍；同时 collector 从不传
previous_growth，加速度也被翻倍。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from trend_radar import db as dbmod
from trend_radar.adapters import youtube as youtube_adapter
from trend_radar.collector import collect_once

ENGAGEMENT_1 = 15_400.0  # 120k views / 3k likes / 400 comments
ENGAGEMENT_2 = 15_660.0  # 第二轮只涨了 260


def _video(engagement: float) -> dict:
    return {
        "platform": "youtube",
        "title": "Test Video Alpha",
        "url": "https://www.youtube.com/watch?v=vid1",
        "published_at": "2026-11-15T06:00:00+00:00",
        "first_seen": "2026-11-15T11:00:00+00:00",
        "engagement": engagement,
        "views": 120000,
        "likes": 3000,
        "shares": 0,
        "comments": 400,
        "source": "Ch",
        "language": "",
        "summary_zh": "",
    }


def _config(db_path: Path) -> dict:
    return {
        "scan_interval_minutes": 15,
        "db_path": str(db_path),
        "sources": {
            "rss": {"enabled": False},
            "gdelt": {"enabled": False},
            "reddit": {"enabled": False},
            "youtube": {"enabled": True, "queries": ["test"], "interval_minutes": 30},
            "x": {"enabled": False},
            "tiktok": {"enabled": False},
        },
        "burst": {},
    }


def _row(db_path: Path) -> dict:
    conn = dbmod.connect(db_path)
    row = conn.execute("SELECT growth, acceleration, score FROM items").fetchone()
    snaps = conn.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()["c"]
    conn.close()
    return {"growth": row["growth"], "acceleration": row["acceleration"], "score": row["score"], "snapshots": snaps}


class TestFirstSeenBaseline(unittest.TestCase):
    def test_three_rounds(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            state = Path(d) / ".youtube_last_run"
            rounds = [[_video(ENGAGEMENT_1)], [_video(ENGAGEMENT_2)], [_video(ENGAGEMENT_2)]]
            with mock.patch(
                "trend_radar.collector.youtube.fetch", side_effect=rounds
            ):
                collect_once(config=_config(db_path))
                if state.exists():
                    state.unlink()
                collect_once(config=_config(db_path))
                if state.exists():
                    state.unlink()
                collect_once(config=_config(db_path))

            # 最终状态是第三轮：互动量没变，增速 0；加速度 = (0 - 520) / 0.5。
            final = _row(db_path)
            self.assertEqual(final["snapshots"], 3)
            self.assertEqual(final["growth"], 0)
            self.assertEqual(final["acceleration"], -1040.0)

            # 单独重放前两轮，验证第二轮只算真实增量、加速度带上轮增速。
            with tempfile.TemporaryDirectory() as d2:
                db2 = Path(d2) / "trends.sqlite3"
                with mock.patch(
                    "trend_radar.collector.youtube.fetch",
                    side_effect=[[_video(ENGAGEMENT_1)], [_video(ENGAGEMENT_2)]],
                ):
                    collect_once(config=_config(db2))
                    collect_once(config=_config(db2))
                second = _row(db2)
                self.assertEqual(second["snapshots"], 2)
                # (15,660 - 15,400) / 0.5 小时 = 520/小时，不是 30,800。
                self.assertEqual(second["growth"], 520.0)
                # 加速度 = (520 - 0) / 0.5 = 1040：上轮增速 0 被正确传入。
                self.assertEqual(second["acceleration"], 1040.0)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


_SEARCH_OK = {
    "items": [
        {
            "id": {"videoId": "vid1"},
            "snippet": {
                "title": "Test Video Alpha",
                "publishedAt": "2026-11-15T06:00:00Z",
                "channelTitle": "Ch",
            },
        }
    ]
}
_STATS_OK = {
    "items": [
        {
            "id": "vid1",
            "statistics": {"viewCount": "120000", "likeCount": "3000", "commentCount": "400"},
        }
    ]
}


class TestYoutubeStatsFailure(unittest.TestCase):
    def test_stats_call_failure_skips_video(self):
        # 统计接口失败：视频不入库、不留互动量 0 的基线快照。
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}), mock.patch(
            "trend_radar.adapters.youtube.requests.get",
            side_effect=[_Resp(_SEARCH_OK), requests.RequestException("boom")],
        ):
            self.assertEqual(youtube_adapter.fetch(["test"]), [])

    def test_stats_success_returns_item(self):
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}), mock.patch(
            "trend_radar.adapters.youtube.requests.get",
            side_effect=[_Resp(_SEARCH_OK), _Resp(_STATS_OK)],
        ):
            items = youtube_adapter.fetch(["test"])
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["engagement"], ENGAGEMENT_1)

    def test_video_missing_from_stats_skipped(self):
        # 统计接口成功，但这个视频没有统计条目：统计未知，不是 0，跳过。
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}), mock.patch(
            "trend_radar.adapters.youtube.requests.get",
            side_effect=[_Resp(_SEARCH_OK), _Resp({"items": []})],
        ):
            self.assertEqual(youtube_adapter.fetch(["test"]), [])


if __name__ == "__main__":
    unittest.main()
