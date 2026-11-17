"""事件综合排名测试：纯函数，不读数据库、不联网。"""
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar.event_ranking import rank_events

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _ev(event_id, first_seen_hours_ago=1.0, **overrides):
    ev = {
        "event_id": event_id,
        "name": f"事件 {event_id}",
        "name_zh": None,
        "first_seen": (NOW - timedelta(hours=first_seen_hours_ago)).isoformat(),
        "platforms": ["rss"],
        "platform_count": 1,
        "item_count": 1,
        "total_score": 0.5,
        "max_score": 0.5,
        "max_growth": 0.0,
        "trend": "flat",
        "items": [],
    }
    ev.update(overrides)
    return ev


class TestRankEvents(unittest.TestCase):
    def test_multi_platform_beats_single_platform(self):
        """成员分数相同，多平台事件排在单平台前面。"""
        multi = _ev("evt_multi", platforms=["rss", "youtube", "gdelt"], platform_count=3)
        single = _ev("evt_single", platforms=["rss"], platform_count=1)
        ranked = rank_events([single, multi], now=NOW)
        self.assertEqual(ranked[0]["event_id"], "evt_multi")
        self.assertGreater(ranked[0]["rank_score"], ranked[1]["rank_score"])

    def test_high_growth_beats_high_volume(self):
        """项目一贯的判断：小而快的事件胜过总量大但不动的事件。"""
        fast = _ev(
            "evt_fast", max_growth=1000.0, max_score=0.3, total_score=0.6, item_count=2
        )
        huge = _ev(
            "evt_huge", max_growth=0.0, max_score=0.9, total_score=9.0, item_count=10
        )
        ranked = rank_events([huge, fast], now=NOW)
        self.assertEqual(ranked[0]["event_id"], "evt_fast")

    def test_bursting_beats_identical_non_bursting(self):
        """其他条件完全相同，爆发的事件排在前面，且原因里带爆发词。"""
        a = _ev("evt_a")
        b = _ev("evt_b")
        bursts = [{"event_id": "evt_b", "reasons": ["growth_spike"]}]
        ranked = rank_events([a, b], bursts=bursts, now=NOW)
        self.assertEqual(ranked[0]["event_id"], "evt_b")
        self.assertIn("增速突增", ranked[0]["rank_reasons"])

    def test_recurrence_loses_burst_boost_and_novelty(self):
        """重复出现：爆发分量和新鲜度分量都记 0，分差恰好是两项权重之和。

        两个完全相同的事件（刚出现，新鲜度 1.0），一个是正常爆发，
        一个是重复出现：正常 65.0 分（0.30×0.5 + 0.20×0.5 + 0.20×1
        + 0.20×0.5 + 0.10×1.0），重复出现 35.0 分，分差正好 30.0
        = (0.20 爆发 + 0.10 新鲜度) × 100。原因里也不写“首次出现”。
        """
        a = _ev("evt_a", first_seen_hours_ago=0.0)
        b = _ev("evt_b", first_seen_hours_ago=0.0)
        bursts = [
            {"event_id": "evt_a", "reasons": ["new_multi_platform"]},
            {
                "event_id": "evt_b",
                "reasons": ["new_multi_platform"],
                "is_recurrence": True,
            },
        ]
        ranked = rank_events([a, b], bursts=bursts, now=NOW)
        by_id = {e["event_id"]: e for e in ranked}
        self.assertEqual(by_id["evt_a"]["rank_score"], 65.0)
        self.assertEqual(by_id["evt_b"]["rank_score"], 35.0)
        self.assertEqual(
            by_id["evt_a"]["rank_score"] - by_id["evt_b"]["rank_score"], 30.0
        )
        self.assertIn("新事件首次跨平台", by_id["evt_a"]["rank_reasons"])
        self.assertIn("1 小时内首次出现", by_id["evt_a"]["rank_reasons"])
        self.assertNotIn("新事件首次跨平台", by_id["evt_b"]["rank_reasons"])
        self.assertTrue(
            all("首次出现" not in r for r in by_id["evt_b"]["rank_reasons"])
        )

    def test_recurrence_keeps_growth_member_and_platform(self):
        """重复出现只扣爆发和新鲜度：增长、成员强度、跨平台分量照常算——
        一个高增长的重复出现事件仍能胜过低增长的普通事件。"""
        rec = _ev(
            "evt_rec", first_seen_hours_ago=0.0, max_growth=1000.0,
            max_score=0.9, total_score=9.0, platform_count=3,
            platforms=["rss", "youtube", "gdelt"],
        )
        plain = _ev("evt_plain", first_seen_hours_ago=0.0)
        bursts = [
            {"event_id": "evt_rec", "reasons": ["new_multi_platform"],
             "is_recurrence": True}
        ]
        ranked = rank_events([plain, rec], bursts=bursts, now=NOW)
        self.assertEqual(ranked[0]["event_id"], "evt_rec")

    def test_fresh_beats_old(self):
        """其他条件完全相同，1 小时前出现的事件胜过 2 天前的。"""
        fresh = _ev("evt_fresh", first_seen_hours_ago=1.0)
        old = _ev("evt_old", first_seen_hours_ago=48.0)
        ranked = rank_events([old, fresh], now=NOW)
        self.assertEqual(ranked[0]["event_id"], "evt_fresh")

    def test_empty_input(self):
        self.assertEqual(rank_events([]), [])

    def test_single_event_no_division_by_zero(self):
        ranked = rank_events([_ev("evt_only")], now=NOW)
        self.assertEqual(len(ranked), 1)
        self.assertTrue(0.0 <= ranked[0]["rank_score"] <= 100.0)

    def test_scores_within_0_100(self):
        events = [
            _ev("evt_1", max_growth=5000.0, max_score=0.95, total_score=12.0,
                platform_count=3, platforms=["rss", "youtube", "gdelt"], item_count=20),
            _ev("evt_2", max_growth=10.0, max_score=0.2, total_score=0.2,
                platform_count=1, item_count=1),
            _ev("evt_3", max_growth=0.0, max_score=0.6, total_score=3.0,
                platform_count=2, platforms=["rss", "reddit"], item_count=5,
                first_seen_hours_ago=30.0),
        ]
        bursts = [{"event_id": "evt_2", "reasons": ["new_multi_platform"]}]
        for ev in rank_events(events, bursts=bursts, now=NOW):
            self.assertTrue(0.0 <= ev["rank_score"] <= 100.0)

    def test_rank_reasons_nonempty_chinese(self):
        """每个事件的原因都是非空的中文短语列表。"""
        events = [
            _ev("evt_1", max_growth=100.0, platform_count=2,
                platforms=["rss", "youtube"]),
            _ev("evt_2"),  # 平平无奇的事件：兜底原因也得是非空中文
            _ev("evt_3", first_seen_hours_ago=100.0),
        ]
        ranked = rank_events(events, now=NOW)
        for ev in ranked:
            self.assertTrue(ev["rank_reasons"])
            for reason in ev["rank_reasons"]:
                self.assertTrue(CJK_RE.search(reason), reason)

    def test_deterministic(self):
        events = [
            _ev("evt_1", max_growth=100.0, max_score=0.8),
            _ev("evt_2", max_growth=100.0, max_score=0.8),
            _ev("evt_3", max_growth=50.0, max_score=0.4),
        ]
        first = [ev["event_id"] for ev in rank_events(events, now=NOW)]
        second = [ev["event_id"] for ev in rank_events(events, now=NOW)]
        self.assertEqual(first, second)
        # 同分时按 event_id 排：evt_1 和 evt_2 分数完全相同。
        self.assertLess(first.index("evt_1"), first.index("evt_2"))

    def test_missing_fields_no_crash(self):
        """缺 first_seen / max_growth 不 crash，按 0 / 很老处理。"""
        bare = {"event_id": "evt_bare", "name": "只有名字"}
        normal = _ev("evt_normal", max_growth=10.0)
        ranked = rank_events([bare, normal], now=NOW)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0]["event_id"], "evt_normal")

    def test_input_not_mutated(self):
        events = [_ev("evt_1", max_growth=100.0), _ev("evt_2", max_growth=10.0)]
        original_ids = [ev["event_id"] for ev in events]
        ranked = rank_events(events, now=NOW)
        self.assertEqual([ev["event_id"] for ev in events], original_ids)
        for ev in events:
            self.assertNotIn("rank_score", ev)
            self.assertNotIn("rank_reasons", ev)
        self.assertIsNot(ranked[0], events[0])

    def test_weights_override(self):
        """权重可覆盖：增长权重归零后，大总量事件反超小而快的事件。"""
        fast = _ev(
            "evt_fast", max_growth=1000.0, max_score=0.3, total_score=0.6, item_count=2
        )
        huge = _ev(
            "evt_huge", max_growth=0.0, max_score=0.9, total_score=9.0, item_count=10
        )
        default = rank_events([huge, fast], now=NOW)
        self.assertEqual(default[0]["event_id"], "evt_fast")
        no_growth = rank_events(
            [huge, fast],
            now=NOW,
            weights={
                "growth": 0.0,
                "member_strength": 1.0,
                "burst": 0.0,
                "cross_platform": 0.0,
                "novelty": 0.0,
            },
        )
        self.assertEqual(no_growth[0]["event_id"], "evt_huge")

    def test_half_life_override(self):
        """半衰期可覆盖：半衰期越短，新旧事件的新鲜度差距越大。"""
        fresh = _ev("evt_fresh", first_seen_hours_ago=1.0)
        old = _ev("evt_old", first_seen_hours_ago=48.0)
        default = rank_events([old, fresh], now=NOW)
        short = rank_events(
            [old, fresh], now=NOW, weights={"freshness_half_life_hours": 1.0}
        )
        gap_default = default[0]["rank_score"] - default[1]["rank_score"]
        gap_short = short[0]["rank_score"] - short[1]["rank_score"]
        self.assertNotEqual(gap_default, gap_short)


