# 搜索主题（topics）

搜索关键词集中在 config.yaml 的 `topics` 一节，所有需要关键词的
来源共用。改主题不用改代码：改配置，重启扫描器即可。

## 默认主题与选词理由

| 类别 | 查询词 | 为什么是这个词 |
|---|---|---|
| 突发最热 | `breaking news` | 不依赖任何具体事件，抓当前最热的突发新闻；配合 Google News 头条 feed（不需要关键词）一起覆盖“现在最热” |
| 经济金融 | `stock market OR inflation OR interest rates` | 三个词各代表一个经济主线：市场、通胀、利率；OR 连起来一次查询覆盖三个方向，省调用次数 |
| 科技 | `technology` | 通用科技词，覆盖面最广；Google News 科技搜索 feed 与 TechCrunch / Verge / Hacker News RSS 覆盖科技。YouTube 已不再用它（2026-11-15 改为 `artificial intelligence`，见下） |
| AI | `artificial intelligence OR AI` | 全称 + 缩写都要：只写 AI 会漏掉写全称的文章，只写全称会漏掉只写 AI 的 |
| 全球政治与冲突 | `war OR geopolitics` | 冲突与地缘政治各一个词；war 覆盖战争冲突报道，geopolitics 覆盖地缘博弈分析 |

全部是**英文关键词**：所有来源（Google/Bing/新闻 API）都是英文内容，
中文关键词匹配不到任何东西。中文标题靠翻译模块解决（见
docs/TRANSLATION.md），不是靠中文查询词。

## 哪些来源用主题

- **Google News RSS**：头条 feed 1 个（不需要关键词）+ 每个主题 1 个
  搜索 feed，共 6 个 feed。
- **Bing News RSS**：每个主题 1 个搜索 feed，共 5 个。
- **免费新闻 API**（NewsAPI / Guardian / NYT / NewsData）：每个主题调
  一次接口，每轮共 5 次调用。间隔按免费额度算出，见 docs/NEWS_APIS.md。
- **GDELT**（停用中）：重开时查询词由主题拼成，例如
  `breaking news OR (stock market OR inflation OR interest rates) OR
  technology OR (artificial intelligence OR AI) OR (war OR geopolitics)`。
  含 OR 的主题加括号，避免和外层的 OR 混在一起。

## 哪些来源不受主题影响

- **出版商 RSS feed**（BBC、半岛电视台、Hacker News、TechCrunch、
  The Verge）：配置里写死的地址，采什么由媒体自己决定，和主题无关。
- **YouTube**：搜索桶 100 次/天，**最多 2 个查询词**，不走 topics。
  目前是 `["breaking news", "artificial intelligence"]`，见 config.yaml。
  2026-11-15 用户决定把 `technology` 换成 `artificial intelligence`：
  AI 是明确的优先方向，纯 AI 视频的标题不一定带 technology 这个词；
  通用科技内容仍由 TechCrunch / Verge / Hacker News RSS 与 Google News
  科技搜索 feed 覆盖。注意这里用精确短语 `artificial intelligence`，
  不用 `artificial intelligence OR AI`——YouTube 搜索不支持 OR 语法，
  OR 写法只适合 Google/Bing 等支持 OR 的来源。2 词 × 48 轮/天 = 96 次
  搜索/天，对 100 次/天的搜索桶留 4 次缓冲，和换词前一样。

## 怎么改主题

1. 打开 config.yaml，找到 `topics:` 一节。
2. 改查询词（增、删、改类别都可以；值可以是字符串或字符串列表）。
3. 重启扫描器（`./启动扫描.sh` 或 `启动扫描.bat`）。

注意两点：

- 查询词越多，每个新闻 API 每轮的调用次数就越多，免费额度吃得越快。
  5 个查询词时的间隔是按额度算好的（见 docs/NEWS_APIS.md）；加到
  6 个以上请重新算一遍间隔，或者接受超限后 429。
- Google/Bing 的搜索 feed 是每轮都拉的（RSS 没有额度），主题越多，
  每轮拉的 feed 越多、入库的条目也越多；目前 5 个主题是 11 个聚合
  feed，入库量在可接受范围。
