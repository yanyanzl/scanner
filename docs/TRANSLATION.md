# 标题自动翻译（翻成中文）

## 为什么做

2026-11-15，一条马拉雅拉姆语 YouTube 新闻直播（"Today Breaking News LIVE |
ഇന്നത്തെ പ്രധാന വാർത്ത | Top Headlines Of The Hour"）因为标题里的英文
"breaking news" 撞上了 YouTube 查询词，被采进来，事件名原样出现在了
Telegram 爆发提醒里。扫描器之前对标题不做任何翻译。用户决定：标题
自动翻成中文再显示。

## 现在怎么工作（只翻优先内容）

翻译范围很窄——**只翻用户最可能看到的东西**，其他内容保持英文，
不做大批量翻译：

1. **Top 5 事件的名字**（综合排名前 5，见 `trend_radar/event_ranking.py`
   `rank_events`，和 Dashboard 的 Top 10 同一个排名）。
2. **触发 Telegram 爆发提醒的事件的名字**（本轮 `detect_bursts` 的列表）。
3. **各事件分数最高的成员标题**（每个目标事件只翻 1 条，分数最高的
   那条；事件名经常就是这条，同一文本只翻一次）。

- 入口：`trend_radar/translate.py`（`azure_translate_many` /
  `deepl_translate_many` / `needs_translation`）。
- 时机：每轮采集的**最后一步**——聚类、排名（Top 5）、爆发检测都
  完成后，**发 Telegram 提醒之前**才翻译。这样提醒当轮就能用上
  中文事件名。翻出的事件名同时写回内存里的 burst 列表（`name_zh`）
  和数据库（`events.name_zh`），成员标题写 `items.title_zh`。
- 频率：**Azure 每轮都跑**（2026-11-15 用户决定：每 15 分钟一轮
  就翻一次，不再每小时最多 1 次——翻译的意义就是每轮都能看到中文
  名）。用量保障靠两层硬上限（见下“额度算术”）：每轮 600 字符 +
  每月 1,800,000 字符。**DeepL 仍是每小时最多 1 次**（state 文件
  `data/.deepl_last_run`，和 YouTube/GDELT 同一套间隔门控），只在
  Azure 不可用时跑。没配 key 时不翻译。
- 优先级：事件名在前、成员标题在后——每轮 600 字符预算不够时先砍
  成员标题，Top 5 事件名、爆发事件名优先保住（提醒和榜单上先看到
  的都是事件名）。
- 月度用量计数：Azure 实际发送的字符数记在 `data/.azure_usage.json`
  （`{"month": "2026-11", "chars": N}`，UTC 月份，跨月归零；只记
  成功请求的字符，失败的不记；文件缺失或损坏时按 0 字处理并重建；
  写入用临时文件 + 改名，原子操作）。本月剩余额度不够时，本轮只翻
  剩余额度内的文本；用完时整轮跳过 Azure（记一条 info 日志）、改走
  DeepL。旧的 `data/.azure_last_run` 不再使用（留着也无害）。
- 缓存：翻译结果存在 SQLite 里，同一标题只翻一次。**失败的翻译不
  会写入**（下轮会再试）；中文文本直接记原文为译文，不调接口。
- 判断“需不需要翻”：`needs_translation()` 算 CJK 字占 CJK+拉丁字母
  的比例，>= 0.9 视为已是中文，不翻；空标题、纯数字/标点标题不翻；
  其他（英文、马拉雅拉姆语、中英混合）都翻。
- 显示：Dashboard（热点榜、增长最快、互动量曲线图例、爆发事件）和
  Telegram 提醒优先用中文标题/事件名，没有翻译时回退到原标题。
  聚类键（事件 ID、相似度）始终用**原始标题**算，翻译不影响聚类结果。
- 每轮日志里不再有旧的 `translated: 30`（那是已删除的 gtx 大批量
  翻译的尝试数，不是成功数）；现在是 `azure_translated` /
  `deepl_translated`（实际翻出的条数），看翻译是否工作要查数据库
  （`title_zh` / `name_zh` 是否有值）。

## 主力：Microsoft Azure AI Translator（已实现，2026-11-15）

Azure 是官方、有文档的翻译服务，免费 F0 层每月 200 万字符（按月
重置），是三家免费方案里额度最大的，所以做主力。

- **怎么调**：`POST https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&to=zh-Hans`，
  请求头 `Ocp-Apim-Subscription-Key: <key>`，区域资源还要加
  `Ocp-Apim-Subscription-Region: <region>`（和资源 Location 写的一样；
  global 资源不用加）。请求体是 JSON 数组，一次请求打包本轮全部文本：
  `[{"Text": "..."}, ...]`（单请求上限 50,000 字符，本项目每轮远不到
  这个数）。返回 `[{"translations": [{"text": "...", "to": "zh-Hans"}]}]`，
  按输入顺序一一对应。
