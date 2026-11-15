# 标题自动翻译（翻成中文）

## 为什么做

2026-11-15，一条马拉雅拉姆语 YouTube 新闻直播（"Today Breaking News LIVE |
ഇന്നത്തെ പ്രധാന വാർത്ത | Top Headlines Of The Hour"）因为标题里的英文
"breaking news" 撞上了 YouTube 查询词，被采进来，事件名原样出现在了
Telegram 爆发提醒里。扫描器之前对标题不做任何翻译。用户决定：标题
自动翻成中文再显示。

## 怎么工作

- 入口：`trend_radar/translate.py`（`needs_translation` / `translate_text` /
  `translate_titles`）。
- 时机：每轮采集的**最后一步**——评分、入库、快照都完成后才翻译，
  翻译绝不拖慢或打断采集。先翻本轮新入库内容的标题（`items.title_zh`），
  再翻本轮事件的名字（`events.name_zh`，在爆发检测之前，这样提醒里
  才能用上中文名）。
- 缓存：翻译结果存在 SQLite 里，同一标题只翻一次；已入库的老内容
  也会在后续轮次里被陆续翻完（每轮有预算）。
- 预算：每轮最多调 `max_per_round` 次翻译接口（默认 30，事件名另有
  10 次上限）。中文标题直接记 `title_zh = 原标题`，不调接口、不占预算；
  相同标题只翻一次；超预算的留到下一轮。
- 显示：Dashboard（热点榜、增长最快、互动量曲线图例、爆发事件）和
  Telegram 提醒优先用中文标题/事件名，没有翻译时回退到原标题。
  聚类键（事件 ID、相似度）始终用**原始标题**算，翻译不影响聚类结果。
- 判断“需不需要翻”：`needs_translation()` 算 CJK 字占 CJK+拉丁字母
  的比例，>= 0.9 视为已是中文，不翻；空标题、纯数字/标点标题不翻；
  其他（英文、马拉雅拉姆语、中英混合）都翻。

## 免费接口与它的非官方身份

用的端点是 Google 免费翻译接口的非官方 `client=gtx` 形式：

```
GET https://translate.googleapis.com/translate_a/single
    ?client=gtx&sl=auto&tl=zh-CN&dt=t&q=<text>
```

- 无需 key、免费、不用注册。
- **非官方**：这是网页版 Google 翻译内部使用的端点，没有任何可用性
  保证，可能被限流或屏蔽。**实测状态（2026-11-15）：本机云服务器 IP
  上多轮冒烟测试全部返回 HTTP 429（0.5 秒间隔 + 浏览器 User-Agent
  也一样），用户家里宽带 IP 上 2026-11-15 18:00 的一轮 40 次尝试也
  全部 429——两边都不通，gtx 对用户已不可用。** 因为这个原因，
  DeepL 已接进来翻重要标题（见下），gtx 只作为大批量兜底保留。
- 失败时的行为：任何失败（网络错误、非 200、JSON 解析失败、返回里
  没有译文）都返回原标题、记一条 warning，不抛异常、不中断采集。
  **失败的翻译不会写入 title_zh**（下轮会再试）；如果端点被彻底
  屏蔽，扫描器会静默地回到“全部显示原标题”的状态，
  其他功能不受影响。
- 注意：每轮日志里的 `translated: 30` 是“尝试翻译的标题数”（每轮
  预算 30 次接口调用），**不是成功数**——全部 429 时它照样是 30，
  数据库里实际翻出来的标题是 0 条。看翻译是否工作要查数据库
  （`title_zh` 是否有值），不要看这个数字。

## DeepL：重要标题专用（已实现，2026-11-15）

gtx 两边都 429 之后，DeepL 是官方、有文档的替代。DeepL 按字符计费，
免费额度有限，所以它**不翻大批量**，只翻用户最可能看到的东西：

- **什么时候跑**：每小时最多 1 次 DeepL 轮次（state 文件
  `data/.deepl_last_run`，和 YouTube/GDELT 同一套间隔门控）。其他轮次
  只走 gtx。没配 `DEEPL_API_KEY` 时不记时间——填好 key 后下一轮立即
  就是 DeepL 轮次，不用等满 1 小时。
- **每轮翻什么**：先翻分数最高的 `deepl_max_per_round` 个还没有中文
  标题的内容标题（默认 1 个），再翻最多 10 个事件名。标题按分数排序
  选，不按时间——预算要花在榜单和提醒里最靠前的内容上。中文标题、
  已有中文名的事件不调接口、不占预算。
