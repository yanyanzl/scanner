"""P3 Telegram 提醒：爆发事件推送到 Telegram（免费 Bot API）。

未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 时不发任何请求，
直接返回 skipped——调度器在没有 Telegram 的情况下照常工作，
和 Reddit / YouTube 没有凭证时的行为一致。

安全：token 只出现在发给 api.telegram.org 的 URL 里；日志和
返回的 errors 里绝不出现 token（出错信息里的 token 会被替换掉）。
"""
from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

REASON_WORDS = {
    "growth_spike": "增速突增",
    "score_surge": "总分跃升",
    "new_multi_platform": "新事件首次跨平台",
}

MAX_MESSAGE_CHARS = 500


def format_burst_message(burst: dict) -> str:
    """把一个 burst dict 写成一条中文提醒（纯文本，不用 HTML 解析）。

    不依赖网络，可单测。超过 500 字时截断并加省略号。
    """
    current = burst.get("current") or {}
    previous = burst.get("previous") or None
    reasons = [REASON_WORDS.get(r, r) for r in (burst.get("reasons") or [])]
    # 事件名优先用中文名（翻译功能写进 events.name_zh），没有时回退到原标题。
    display_name = (burst.get("name_zh") or burst.get("name") or "").strip()
    lines = [
        "🚨 热点爆发提醒",
        f"事件：{display_name}",
        f"原因：{'、'.join(reasons) if reasons else '爆发'}",
    ]
    platforms = (current.get("platforms") or "").strip()
    if platforms:
        lines.append(f"平台：{platforms}（{current.get('platform_count', 0)} 个）")
    growth_now = current.get("max_growth")
    score_now = current.get("total_score")
    if previous:
        lines.append(f"增长：{previous.get('max_growth')} → {growth_now}/小时")
        lines.append(f"总分：{previous.get('total_score')} → {score_now}")
    else:
        lines.append(f"增长：{growth_now}/小时")
        lines.append(f"总分：{score_now}")
    lines.append(f"条数：{current.get('item_count', 0)}")
    lines.append(f"事件 ID：{burst.get('event_id', '')}")
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    return text


def _sanitize(text: str, token: str) -> str:
    """从一段文字里去掉 token（防止异常信息里夹带请求 URL 时泄漏）。"""
    if token and token in text:
        return text.replace(token, "<token>")
    return text


def send_burst_alerts(
    bursts: list[dict] | None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    timeout: int = 15,
) -> dict:
    """给每个爆发事件发一条 Telegram 消息。

    token / chat_id 优先用参数，其次读环境变量 TELEGRAM_BOT_TOKEN /
    TELEGRAM_CHAT_ID。两者缺任何一个时不发任何请求，返回
    {"sent": 0, "skipped": len(bursts), "errors": []}。

    一条失败不影响其他条：失败记进 errors 并记 warning，函数本身
    不抛异常。返回 {"sent": n, "skipped": m, "errors": [...]}，
    其中 errors 每项是 {"event_id": ..., "error": ...}，error 里不含 token。
    """
    bursts = list(bursts or [])
    token = (bot_token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        if bursts:
            logger.info(
                "Telegram 未配置：未设置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，跳过 %d 条提醒",
                len(bursts),
            )
        return {"sent": 0, "skipped": len(bursts), "errors": []}

    url = f"{API_BASE}/bot{token}/sendMessage"
    sent = 0
    errors: list[dict] = []
    for burst in bursts:
        event_id = burst.get("event_id", "")
        try:
            resp = requests.post(
                url,
                json={"chat_id": chat, "text": format_burst_message(burst)},
                timeout=timeout,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Telegram 返回 HTTP {resp.status_code}")
            try:
                ok = resp.json().get("ok", True)
            except ValueError:
                ok = True
            if not ok:
                raise RuntimeError("Telegram 返回 ok=false")
        except Exception as exc:
            message = _sanitize(str(exc), token)
            logger.warning("Telegram 提醒发送失败（事件 %s）: %s", event_id, message)
            errors.append({"event_id": event_id, "error": message})
            continue
        sent += 1
        logger.info("Telegram 提醒已发送（事件 %s）", event_id)
    return {"sent": sent, "skipped": len(bursts) - sent - len(errors), "errors": errors}