- **不传 `from` 参数**：让 Azure 自动检测源语言——要翻的标题里有英文、
  马拉雅拉姆语、印地语、俄语等多种语言，强制 `from=en` 会翻错小语种标题。
- **失败时的行为**：429/403 时记一条 warning（**不断言是月额度还是
  频率限制**——响应里没说就不猜），本轮停止 Azure、改走 DeepL；
  401（key 不对）/网络错/返回异常同样记一条 warning 后改走 DeepL。
  key 只出现在请求头里，日志和错误信息里绝不出现。
- **超额会不会自动扣费**：F0 是免费层，没有绑定的付费方式就扣不了；
  第三方说法是超限只返回 429/403、等到下月恢复，**未找到微软官方
  逐字确认**，这里不把它当事实写。稳妥的做法是只在 Azure 门户里看
  Metrics 核对实际月字符数（见下）。

### 额度算术（按 24/7 运行的最坏情况，2026-11-15 用户定：每月 180 万字符以内）

| | 数字 |
| --- | --- |
| 每轮文本 | 约 10–15 条（5 个 Top 5 事件名 + 最多约 5 个爆发事件名，大多与 Top 5 重合 + 约 5 条成员标题） |
| 每轮字符 | 约 600–900 字（按每条约 60 字算），**每轮硬上限 600 字**（超出的留到下一轮） |
| 每天 | 600 字/轮 × 96 轮/天（15 分钟一轮）= 57,600 字 |
| 每月 | 57,600 × 30 天 = **1,728,000 字**；31 天的月份 57,600 × 31 = **1,785,600 字** |
| 用户上限（每月 1,800,000 字） | 30 天的月份余 72,000 字，31 天的月份余 14,400 字——**都够** |
| Azure F0（每月 2,000,000 字） | 月度上限 1,800,000 字是 F0 的 90%，留 200,000 字缓冲 |
| 月度硬上限（兜底） | `azure_monthly_cap_chars` 默认 1,800,000 字：`data/.azure_usage.json` 计数，用完本轮跳过 Azure——即使轮次比 15 分钟更密（手动 `--once` 多跑几轮），月度总量也烧不穿 |

对照：如果像已删除的 gtx 那样每 15 分钟翻 30 条标题，一个月约 690 万
字符，F0 一星期就烧完——所以每轮翻归每轮翻，范围必须窄（只翻 Top 5 /
爆发事件名 + 各事件 1 条成员标题），且 600 字/轮的硬上限不能松。
超额换算（万一哪天不够用）：手动切到 S1 后 $10/百万字符；月用 250 万
字符约 $5，300 万字符约 $10。

### 注册拿 key 的步骤

1. 打开 https://azure.microsoft.com/free 注册一个 **Azure 免费账号**
   （邮箱 + 电话验证；微软官方称需信用卡/借记卡验证，会有一笔 $1
   临时预授权，不扣费）。
