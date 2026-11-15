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
  上三轮冒烟测试全部返回 HTTP 429（0.5 秒间隔 + 浏览器 User-Agent
  也一样），一条中文都没翻出来——云机房出口 IP 很可能被该端点按
  IP 信誉直接限流。家用宽带 IP 通常能正常用这个端点，真正的验证
  需要在用户 Mac 上跑第一轮后看日志和数据库。**
- 失败时的行为：任何失败（网络错误、非 200、JSON 解析失败、返回里
  没有译文）都返回原标题、记一条 warning，不抛异常、不中断采集。
  **失败的翻译不会写入 title_zh**（下轮会再试）；如果端点被彻底
  屏蔽，扫描器会静默地回到“全部显示原标题”的状态，
  其他功能不受影响。

## 配置（config.yaml）

```yaml
translation:
  enabled: true     # false 可彻底关闭翻译
  target_lang: zh-CN
  max_per_round: 30  # 每轮最多调 30 次翻译接口（事件名另有 10 次上限）
  delay_seconds: 0.5  # 两次接口调用之间等待 0.5 秒，免费接口对连续请求敏感
```

注意：配置里完全没有 `translation` 一节时默认关闭——这样旧的内联
配置（测试里大量使用）不会突然开始调翻译接口；正式的 config.yaml
里有该节，`enabled` 默认为 true。

## 如果 Google 屏蔽了这个端点（备选，未实现）

- **DeepL 免费 API**：需要注册拿一个免费 key（每月 500,000 字符），
  官方支持、有文档，限额内免费。实现时把 `translate_text` 的后端
  换成 `https://api-free.deepl.com/v2/translate`，key 放 `.env`
  （`DEEPL_API_KEY`），其余逻辑（缓存、预算、回退）不用动。
- 其他免费路线：LibreTranslate 公共实例（不稳定）、MyMemory
  翻译记忆库 API（免费额度小）。都不如 DeepL 免费版可靠。
- 付费翻译服务（Google Cloud Translation、Azure 等）按项目免费优先
  原则，只有在没有任何免费方案可用时才考虑。