def _screenshot_batch():
    """2026-11-16 20:28 Dashboard 截图的 Top 10 批（mock，数值取自截图）。

    #1 警察局长讲话（增长 1544.73，最高分 0.4555，总分 45.95）；
    #2 纯 hashtag 印尼标题（增长 375.67，最高分 0.35）；
    #3 小丑（增长 169.26，最高分 0.34，总分 2.94，11 条）；
    其余 7 条取自截图的增长/分数。未惩罚时顺序与截图一致
    （警察局长、hashtag、小丑前 3）。
    """
    now = datetime(2026, 11, 16, 20, 30, tzinfo=timezone.utc)

    def ev(eid, name, growth, max_score, total_score, age_h, items=1):
        return {
            "event_id": eid,
            "name": name,
            "name_zh": None,
            "first_seen": (now - timedelta(hours=age_h)).isoformat(),
            "platforms": ["youtube"],
            "platform_count": 1,
            "item_count": items,
            "total_score": total_score,
            "max_score": max_score,
            "max_growth": growth,
            "trend": "rising",
            "items": [],
        }

    return now, [
        ev("evt_police", "BREAKING NEWS - Arahan Kapolri Suyudi di HUT ke-81 Brimob Polri", 1544.73, 0.4555, 45.95, 1.81, items=146),
        ev("evt_hashtag", "最新消息#rakyat#KPK #klipingberita #politikindonesia #fayruseh #dpr", 375.67, 0.35, 0.35, 2.0),
        ev("evt_clown", "Clown's Artificial Intelligence#cosplay #shorts #SpiderMan#kpop", 169.26, 0.34, 2.94, 20.0, items=11),
        ev("evt_arabic", "الذكاء الاصطناعي 😂 #ضحك #الكويت #تحشيش #اشتراك_بالقناة", 185.76, 0.30, 0.30, 6.0),
        ev("evt_aimachine", "人工智能——如果我们只是机器呢？", 65.26, 0.30, 0.30, 2.0),
        ev("evt_loultimo", "LO ULTIMO !! JORGE ALFREDO VARGAS", 57.42, 0.30, 0.30, 2.0),
        ev("evt_realai", "现实生活与人工智能", 258.97, 0.34, 0.34, 14.0),
        ev("evt_whatbox", "“What box?” Has AI already escaped? 🤔 #ai #aieffects #artificialintelligence", 37.39, 0.30, 0.30, 1.0),
        ev("evt_russian", "627. Искусственный интеллект против содержанок", 57.86, 0.30, 0.30, 8.0),
        ev("evt_musk", "埃隆·马斯克关于谷歌人工智能的看法是对的吗？ #AI#Google #ElonMusk #ArtificialIntelligence", 89.83, 0.32, 0.32, 13.0),
    ]


