# 免费新闻 API（第三类来源）

2026-11-15 新增：4 个免费新闻 API。查询词全部来自 config.yaml 的
`topics`（默认 5 个）；每个来源单独限频（state 文件 `data/.<name>_last_run`），
间隔按免费额度算出；无 key 时 adapter 直接返回 `[]`、不发请求。

> **全部未实测**：没有 key，四个接口都没调过真实请求。用户填好 key 后，
> 看前几轮的日志（`NewsAPI: 采集到 N 条` / `未启用` / `采集失败`）和
> 入库数量确认。key 只放本地 `.env`，绝不进仓库。

## 一览

| 来源 | 免费额度（已核实） | 文章延迟 | key | 每轮调用 | 间隔 | 每天用量/上限 |
|---|---|---|---|---|---|---|
| NewsAPI.org | 100 次/天 | 约 24 小时 | NEWSAPI_KEY | 5 | 90 分钟 | 80 / 100 |
| Guardian | 500 次/天、1 次/秒 | 无 | GUARDIAN_API_KEY | 5 | 15 分钟 | 480 / 500 |
| NYT Article Search | 500 次/天、5 次/分钟 | 无 | NYT_API_KEY | 5 | 15 分钟 | 480 / 500 |
| NewsData.io | 200 credits/天 | 12 小时 | NEWSDATA_API_KEY | 5 | 45 分钟 | 160 / 200 |

每天用量 = (1440 / 间隔分钟) × 5 个查询。四个来源都不带互动量数据
（engagement 恒为 0），只补文章广度与跨平台信号；增长仍靠
YouTube / Reddit。

## NewsAPI.org

- 免费额度：**100 次请求/天**。来源：官网 newsapi.org/pricing
  （2026-11-15 核实；官网 FAQ 确认 Developer 计划仅限开发测试环境；
  100 次/天与 24 小时延迟见官网定价页的多方引用，如 apicostcalc
  2026-11-14 对官网的核查、1lookup 2026-10 的核查）。
- 限制：文章**延迟约 24 小时**，只查最近 1 个月；内容截断（只有标题 +
  描述，没有全文）；**仅限开发测试环境使用**——官网 FAQ 原话：
  Developer 计划不能用于 staging 或生产环境（包括内部使用）。
  这个雷达是长期运行的个人工具，严格说不属于“开发环境”：
  用它之前请知晓这一条，接受就用，不接受就跳过这个来源。
- 信用卡：不需要。试用期：不是试用，是长期免费的 Developer 计划。
- 注册：https://newsapi.org/register 拿 key，填进 `.env` 的 `NEWSAPI_KEY`。
- 取什么：`/v2/everything`，每个查询词 `sortBy=publishedAt`、20 条。
  `[Removed]` 占位标题（已下架文章）会被跳过。
- 间隔算术：100 ÷ 5 = 每天最多 20 轮 → 间隔 ≥ 72 分钟；取 90 分钟，
  每天 16 轮 × 5 = 80 次，余 20。

## Guardian Open Platform

- 免费额度：**500 次请求/天、1 次/秒**，非商业用途。来源：2026-10-22
  对官网（open-platform.theguardian.com）的核查与多方 2026 年资料一致。
  **注意一个冲突**：旧文档（2019 年的 CRAN 包文档等）写的是 5,000 次/天、
  12 次/秒；2026 年的多方资料（包括 2026-10-22 对官网的核查）写的是
  500 次/天、1 次/秒。本项目按保守的 500 次/天使用——两种说法下都
  不会超限。官网页面因域名策略无法直接拉取复核，数字以 2026-10-22
  的官网核查为准；如果用户注册后在账户页看到不同数字，以账户页为准。
- 限制：无文章延迟；可读全文（本项目只取标题 + 链接 + 发布时间，
  不取正文）；非商业用途。
- 信用卡：不需要。试用期：不是试用，是长期免费的 Developer key。
- 注册：https://open-platform.theguardian.com/access 注册拿 key，
  填进 `.env` 的 `GUARDIAN_API_KEY`。
- 取什么：`content.guardianapis.com/search`，每个查询词
  `order-by=newest`、20 条。
- 间隔算术：500 ÷ 5 = 每天最多 100 轮 → 间隔 ≥ 14.4 分钟；取 15 分钟，
  每天 96 轮 × 5 = 480 次，余 20。

## NYT Article Search API

- 免费额度：**每个 API 每天 500 次、每分钟 5 次**，非商业用途。
  来源：官网 developer.nytimes.com FAQ #11（多方 2026 年资料一致，
  如 apidog 2026-10-22 对官网的核查；另有一个 4,000 次/天的说法
  只出现在低质量转载里，与 FAQ 引用冲突，不采用）。
- 限制：无文章延迟；只返回标题 + 摘要 + 链接，没有全文；
  每页 10 条，本项目只取第 1 页；**两次调用之间至少隔 12 秒**
  （官网 FAQ 的要求，`adapters/nyt.py` 里已实现）。
- 信用卡：不需要。试用期：不是试用，是长期免费 key。
- 注册：https://developer.nytimes.com 注册应用、启用 Article Search API、
  拿 key，填进 `.env` 的 `NYT_API_KEY`。注意：只注册不够，必须在
  应用里把 Article Search 这个 API 启用，否则返回 401。
- 取什么：`/svc/search/v2/articlesearch.json`，每个查询词
  `sort=newest`、第 1 页 10 条。
- 间隔算术：500 ÷ 5 = 每天最多 100 轮 → 间隔 ≥ 14.4 分钟；取 15 分钟，
  每天 96 轮 × 5 = 480 次，余 20。

## NewsData.io

- 免费额度：**每天 200 credits**。来源：官网 newsdata.io/pricing
  （2026-11-15 直接核实）：Free $0，200 API credits per day，
  过去 48 小时内容，**文章延迟 12 小时**，1–10 篇/credit，
  “No cost, instant access”。免费档限速 30 次/15 分钟
  （官网 llms-full.txt）。
- 限制：文章延迟 12 小时；没有 archive、count 端口、全文与 AI 摘要；
  关键词搜索限 100 字符（本项目查询词都远短于这个）；过去 48 小时。
- 信用卡：不需要（官网资料：no credit or debit card required）。
  试用期：不是试用，免费计划是长期有效的（官网：lifetime access）。
- 注册：https://newsdata.io 注册拿 key，填进 `.env` 的 `NEWSDATA_API_KEY`。
- 取什么：`/api/1/latest`，每次调用 1 credit，每个查询词最多 10 篇。
- 间隔算术：200 ÷ 5 = 每天最多 40 轮 → 间隔 ≥ 36 分钟；取 45 分钟，
  每天 32 轮 × 5 = 160 credits，余 40。

## 失败时的行为

和 Reddit / YouTube 没有凭证时一样：无 key 返回 `[]`、记一条
“未启用”，不发任何请求；429 / 401 / 网络错 / 坏 JSON 都只记一条
warning、跳过这个查询，不中断本轮；单个来源异常不会影响其他来源。

## 下载文件自动清理（1 小时规则）

扫描器下载的任何文件都放在 `data/downloads/`（已在 `.gitignore` 的
`data/*` 范围内，不会进仓库）。每轮 `collect_once` 开始时跑一次清理：
删掉该目录下 mtime 超过 1 小时（3600 秒）的文件。只扫这一个目录，
不递归进子目录，绝不碰别的地方；目录不存在时直接跳过。
现在还没有任何下载（GDELT GKG 没接，见 docs/GKG.md），这条规则是
给以后的批量下载准备的。实现见 `trend_radar/downloads.py`，
测试见 `tests/test_downloads.py`。