2. 登录 [Azure 门户](https://portal.azure.com)，点 **创建资源**，
   搜索 **Translator**，点创建。
3. 选订阅、资源组、**区域**（Location，如 `global` 或 `westeurope`）、
   **定价层 F0**（免费，每月 200 万字符）。其他保持默认，点创建，等
   资源部署完成。
4. 进资源的 **Keys and Endpoint** 页，复制 **KEY 1**；记下 **Location**
   （`global` 就不用填 Region，区域资源才填）。
5. 打开扫描器目录下的 `.env`，加两行（没有 `.env` 就先把
   `.env.example` 复制为 `.env`）：
   ```
   AZURE_TRANSLATOR_KEY=你的KEY 1
   AZURE_TRANSLATOR_REGION=你的Location（global 资源留空）
   ```
6. 重启扫描器（`./启动扫描.sh`）。下一轮就是 Azure 轮次，日志里会
   出现 `Azure 本轮翻译优先文本 … 条（… 字符），本月累计 …/1800000 字符`；
   `counts` 里多一项 `azure_translated`。用量可以在 Azure 门户里资源的 **Metrics**
   页随时查看（按 `TotalTokenCalls` / 字符数核对），也可以直接看
   `data/.azure_usage.json` 里的当月累计。

## 备用：DeepL API

Azure 不可用（没配 key、429/403、请求失败、月度用量用完）时才走 DeepL，
同样只翻优先内容、同样每轮 1,200 字符硬上限，**但每小时最多 1 次**
（state 文件 `data/.deepl_last_run`）。为什么 DeepL 不跟 Azure 每轮跑：
它的免费额度是**一次性 100 万字符、不是按月重置**——每小时跑一次、
每轮拉满 1,200 字，一个月最多 864,000 字，约 5 周就烧完；每轮都跑
（96 轮/天）的话不到两周就没了。DeepL 只在 Azure 偶发失败时用，实际
用量远低于这个数。

- **怎么调**：`POST https://api-free.deepl.com/v2/translate`（免费 key）
  或 `https://api.deepl.com/v2/translate`（付费 key），表单参数
  `text` + `target_lang=ZH`（源语言自动检测），认证是请求头
  `Authorization: DeepL-Auth-Key <key>`。自 2025-11-01 起 `auth_key`
  查询参数/请求体参数已废弃，用它会返回 403——代码里不用它。
  key 按后缀选 host：以 `:fx` 结尾的免费 key 走 api-free，否则走
  api.deepl.com；发错 host 会 403，失败时标题留给下一轮。
- **失败时的行为**：401/403/429/网络错等只记 warning、该标题留给
  下一轮；**HTTP 456（免费额度用完）时记一条额度用完的 warning，
  本轮立即停止一切 DeepL 调用**，未翻的标题保持英文；失败的翻译
  不写 `title_zh`/`name_zh`。key 只出现在 Authorization 请求头里，
  日志和错误信息里绝不出现。
- **额度（2026-11-15 核实，DeepL 官网帮助中心）**：API Free 与 API Pro
  均已停售，新用户无法再买。**新注册只能选 Developer 计划：免费，
  一次性 100 万字符，用完不恢复**，之后要升级到付费 Growth 计划才能
  继续用。按优先内容的典型用量（DeepL 每轮约 600–900 字、每小时最多
  1 次），Developer 的 100 万字符约能用 1.5–2 个月；但 DeepL 只在 Azure
  不可用时跑，实际用量远低于这个数。最坏情况（Azure 长期不可用、DeepL
  每轮拉满 1,200 字）一个月最多 864,000 字，约 5 周烧完——所以 DeepL
  保持每小时 1 次，绝不跟 Azure 每轮跑。
- **注册拿 key 的步骤**：打开 https://www.deepl.com/pro-api（DeepL API
  定价页），选 **Developer** 计划（免费，不需要信用卡）；注册一个
  DeepL 账号；登录后进账号里的 **API Keys**（或 API Keys & Limits）页，
  复制 **Authentication Key**（免费 key 以 `:fx` 结尾）；写进 `.env`：
  `DEEPL_API_KEY=你的key`，重启扫描器。

## 已删除：Google 免费 gtx 端点

2026-11-15 深挖结论（报告见研究笔记）：`translate.googleapis.com/translate_a/single?client=gtx`
是网页版 Google 翻译的**非官方内部端点**，没有文档、没有 SLA、没有
公开限额。429 的主因是 **IP 声誉 + 程序化客户端识别**，不是没登录、
不是没 key、也不是发太快——证据：量极小（每分钟约 2 次请求）、0.5 秒
间隔、双 IP、第一次调用就 429；同一条网络用 curl 调全灭、用 Chromium
打开就 200。降速、换 client 参数、加 cookie/Referer 都是社区偏方，
没有靠谱的；登录 Google 账号、给该端点加 API key 也没用（它不接受
鉴权）。**结论：修不好，停止投入；调用代码与测试已一并删除。**
Google 官方 Cloud Translation API（每月前 50 万字符免费，但要建 GCP
项目、必须开 billing，超额 $20/百万字符）是备选，未接入——Azure 免费
量是它的 4 倍、超额单价是一半。

## 配置（config.yaml）

```yaml
translation:
  enabled: true     # false 可彻底关闭翻译
  target_lang: zh-CN
  azure_max_per_round: 15  # Azure 每轮最多翻的文本条数（每轮都跑）
  deepl_max_per_round: 15  # DeepL 每轮最多翻的文本条数（每小时最多 1 次，备用）
  azure_max_chars_per_round: 600  # Azure 每轮字符硬上限（600 × 96 轮/天 × 31 天 = 1,785,600 < 1,800,000）
  azure_monthly_cap_chars: 1800000  # Azure 月度字符上限（data/.azure_usage.json 计数，用完本轮跳过 Azure）
```

注意：配置里完全没有 `translation` 一节时默认关闭——这样旧的内联
配置（测试里大量使用）不会突然开始调翻译接口；正式的 config.yaml
里有该节，`enabled` 默认为 true。