class TestHashtagPenalty(unittest.TestCase):
    def test_screenshot_order_without_penalty(self):
        """惩罚关掉时，截图的顺序重现：警察局长、hashtag、小丑前 3。"""
        now, batch = _screenshot_batch()
        ranked = rank_events(
            batch, now=now, hashtag_penalty_max=0, generic_title_penalty_max=0
        )
        top3 = [e["event_id"] for e in ranked[:3]]
        self.assertEqual(top3, ["evt_police", "evt_hashtag", "evt_clown"])

    def test_hashtag_and_clown_drop_out_of_top3(self):
        """默认惩罚（上限 30 分）：小丑与纯 hashtag 标题掉出前 3，
        警察局长（占比 0）仍是第 1；30 分下纯 hashtag 标题扣到 0 分，
        小丑剩约 9 分（20 分时分别是 8.3 和 14.2）。"""
        now, batch = _screenshot_batch()
        ranked = rank_events(batch, now=now, generic_title_penalty_max=0)
        self.assertEqual(ranked[0]["event_id"], "evt_police")
        top3 = [e["event_id"] for e in ranked[:3]]
        self.assertNotIn("evt_clown", top3)
        self.assertNotIn("evt_hashtag", top3)
        by_id = {e["event_id"]: e for e in ranked}
        self.assertEqual(by_id["evt_hashtag"]["rank_score"], 0.0)
        self.assertAlmostEqual(by_id["evt_clown"]["rank_score"], 9.0, delta=0.2)

    def test_penalty_proportional_to_ratio(self):
        """扣分与 hashtag 占比成正比：占比越高扣得越多。"""
        now, batch = _screenshot_batch()
        ranked = {
            e["event_id"]: e
            for e in rank_events(batch, now=now, generic_title_penalty_max=0)
        }
        plain = {
            e["event_id"]: e
            for e in rank_events(
                batch, now=now, hashtag_penalty_max=0, generic_title_penalty_max=0
            )
        }
        for eid in ("evt_hashtag", "evt_clown", "evt_musk"):
            drop = plain[eid]["rank_score"] - ranked[eid]["rank_score"]
            self.assertGreater(drop, 0)
            # rank_score 输出只保留 1 位小数，差值与 30×占比允许 0.2 误差；
            # 扣分超过原分时下限 0 截断，差值就是原分本身。
            self.assertAlmostEqual(
                drop,
                min(30.0 * ranked[eid]["hashtag_ratio"], plain[eid]["rank_score"]),
                delta=0.2,
            )
        # 占比 0 的警察局长一分不扣。
        self.assertEqual(
            ranked["evt_police"]["rank_score"], plain["evt_police"]["rank_score"]
        )

    def test_hashtag_ratio_and_recurrence_in_output(self):
        """输出里带 hashtag_ratio 与 is_recurrence，供简报与 Dashboard 直接读。"""
        now, batch = _screenshot_batch()
        bursts = [{"event_id": "evt_realai", "reasons": ["growth_spike"], "is_recurrence": True}]
        ranked = {e["event_id"]: e for e in rank_events(batch, bursts=bursts, now=now)}
        self.assertGreater(ranked["evt_hashtag"]["hashtag_ratio"], 0.8)
        self.assertAlmostEqual(ranked["evt_clown"]["hashtag_ratio"], 0.51, places=2)
        self.assertEqual(ranked["evt_police"]["hashtag_ratio"], 0.0)
        self.assertTrue(ranked["evt_realai"]["is_recurrence"])
        self.assertFalse(ranked["evt_police"]["is_recurrence"])

    def test_hashtag_reason_shown(self):
        """占比达到阈值时，原因里写出 hashtag 占比（扣分有出处）。"""
        now, batch = _screenshot_batch()
        ranked = {e["event_id"]: e for e in rank_events(batch, now=now)}
        self.assertTrue(
            any("hashtag" in r for r in ranked["evt_clown"]["rank_reasons"])
        )
        self.assertFalse(
            any("hashtag" in r for r in ranked["evt_police"]["rank_reasons"])
        )

    def test_config_file_has_penalty_key(self):
        """config.yaml 里有 ranking.hashtag_penalty_max（默认 30.0）。"""
        from trend_radar.event_ranking import load_ranking_config

        text = (Path(__file__).resolve().parent.parent / "config.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("hashtag_penalty_max", text)
        self.assertEqual(load_ranking_config()["hashtag_penalty_max"], 30.0)


def _screenshot_batch_1117():
    """2026-11-17 11:45 Dashboard 截图的 Top 10 批（mock，数值取自截图）。

    标题用面板上显示的中文名（同 11-16 mock 的做法）；增长与首次出现
    取自截图的“最大增长/小时”和“首次出现”列；最高分取自截图的原因
    栏（有显示的 6 条：#2 0.36、#6 0.33、#7 0.32、#8 0.32、#9 0.31、
    #10 0.32）。#1 0.46、#3/#4/#5 0.315 是截图未显示的推测值，只为
    复现顺序；total_score 截图里不显示，取与 max_score 相近的值
    （#7 取 0.33、#10 取 0.20 微调顺序）。真实批次有几百个事件，
    min-max 在全批里算，所以 mock 的绝对分（64.8/25.6/24.9/…）和
    截图（64.8/23.8/22/…）不完全一样，锁的是顺序——和 11-16 mock 一样。

    第 11 行 evt_filler 是为测 Top 10 边界加的填充行（中性标题，
    分数介于 #10 与被扣后的 #3 之间）：不加它，10 行 mock 里被扣到
    末尾仍在前 10，测不出“掉出 Top 10”。
    """
    now = datetime(2026, 11, 17, 11, 45, tzinfo=timezone.utc)

    def ev(eid, name, growth, max_score, total_score, first_seen):
        return {
            "event_id": eid,
            "name": name,
            "name_zh": None,
            "first_seen": first_seen,
            "platforms": ["youtube"],
            "platform_count": 1,
            "item_count": 1,
            "total_score": total_score,
            "max_score": max_score,
            "max_growth": growth,
            "trend": "rising",
            "items": [],
        }

    return now, [
        ev("evt_delhi", "今日突发新闻|11月17日新闻 |巴巴·巴格什瓦尔 |拉姆卡塔 |阿克希莱…", 1044.58, 0.46, 0.46, "2026-11-16T10:18:00+00:00"),
        ev("evt_agent", "人工智能代理：人工智能的下一轮进化（你需要了解的一切）", 76.38, 0.36, 0.36, "2026-11-16T12:03:00+00:00"),
        ev("evt_breaking", "突发新闻", 81.6, 0.315, 0.315, "2026-11-17T09:54:00+00:00"),
        ev("evt_hokoon", "突发新闻：赛义德·霍孔被捕 |审讯正在进行中", 54.75, 0.315, 0.315, "2026-11-17T09:22:00+00:00"),
        ev("evt_iran", "[突发新闻] 伊朗反射炸弹对抗华盛顿目标？", 0, 0.315, 0.315, "2026-11-17T11:44:00+00:00"),
        ev("evt_vargas", "最新消息！！豪尔赫·阿尔弗雷多·巴尔加斯", 49.22, 0.33, 0.33, "2026-11-16T18:53:00+00:00"),
        ev("evt_627", "627. 针对被囚禁女性的人工智能", 57.86, 0.32, 0.33, "2026-11-16T12:03:00+00:00"),
        ev("evt_smart", "人工智能“智能”😢🥀", 0, 0.32, 0.32, "2026-11-16T22:04:00+00:00"),
        ev("evt_robot", "机器人与人工智能时代", 0, 0.31, 0.31, "2026-11-16T20:53:00+00:00"),
        ev("evt_machine", "人工智能——如果我们只是机器呢？", 65.26, 0.32, 0.20, "2026-11-16T18:53:00+00:00"),
        ev("evt_filler", "量子计算机新突破", 0, 0.30, 0.30, "2026-11-16T15:45:00+00:00"),
    ]


class TestGenericTitlePenalty(unittest.TestCase):
    def test_screenshot_order_without_penalty(self):
        """两个惩罚都关掉时，截图的 Top 10 顺序重现（填充行第 11）。"""
        now, batch = _screenshot_batch_1117()
        ranked = rank_events(
            batch, now=now, hashtag_penalty_max=0, generic_title_penalty_max=0
        )
        top10 = [e["event_id"] for e in ranked[:10]]
        self.assertEqual(
            top10,
            [
                "evt_delhi",
                "evt_agent",
                "evt_breaking",
                "evt_hokoon",
                "evt_iran",
                "evt_vargas",
                "evt_627",
                "evt_smart",
                "evt_robot",
                "evt_machine",
            ],
        )
        self.assertEqual(ranked[10]["event_id"], "evt_filler")

    def test_breaking_drops_out_of_top10(self):
        """默认通用惩罚（上限 15 分）：光秃秃的“突发新闻”24.9 → 约 9.9，
        掉出 Top 10；#1 不变；#4/#5/#6 带通用词但有实质内容，一分不扣。"""
        now, batch = _screenshot_batch_1117()
        ranked = rank_events(batch, now=now, hashtag_penalty_max=0)
        by_id = {e["event_id"]: e for e in ranked}
        plain = {
            e["event_id"]: e
            for e in rank_events(
                batch, now=now, hashtag_penalty_max=0, generic_title_penalty_max=0
            )
        }
        self.assertEqual(ranked[0]["event_id"], "evt_delhi")
        self.assertEqual(by_id["evt_delhi"]["rank_score"], 64.8)
        top10 = [e["event_id"] for e in ranked[:10]]
        self.assertNotIn("evt_breaking", top10)
        self.assertEqual(ranked[-1]["event_id"], "evt_breaking")
        self.assertAlmostEqual(by_id["evt_breaking"]["rank_score"], 9.9, delta=0.2)
        self.assertAlmostEqual(
            plain["evt_breaking"]["rank_score"]
            - by_id["evt_breaking"]["rank_score"],
            15.0,
            delta=0.2,
        )
        for eid in ("evt_hokoon", "evt_iran", "evt_vargas"):
            self.assertEqual(
                by_id[eid]["rank_score"], plain[eid]["rank_score"]
            )
            self.assertFalse(by_id[eid]["is_generic_title"])
        self.assertTrue(by_id["evt_breaking"]["is_generic_title"])
        self.assertIn("通用标题", by_id["evt_breaking"]["rank_reasons"])
        self.assertNotIn("通用标题", by_id["evt_hokoon"]["rank_reasons"])

    def test_generic_title_boundaries(self):
        """判定边界：去掉通用词后一个字母都不剩才算通用。"""
        from trend_radar.text_utils import is_generic_title

        generic = [
            "突发新闻",
            "突发新闻！！",
            "最新消息！！",
            "今日头条",
            "Breaking News",
            "breaking news",
            "LATEST NEWS",
            "JUST IN",
            "【突发新闻】",
            "",
            "突发新闻 1",
        ]
        for title in generic:
            self.assertTrue(is_generic_title(title), title)
        not_generic = [
            "突发新闻：赛义德·霍孔被捕",
            "[突发新闻] 伊朗反射炸弹对抗华盛顿目标？",
            "最新消息！！豪尔赫·阿尔弗雷多·巴尔加斯",
            "Breaking News: X",
            "JUST IN: 市场大跌",
            "突发新闻 A",
            "今日突发新闻|11月17日新闻 |巴巴·巴格什瓦尔",
            "人工智能——如果我们只是机器呢？",
        ]
        for title in not_generic:
            self.assertFalse(is_generic_title(title), title)

    def test_penalties_stack(self):
        """两个惩罚可叠加：既通用又 hashtag 重的标题，两项都扣（最低 0 分）。"""
        a = _ev("evt_plain", first_seen_hours_ago=1.0, name="普通事件")
        b = _ev(
            "evt_stacked",
            first_seen_hours_ago=1.0,
            name="突发新闻 #breaking #news #live",
        )
        ranked = {e["event_id"]: e for e in rank_events([a, b], now=NOW)}
        plain = {
            e["event_id"]: e
            for e in rank_events(
                [a, b], now=NOW, hashtag_penalty_max=0, generic_title_penalty_max=0
            )
        }
        stacked = ranked["evt_stacked"]
        self.assertTrue(stacked["is_generic_title"])
        self.assertAlmostEqual(stacked["hashtag_ratio"], 0.83, places=2)
        drop = plain["evt_stacked"]["rank_score"] - stacked["rank_score"]
        # hashtag 扣 30×0.83 ≈ 24.8，通用再扣 15，共约 39.8。
        self.assertAlmostEqual(drop, 30.0 * 0.83 + 15.0, delta=0.5)
        # 对照：只通用不带 hashtag 的标题，恰好扣 15 分。
        c = _ev("evt_generic_only", first_seen_hours_ago=1.0, name="突发新闻")
        only = {e["event_id"]: e for e in rank_events([a, c], now=NOW)}
        only_plain = {
            e["event_id"]: e
            for e in rank_events(
                [a, c], now=NOW, hashtag_penalty_max=0, generic_title_penalty_max=0
            )
        }
        self.assertAlmostEqual(
            only_plain["evt_generic_only"]["rank_score"]
            - only["evt_generic_only"]["rank_score"],
            15.0,
            delta=0.2,
        )

    def test_config_file_has_generic_penalty_key(self):
        """config.yaml 里有 ranking.generic_title_penalty_max（默认 15.0）。"""
        from trend_radar.event_ranking import load_ranking_config

        text = (Path(__file__).resolve().parent.parent / "config.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("generic_title_penalty_max", text)
        self.assertEqual(load_ranking_config()["generic_title_penalty_max"], 15.0)


if __name__ == "__main__":
    unittest.main()
