# P2 准备：事件持久 ID + 爆发检测

- 日期：2026-11-15
- 状态：准备工作已完成，**预测本身尚未实现**。本文件只讲这次做了什么、怎么设计的、还缺什么。

## 为什么需要这一步

P1 的聚类是“每轮从头算”：同一事件在不同轮次里没有稳定身份，无法回答
“这个事件比上一轮涨了多少”“它是不是正在爆发”。P2 爆发预测的前提是
事件能跨轮次被认出来，并且每一轮的状态都被存下来。本次工作就是这两个
前提：持久事件 ID + 事件快照表 + 基于快照对比的爆发检测。

## 1. 事件 ID 方案

函数：`trend_radar/clustering.py::canonical_event_id(member_items)`，
结果出现在每个 `cluster_items()` / `cluster_from_db()` 返回的事件里，
字段名 `event_id`，格式 `evt_` + SHA-256 前 16 位十六进制。

**取法（按顺序）：**

1. 核心词 = 在至少 2 个成员标题里都出现的 token，去重、排序。
   如果核心词 ≥ 2 个，就用它。
2. 否则（单条事件、或成员措辞几乎没有重合）退回用代表标题
   （分数最高的成员）的全部 token，去重、排序。
3. 如果最后的 token 少于 3 个，追加最早成员 URL 的短 hash
   （`u:<sha1(url) 前 12 位>`）作区分。
4. 对 token 列表取 SHA-256。

**为什么这样定：**

- 不用代表标题本身：代表标题会随分数变化而换人，id 会跟着变；
  核心词是多个标题的交集，比单个标题稳定。
- 不用“最早出现时间 + 标题”等外部键：标题是事件唯一的跨平台
  内容标识，URL 是单条内容的标识，不是事件的。

**已知的局限（不要假装不存在）：**

- 措辞漂移会分裂事件：如果同一事件的新标题和旧标题没有 2 个共享
  token，核心词会变，id 随之变化，旧快照留在旧 id 下。这是标题
  聚类本身的局限，v0.2 的缓解方向是把正文首句也纳入向量、
  或用持久的事件合并表（见“还缺什么”）。
- 两个事件可能合并：如果两个不同事件的标题措辞高度重合，核心词
  相同，id 会撞车。SHA-256 碰撞本身可忽略；真正风险是措辞重合。
- 最早成员滑出窗口会改 id：短标题事件的 URL 区分项依赖最早那条；
  它滑出 24 小时窗口后，id 可能变化。
- 同名不同期的短标题（如 BBC "Tech Now"）：cluster_items 本来就不
  合并它们（短标题规则），URL 区分项保证它们拿到不同的 event_id，
  但代价是“同一节目不同期”会被当成不同事件——这是有意为之，
  宁可拆散，不要错误合并。

## 2. 数据库表

`trend_radar/db.py` 的 SCHEMA 里新增（`CREATE TABLE IF NOT EXISTS`，
老数据库第一次连接时自动建表，不用手动迁移）：

```sql
events (
    event_id TEXT PRIMARY KEY,
    name TEXT, first_seen TEXT, last_seen TEXT,
    platform_count INTEGER, item_count INTEGER,
    total_score REAL, max_score REAL, max_growth REAL
);
event_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL, collected_at TEXT NOT NULL,
    item_count INTEGER, platform_count INTEGER,
    total_score REAL, max_score REAL, max_growth REAL,
    platforms TEXT,  -- 逗号分隔，如 "rss,youtube"
    FOREIGN KEY (event_id) REFERENCES events (event_id)
);
```

写入：`trend_radar/clustering.py::persist_events(conn, events, now=None)`，
每调一次为每个事件做两件事：

- upsert `events` 行：`first_seen` 取历史最早值（SQL 的
  `min(first_seen, excluded.first_seen)`，绝不往后挪），`last_seen`
  和各项指标更新为本轮的值；
- 在 `event_snapshots` 里追加一行。

**已接进调度器（2026-11-15）：** `trend_radar/collector.py::collect_once`
在每轮采集入库后调 `cluster_from_db()` + `persist_events()`，再调
`detect_bursts()`，结果记进返回的 counts（`events` / `bursts`），
爆发事件同时写进日志。聚类/持久化失败只记 warning，不影响本轮
采集。接线测试见 `tests/test_collector_events.py`。Dashboard 的
“正在爆发的事件”一节读同一张表（`trend_radar/dashboard_data.py::load_bursts`）。

## 3. 爆发检测

模块：`trend_radar/burst.py`，`detect_bursts(conn, thresholds=None, now=None)`，
CLI：`python -m trend_radar.burst --db data/trends.sqlite3 [--json]`。

**规则（全部是初始假设，需用真实快照数据校准，和评分权重一样）：**

| 规则 | 条件（最新快照 vs 上一轮快照） | 默认阈值 |
| --- | --- | --- |
| `growth_spike` | max_growth 翻倍以上，且绝对增量达标 | ×2.0，增量 ≥ 100/小时 |
| `score_surge` | total_score 翻倍以上，且绝对增量达标 | ×2.0，增量 ≥ 5.0 |
| `new_multi_platform` | 只有一条快照（首次出现），且已跨 ≥ N 个平台；且条数 ≥ 10 或总分 ≥ 5.0（最低质量门槛，见下） | N = 2 |

