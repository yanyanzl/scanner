"""P1 v0.1 跨平台事件聚类：本地 TF-IDF + 余弦相似度 + 并查集。

纯标准库，不加新依赖，不调付费 LLM，不联网。
设计与局限见 docs/P1_CLUSTERING.md。

同一事件在多个平台出现时，识别为一个事件，而不是多条独立信息。
只看标题（不看正文），只聚时间窗口内的内容。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trend_radar.scoring import parse_dt

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_THRESHOLD = 0.35
DEFAULT_MIN_PLATFORMS = 1
DEFAULT_TIME_WINDOW_HOURS = 24
MAX_ITEMS = 200

EN_STOPWORDS = frozenset(
    """
    a an the and or of to in on for with by at from as is are was were be been
    it its this that these those there here what when who which will would can
    could should has have had not no new after before over under about into
    more most other some any all each per via vs says said say says
    """.split()
)

ZH_STOPWORDS = frozenset("的了是在和与或这那有也都就为以于对把被个们及等之而若但")


def tokenize(title: str) -> list[str]:
    """标题分词：英文按词、中文按字，去停用词。

    中文不用二字（bigram）：短标题下 bigram 太稀疏，同一事件的不同措辞
    几乎没有重合的二字；按字切分时共享字（苹果 / 新 / 芯片里的字）才能
    体现同一事件。代价是单字区分度低，由相似度规则里的"至少共享 2 个
    token"来兜底（见 cluster_items）。
    """
    if not title:
        return []
    text = title.lower()
    tokens: list[str] = []
    for word in re.findall(r"[a-z0-9]+", text):
        if len(word) >= 2 and word not in EN_STOPWORDS:
            tokens.append(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(ch for ch in run if ch not in ZH_STOPWORDS)
    return tokens


def _tfidf_vectors(docs: list[list[str]]) -> list[dict[str, float]]:
    """TF-IDF 向量（已归一化为单位向量）。"""
    n = len(docs)
    df: Counter = Counter()
    for tokens in docs:
        for tok in set(tokens):
            df[tok] += 1
    vectors: list[dict[str, float]] = []
    for tokens in docs:
        counts = Counter(tokens)
        vec: dict[str, float] = {}
        for tok, count in counts.items():
            idf = math.log((n + 1) / (df[tok] + 1)) + 1.0
            vec[tok] = (1.0 + math.log(count)) * idf
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            vec = {tok: v / norm for tok, v in vec.items()}
        vectors.append(vec)
    return vectors


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(tok, 0.0) for tok, v in a.items())


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _item_time(item: dict) -> datetime | None:
    return parse_dt(item.get("first_seen")) or parse_dt(item.get("published_at"))


def _normalised_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (title or "").lower())


def cluster_items(
    items: list[dict],
    similarity_threshold: float = DEFAULT_THRESHOLD,
    min_platforms: int = DEFAULT_MIN_PLATFORMS,
) -> list[dict]:
    """把 items 按标题相似度聚成事件列表。

    每个事件：name（分数最高的成员标题）、first_seen、platforms、
    platform_count、item_count、total_score、max_score、max_growth、trend、items。
    空输入返回 []。不改动输入。
    """
    items = [it for it in items if (it.get("title") or "").strip()]
    if not items:
        return []
    docs = [tokenize(it.get("title", "")) for it in items]
    vectors = _tfidf_vectors(docs)
    uf = _UnionFind(len(items))
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if not docs[i] or not docs[j]:
                continue
            # 标题太短（少于 3 个有效 token）时不合并：实测 BBC 的 "Tech Now"
            # 是同名不同期的节目，不同 URL 却标题完全相同，按标题合并会误并。
            # 短标题宁可拆散（显示为多个单条事件），也不要错误合并。
            if min(len(set(docs[i])), len(set(docs[j]))) < 3:
                continue
            if _normalised_title(items[i].get("title", "")) == _normalised_title(
                items[j].get("title", "")
            ):
                uf.union(i, j)
                continue
            # 相似度取 TF-IDF 余弦与 token 重合系数（overlap coefficient）的大者：
            # 短标题的 TF-IDF 余弦天然偏低（分母被大量只出现一次的词撑大），
            # 只用余弦会把同一事件拆散。重合系数要求至少共享 2 个 token，
            # 防止 "Apple iPhone" 与 "Apple iPad" 只因共享 "apple" 就被误并。
            set_i, set_j = set(docs[i]), set(docs[j])
            shared = len(set_i & set_j)
            overlap = shared / min(len(set_i), len(set_j)) if min(len(set_i), len(set_j)) else 0.0
            if _cosine(vectors[i], vectors[j]) >= similarity_threshold or (
                overlap >= similarity_threshold and shared >= 2
            ):
                uf.union(i, j)
    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(items)):
        groups[uf.find(idx)].append(idx)

    events: list[dict] = []
    for members in groups.values():
        member_items = [items[i] for i in members]
        platforms = sorted({it.get("platform", "") for it in member_items if it.get("platform")})
        if len(platforms) < max(1, int(min_platforms)):
            continue
        representative = max(
            member_items, key=lambda it: (float(it.get("score") or 0), float(it.get("growth") or 0))
        )
        times = [t for t in (_item_time(it) for it in member_items) if t is not None]
        total_score = round(sum(float(it.get("score") or 0) for it in member_items), 4)
        max_score = round(max(float(it.get("score") or 0) for it in member_items), 4)
        max_growth = round(max(float(it.get("growth") or 0) for it in member_items), 2)
        events.append(
            {
                "name": representative.get("title", ""),
                "first_seen": min(times).isoformat() if times else None,
                "platforms": platforms,
                "platform_count": len(platforms),
                "item_count": len(member_items),
                "total_score": total_score,
                "max_score": max_score,
                "max_growth": max_growth,
                "trend": "rising" if max_growth > 0 else "flat",
                "items": [
                    {
                        "title": it.get("title", ""),
                        "platform": it.get("platform", ""),
                        "url": it.get("url", ""),
                        "score": float(it.get("score") or 0),
                        "growth": float(it.get("growth") or 0),
                    }
                    for it in sorted(
                        member_items, key=lambda it: float(it.get("score") or 0), reverse=True
                    )
                ],
            }
        )
    events.sort(key=lambda e: (e["platform_count"], e["total_score"], e["max_score"]), reverse=True)
    return events


def _load_config_defaults() -> dict:
    """读 config.yaml 的 clustering 一节；读不到就用默认（不依赖 yaml 是否安装）。"""
    defaults = {
        "similarity_threshold": DEFAULT_THRESHOLD,
        "min_platforms": DEFAULT_MIN_PLATFORMS,
        "time_window_hours": DEFAULT_TIME_WINDOW_HOURS,
    }
    try:
        import yaml  # type: ignore

        cfg = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        for key in defaults:
            if key in (cfg.get("clustering") or {}):
                defaults[key] = cfg["clustering"][key]
    except Exception:
        pass
    return defaults


def cluster_from_db(
    db_path: str | Path,
    hours: int | None = None,
    similarity_threshold: float | None = None,
    min_platforms: int | None = None,
    limit: int = MAX_ITEMS,
) -> list[dict]:
    """读 SQLite（items 表）里最近窗口内的内容并聚类。DB 不存在返回 []。"""
    path = Path(db_path)
    if not path.exists():
        return []
    cfg = _load_config_defaults()
    hours = DEFAULT_TIME_WINDOW_HOURS if hours is None else hours
    similarity_threshold = (
        float(cfg["similarity_threshold"]) if similarity_threshold is None else similarity_threshold
    )
    min_platforms = int(cfg["min_platforms"]) if min_platforms is None else min_platforms
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(hours))
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM items ORDER BY score DESC, growth DESC LIMIT ?", (int(limit),)
        ).fetchall()
    finally:
        conn.close()
    items = [dict(r) for r in rows]
    recent: list[dict] = []
    for it in items:
        t = parse_dt(it.get("collected_at")) or _item_time(it)
        if t is None or t >= cutoff:
            recent.append(it)
    return cluster_items(recent, similarity_threshold=similarity_threshold, min_platforms=min_platforms)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P1 v0.1: cluster recent items into events.")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "trends.sqlite3"))
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--min-platforms", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="print events as JSON")
    args = parser.parse_args(argv)
    hours = args.hours if args.hours is not None else _load_config_defaults()["time_window_hours"]
    events = cluster_from_db(
        args.db,
        hours=args.hours,
        similarity_threshold=args.threshold,
        min_platforms=args.min_platforms,
    )
    if args.json:
        print(json.dumps(events, ensure_ascii=False, indent=2))
    elif not events:
        print("暂无事件：数据库为空，或窗口内没有可聚类的内容。")
    else:
        print(f"事件 {len(events)} 个（最近 {hours:g} 小时）：")
        for i, ev in enumerate(events, 1):
            print(
                f"{i}. [{ev['platform_count']} 平台 | {ev['item_count']} 条 | "
                f"总分 {ev['total_score']} | 最高分 {ev['max_score']} | "
                f"最大增长 {ev['max_growth']}/小时 | {ev['trend']}] {ev['name']}"
            )
            print(f"   平台：{', '.join(ev['platforms'])} | 首次出现：{ev['first_seen']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
