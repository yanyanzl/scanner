# P3：Telegram 实时提醒

- 日期：2026-11-15
- 状态：提醒模块已实现并测试，未接进调度器（接线见下文）。

## 这是什么

每轮采集检测到爆发事件后，给用户发一条 Telegram 消息。Bot API
免费，不引入新依赖（`requests` 已在 `requirements.txt` 里）。

## 设置步骤（只做一次）

1. 在 Telegram 里打开 **@BotFather**，发 `/newbot`，按提示起个名字
   和用户名（用户名必须以 `bot` 结尾，如 `trend_radar_bot`）。
   建好后 BotFather 会给一串 token，形如
   `123456789:AAE...`——这就是 `TELEGRAM_BOT_TOKEN`。
2. 在 Telegram 里找到刚建好的 bot，点 Start，随便发一条消息
   （如 `hi`）。这一步必须做，否则 bot 不能主动给你发消息。
3. 在浏览器打开（把 `<token>` 换成上一步的 token）：
   `https://api.telegram.org/bot<token>/getUpdates`
   在返回的 JSON 里找 `"chat":{"id": 987654321, ...}`，那个数字
   就是 `TELEGRAM_CHAT_ID`（是你和 bot 的私聊 id）。
4. 把两项写进项目根目录的本地 `.env`（和 `YOUTUBE_API_KEY` 同一个文件）：

   ```env
   TELEGRAM_BOT_TOKEN=123456789:AAE...
   TELEGRAM_CHAT_ID=987654321
   ```

   `.env` 已被 `.gitignore` 覆盖，绝不会进 GitHub。不要把 token
   发给别人、不要贴进群聊——拿到 token 的人能冒充你的 bot 发消息。
   如果 token 泄漏，在 @BotFather 里发 `/revoke` 重新生成。

## 消息格式

每条爆发事件一条消息，纯文本（不用 HTML 解析），大约 150 字：

```text
🚨 热点爆发提醒
事件：Kanak News 2PM Headlines
原因：增速突增、总分跃升
平台：rss,youtube（2 个）
增长：100.0 → 704.0/小时
总分：4.0 → 12.0
条数：3
事件 ID：evt_abc123
```

原因的中文对照：`growth_spike` = 增速突增，`score_surge` = 总分跃升，
`new_multi_platform` = 新事件首次跨平台（首次出现即跨平台，
没有上一轮可比，只显示当前值）。消息里不放 Dashboard 链接，
因为面板是本机 localhost，手机上打不开。

## 没配置时的行为

没有 `TELEGRAM_BOT_TOKEN` 或 `TELEGRAM_CHAT_ID` 时，`send_burst_alerts`
不发任何网络请求，记一条 `Telegram 未配置` 日志，返回
`{"sent": 0, "skipped": len(bursts), "errors": []}`。调度器照常跑，
和 Reddit / YouTube 没有凭证时一样。

一条发送失败（断网、401 token 错、403 被拉黑）不影响其他条，
失败记进 `errors` 并记 warning，函数本身不抛异常。token 绝不
出现在日志和返回的 errors 里。

## 接线（待接入 collect_once）

`trend_radar/collector.py` 里检测到 bursts 的位置（`counts["bursts"]`
赋值后），加：

```python
from trend_radar.alerts import send_burst_alerts

# … bursts = detect_bursts(conn, thresholds=cfg.get("burst")) 之后：
if bursts:
    alert_result = send_burst_alerts(bursts)
    counts["alerts_sent"] = alert_result["sent"]
    counts["alerts_failed"] = len(alert_result["errors"])
    if alert_result["errors"]:
        logger.warning("Telegram 提醒失败 %d 条: %s",
                       len(alert_result["errors"]), alert_result["errors"])
```

config.yaml 可选加一节（不加也行，模块只读环境变量）：

```yaml
telegram:
  enabled: true  # 无凭证时自动跳过；填好 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 后自动发
```

## 还没做的事

- 接线本身（上面那段）还没进 `collect_once`。
- 没有 Telegram 轮询 / 命令：bot 只发消息，不读消息、不回指令。
- 没有免打扰时段：半夜爆发也会推；后续可以在 `telegram:` 一节
  加 `quiet_hours`，接线时按时间跳过。
- 没有去重：如果同一事件连续多轮触发，会每轮都推一条；后续
  可以按 event_id + 检测时间做节流（例如同一事件 1 小时内只推一次）。