**最低质量门槛（2026-11-15，只用于 `new_multi_platform`）：** 刚出现就
跨平台本身是信号，但 7 条内容、总分 1.88 的 YouTube Shorts（小丑
cosplay，2026-11-15 实测）也能凑出 2 个平台——它拿了爆发加分、发了
提醒、还冲到 Top 10 第 2 名。所以规则 (c) 再加一条：最新快照的
`item_count >= alert_min_items`（默认 10）**或** `total_score >=
alert_min_total_score`（默认 5.0），满足其一才算爆发；两个都不满足
时不产生 burst、不发提醒、Top 10 里没有爆发加分、Dashboard“正在爆发”
一节里也没有它。`growth_spike` / `score_surge` 自带绝对增量门槛
（翻倍 + 增量 ≥ 100/小时 / ≥ 5.0），不受这条影响。阈值是初始假设，
未校准，在 `config.yaml` 的 `burst:` 一节里改。

**重复出现（2026-11-15）：** 每小时直播这种标题（马拉雅拉姆语小时
直播，2026-11-15 实测）每小时换一个新事件 id、每次都拿爆发加分、
天天霸榜。`detect_bursts` 给每个 `new_multi_platform` burst 打
`is_recurrence` 标记：库里已有一个更早的同名多平台事件时为真——
更早 = `first_seen` 严格早于本事件、且落在最近
`recurrence_window_hours` 小时（默认 24）内；同名 = 原名归一化后
相同（小写、空白折叠）；多平台 = 那个事件最新快照的
`platform_count >= 2`。判断函数是 `trend_radar/burst.py::is_recurrence`
（collector 和 Dashboard Top 10 共用这一个入口，不各自再查库）。
重复出现的效果：(a) collector 不发 Telegram 提醒（记一条
“重复出现 … 个，不发提醒”日志，round 汇总里 `bursts_recurrence`
单独计数）；(b) Top 10 排名里爆发分量记 0、新鲜度分量记 0、原因里
不写“首次出现”（增长、成员强度、跨平台分量是真实的，照常算）。
轮内同名去重（同名爆发只发一条提醒）不受影响，重复出现是它的
跨轮次补充。不做标题黑名单（不硬编码“Top Headlines Of The Hour”
这类字符串），规则是通用的。代价：同一标题的真新事件在窗口期内
也会被压住；窗口就是调这个的旋钮。`growth_spike` / `score_surge`
是老事件的变化信号，不按重复出现处理。

阈值在 `config.yaml` 的 `burst:` 一节里：

```yaml
burst:
  growth_multiplier: 2.0
  growth_min_delta: 100.0
  score_multiplier: 2.0
  score_min_delta: 5.0
  new_event_min_platforms: 2
  alert_min_items: 10
  alert_min_total_score: 5.0
  recurrence_window_hours: 24
```

**为什么每个规则都有“绝对增量”兜底：** 只看倍数会被小基数骗过
（增长从 0 涨到 50 就是“无限倍”）。倍数 + 绝对增量两个条件
同时满足才算爆发。

**返回值：** burst 列表（按最新 total_score 降序），每项含
`event_id`、`name`、`reasons`（命中的规则名）、`is_recurrence`
（`new_multi_platform` 里同名事件在窗口内已跨平台出现过；其他
规则恒为 `False`）、`current`（最新快照值）、`previous`（上一轮
快照值，规则 (c) 时为 `None`）、`detected_at`。没有爆发时返回 `[]`。
只有一条快照的事件，规则 (a)(b) 无从比较，不触发；只有规则 (c)
会触发（还要过最低质量门槛）。

## 4. 测试

- `tests/test_events.py`（7 个）：同一事件两轮聚类保持同一 id；
  同名不同期短标题拿到不同 id；persist 两轮后 first_seen 不后移、
  快照各追加一行；`cluster_from_db` 的结果带稳定 event_id。
- `tests/test_burst.py`：增长尖峰、总分跃升、新事件跨平台
  均被检出；平坦历史、单快照单平台、小基数增长均不触发；另有
  最低质量门槛测试（7 条/1.88 分不触发，10 条或 5.0 分触发，
  边界与自定义阈值）与重复出现测试（同名 1 小时后出现判重复、
  25 小时后/改名/早先单平台不判重复、窗口关闭不判重复、
  growth_spike / score_surge 不受门槛影响）。
- `tests/test_collector_events.py`（1 个）：`collect_once` 不跑任何
  adapter 时，已入库内容仍被聚类持久化；第二轮同一 event_id、
  快照追加一行。
- `tests/test_dashboard_data.py` 里 `TestLoadBursts`（2 个）：
  缺 DB / 空 DB 返回 []，增长尖峰事件被 `load_bursts` 返回。
- 全套：`python3 -m unittest discover -s tests`（Dashboard 部分
  另见 `tests/test_dashboard_data.py` 的 10 个加载器测试）。

## 5. 还缺什么（完整 P2 预测尚未实现）

- **1–2 周真实快照**：爆发阈值现在是拍脑袋的初始假设；必须等
  Reddit / YouTube 的互动量跑出真实增长曲线后，用实际分布校准
  `growth_min_delta` / `score_min_delta`（目前的单位是“互动量/小时”，
  不同平台量级差很大，单一阈值可能要按平台拆）。
- **预测本身**：现在只有“检测已经发生的爆发”，没有“预测下一轮
  会不会爆发”。预测需要：事件级加速度序列（用快照算二阶差分）、
  跨平台扩散速度、以及基于历史的基线模型；这些都要等快照攒够。
- **事件合并/分裂处理**：见第 1 节局限；长期方案是维护一张
  `event_aliases`（旧 id -> 新 id）映射表，聚类漂移时人工或自动
  合并历史。
- **阈值校准**：聚类阈值 0.35 同样未校准，两个阈值要一起用真实
  多平台数据调，不要只调一个。