- **怎么调**：`POST https://api-free.deepl.com/v2/translate`（免费 key）
  或 `https://api.deepl.com/v2/translate`（付费 key），表单参数
  `text` + `target_lang=ZH`（源语言自动检测），认证是请求头
  `Authorization: DeepL-Auth-Key <key>`。自 2025-11-01 起 `auth_key`
  查询参数/请求体参数已废弃，用它会返回 403——代码里不用它。
  key 按后缀选 host：以 `:fx` 结尾的免费 key 走 api-free，否则走
  api.deepl.com；发错 host 会 403，失败时回退到 gtx。
- **失败时的行为**：401/403/429/网络错等只记 warning、该标题留给
  gtx 或下一轮；**HTTP 456（免费额度用完）时记一条额度用完的 warning，
  本轮立即停止一切 DeepL 调用**，未翻的标题保持英文；失败的翻译
  不写 `title_zh`/`name_zh`。key 只出现在 Authorization 请求头里，
  日志和错误信息里绝不出现。

### 额度算术（按 24/7 运行的最坏情况）

| 计划 | 额度 | 满负荷用量 | 结论 |
| --- | --- | --- | --- |
| 旧版 API Free（已停售，老用户才有） | 每月 500,000 字符 | 每轮 11 条 × 约 60 字 × 24 轮/天 × 30 天 = 475,200 字/月 | 够用，余约 5% |
| **Developer 计划（新注册只能选这个）** | **一次性 1,000,000 字符，用完不恢复** | 同上，每月最多 475,200 字 | **满负荷约 63 天用完**；实际远低于满负荷（电脑睡眠、很多轮次没有 10 个待翻事件名、gtx 偶尔能通），一般能用几个月 |

**重要（2026-11-15 核实，DeepL 官网帮助中心）**：API Free 与 API Pro
均已停售，新用户无法再买。**新注册只能选 Developer 计划：免费，
一次性 100 万字符，用完不恢复**，之后要升级到付费 Growth 计划才能
继续用。也就是说 DeepL 对新用户不是“每月都有的免费额度”，而是
“一共 100 万字符的免费试用量”。代码按这个口径设计：每轮字符硬上限
660 字（`DEEPL_MAX_CHARS_PER_ROUND`），即使 31 天、每轮拉满也最多
660 × 24 × 31 = 491,040 字，单月烧不穿旧版月额度；Developer 计划下
最坏情况约 63 天烧完 100 万字符，实际使用一般远低于这个速度。
额度用完时 456 的 warning 会告诉你，之后标题保持英文，其他功能不受影响。

### 注册拿 key 的步骤

1. 打开 https://www.deepl.com/pro-api（DeepL API 定价页），选
   **Developer** 计划（免费）。
2. 注册一个 DeepL 账号（邮箱 + 密码；这是单独的 API 账号）。
   Developer 计划注册不需要信用卡。
3. 登录后进账号里的 **API Keys**（或 API Keys & Limits）页，复制
   **Authentication Key**。免费 key 以 `:fx` 结尾。
4. 打开扫描器目录下的 `.env`，加一行（没有 `.env` 就先把
   `.env.example` 复制为 `.env`）：
   ```
   DEEPL_API_KEY=你的key
   ```
5. 重启扫描器（`./启动扫描.sh`）。下一轮就是 DeepL 轮次，日志里会
   出现 `DeepL 本轮翻译标题/事件名 … 个`；`counts` 里多一项
   `deepl_translated`。用量可以在 DeepL 账号的 API Keys & Limits
   页随时查看。

## 配置（config.yaml）

```yaml
translation:
  enabled: true     # false 可彻底关闭翻译
  target_lang: zh-CN
  max_per_round: 30  # 每轮最多调 30 次翻译接口（事件名另有 10 次上限）
  delay_seconds: 0.5  # 两次接口调用之间等待 0.5 秒，免费接口对连续请求敏感
  deepl_max_per_round: 1  # DeepL 轮次（每小时最多 1 次）里翻几个分数最高的标题
```

注意：配置里完全没有 `translation` 一节时默认关闭——这样旧的内联
配置（测试里大量使用）不会突然开始调翻译接口；正式的 config.yaml
里有该节，`enabled` 默认为 true。

## 其他备选（未实现）

- LibreTranslate 公共实例（不稳定）、MyMemory 翻译记忆库 API
  （免费额度小）。都不如 DeepL 免费版可靠。
- 付费翻译服务（Google Cloud Translation、Azure 等）按项目免费优先
  原则，只有在没有任何免费方案可用时才考虑。
