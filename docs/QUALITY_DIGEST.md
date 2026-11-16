# 每日质量简报（2026-11-16）

每天早上 8 点，扫描器把昨日的质量摘要打进 Muse 对话，让 Cheese 收到后
分析、再优化。这是“结果 → 分析 → 优化”循环的机制——2026-11-16 用户
在 Dashboard 截图后问“结果出来你能不能分析再优化”，答案就是这个
简报：不用等用户截图，扫描器每天主动把结果送到 Cheese 面前。

## 内容

紧凑，约 1000 字符以内：

- 日期（昨日）。
- Top 5 事件（当前综合排名）：排名、中文名或原名、综合分、平台数、
  最大增长。
- 当日计数：事件数、爆发数、被抑制的重复出现数、Telegram 已发数、
  Muse 已发/失败数。
- 质量信号（Top 10 全量）：单平台事件数、hashtag 占比 ≥ 0.5 的
  事件数、recurrence 事件数。

注意口径：Top 5 是发送当时的当前排名，计数是昨日的轮次汇总，两者
口径不同，文本里已标明。如果扫描器昨天没跑，计数一栏写
“昨日无轮次记录”，Top 5 照常发。

## 标记

首行是独立的标记，与爆发提醒区分开：

【质量简报｜quality-digest v1｜date=<YYYY-MM-DD>】

`parse_scanner_marker` 不会把它认成爆发提醒（没有 `scanner-alert`
token）；识别用 `trend_radar/quality_digest.py` 里的
`parse_quality_digest` / `is_quality_digest`。版本管理与爆发提醒
同理：改格式时 `DIGEST_MARKER_VERSION` 升版。

## 调度与开关

- 调度：`run_scheduler.py` 的 `create_scheduler` 在 15 分钟采集任务
  之外再加一个 APScheduler cron 任务，默认每天 8 点（调度器本地时间）。
- 开关：`config.yaml` 的 `quality_digest:` 一节，`enabled: true`、
  `hour: 8`。`enabled: false` 关掉简报；`muse_chat` 没启用时也跳过
  （走同一条发送通道）。
- 失败只记 warning：发送失败、没数据、muse_chat 未启用时都不抛异常，
  绝不影响扫描轮次。

## 实现

- 内容构建是纯函数 `format_quality_digest(data)`（`trend_radar/quality_digest.py`），
  数据来自 `gather_digest_data`（接 `dashboard_data.load_top_events`，
  不直接拼 SQL；事件数来自聚类输出）；轮次计数由 `collect_once` 每轮
  末尾调用 `record_round_counts` 记到本地 `data/.quality_daily.json`
  （按本地日期分桶，只保留昨天和今天，原子写入，不进仓库）。
- 发送复用 `muse_chat.send_to_muse_chat`（CLI `--test` 发任意文本的
  同一条路），没有第二套 AppleScript / CDP。
- 测试：`tests/test_quality_digest.py`（标记、格式化、计数、配置、
  发送跳过）、`tests/test_scheduler.py`（任务存在、默认 8 点、关闭时
  无任务）。

## 已知局限

- Top 10 全是单平台是结构性问题（RSS 没互动量、RSS 与 YouTube 标题
  跨语言难聚到同一事件），简报里的“单平台事件数”就是用来追踪这个的；
  跨语言聚类是 1-2 周真实数据校准后的研究项，不在这批里做。
- 简报在 8 点发，汇总的是“昨天”；当天 0 点到 8 点的轮次会计入
  下一天的简报。
