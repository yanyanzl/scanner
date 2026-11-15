# GDELT GKG：为什么没有接（标题字段不存在）

结论先说：**GKG 记录里没有文章标题字段，所以没法接。**
GKG 能告诉你“某篇文章提到了谁、在哪、什么主题、情绪如何”，
但给不出那篇文章的标题——没有标题就没法入库、没法聚类、没法显示，
这条路到此为止。

## 依据：官方 Codebook V2.1 的完整字段表

来源：GDELT 官方 GKG 2.1 Codebook
（http://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf，
2026-11-15 核实）。

GKG 2.1 一条记录的全部字段（按 Codebook 顺序）：

| 字段 | 内容 |
|---|---|
| GKGRECORDID | 记录唯一 ID（日期 + 序号） |
| DATE | 文章发布日期时间 |
| SourceCollectionIdentifier | 来源集合（1=WEB、2=引用、3=CORE、4=DTIC、5=JSTOR、6=非文本） |
| SourceCommonName | 来源域名（如 bbc.com）或“BBC Monitoring” |
| DocumentIdentifier | 文章唯一标识：WEB 来源时就是完整 URL |
| Counts / V2Counts | 文中提到的数量（死亡、抗议人数等） |
| Themes / V2Themes | 主题（300+ 个，如 TERRORISM、ECON_INFLATION） |
| Locations / V2Locations | 文中提到的地点 |
| Persons / V2Persons | 文中提到的人名 |
| Organizations / V2Organizations | 文中提到的机构名 |
| V2Tone | 情绪六维（整体 tone、正面、负面、极性、活跃度、代词密度、词数） |
| Dates | 文中提到的日期 |
| GCAM | 2,300+ 个内容分析维度（LIWC 等词典的得分） |
| SharingImage | 文章的分享图 URL |
| RelatedImages | 相关图片 URL 列表 |
| SocialImageEmbeds / SocialVideoEmbeds | 内嵌的社交媒体图片/视频 URL |
| Quotations | 文中引用的原话 |
| AllNames | 文中提到的所有专名（事件、法律、节日等） |
| Amounts | 文中提到的精确数字与对象 |
| TranslationInfo | 机器翻译来源语言与引擎 |
| Extras | 预留的 XML 块（目前只有学术文章的引用列表，新闻内容为空） |

**没有标题字段。**全表唯一出现 “Title” 的地方，是 Extras 里
`CITEDREFERENCESLIST` 块中的 `<Title>`——那是学术文章文末引用列表
里*被引用文献*的标题，不是这篇新闻本身的标题，对新闻内容为空。
之前“GKG 有标题”的说法是错的，以 Codebook 为准。

## GKG 能提供什么、不能提供什么

能提供：每篇文章的 URL、来源域名、发布日期、主题、人物、机构、
地点、情绪、GCAM 维度、引用原话、相关图片。适合做“某主题/某地/
某人被提到多少次”这种趋势统计。

不能提供：文章标题、文章正文、文章摘要。没有标题，扫描器没法把
一条 GKG 记录变成一个 Item（Item 必须有 title；聚类、评分、面板、
提醒全都依赖标题）。

## 为什么不绕过去

三条绕路都不走：

1. **用主题/人名拼一个“标题”**——那是编造内容，项目规则禁止
   假数据。
2. **拿 DocumentIdentifier 的 URL 去抓文章页取标题**——那是爬页面
   提取标题，项目规则禁止用爬取绕过访问限制。
3. **只存 URL 不存标题**——聚类、评分、Top 10、Telegram 提醒全都
   读标题，没标题的条目在系统里是不可用的。

## 如果以后还想用 GDELT

- GDELT Doc API（全文搜索，能返回标题）仍是唯一能给标题的 GDELT
  接口，但 2026-11-15 已因持续限流停用（见 config.yaml 注释）；
  恢复的前提是窄查询在浏览器里返回 JSON。
- GDELT BigQuery（`gdelt-bq:gdeltv2.gkg`）是同一份 GKG 数据，
  字段表相同，同样没有标题，换 BigQuery 也解决不了标题问题。
- NGrams 文件只有词频，连 URL 都没有，更不适合。

## 下载文件 1 小时清理规则

如果以后接了 GKG（或别的批量下载），文件下载到 `data/downloads/`，
每轮 `collect_once` 开始时删掉 mtime 超过 1 小时的文件（见
`trend_radar/downloads.py` 与 docs/NEWS_APIS.md）。现在没有下载，
这条规则是给以后准备的。
