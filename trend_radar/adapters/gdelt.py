"""GDELT 采集：全球新闻事件数据库公开 API，无需凭证。

限流说明（2026-11-15 实测）：
- GDELT 连续 3 次失败：两次 HTTP 429，一次返回 200 但正文不是 JSON，
  而是纯文本 "Please limit requests to one every 5 seconds …"，
  这正是 10:58 那次 "Expecting value: line 1 column 1" 的原因——
  被限流时接口返回限流提示文本而不是 JSON。
- 我们的调用频率（每 15 分钟 1 次）远低于它说的每 5 秒 1 次，
  仍被限流：可能是按 IP 的共享配额，也可能是宽查询
  （"technology OR breaking news"）成本太高被节流。
  确切触发条件无法从客户端证实，所以三管齐下：
  1) 带上可识别的 User-Agent；
  2) 429 / 限流文本 / 5xx 时退避重试（遵从 Retry-After 头，最多 3 次）；
  3) collector 里把 GDELT 放宽到每 60 分钟 1 次（GDELT 没有互动量数据，
     降频对增长计算几乎没有损失）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)
API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = "GlobalTrendRadar/1.0 (personal research monitor; local hotspot scanner)"

MAX_ATTEMPTS = 3
# 两次重试之间的等待（秒）：10 秒、30 秒。收到 429 且带 Retry-After 头时
# 以该头为准（上限 60 秒）。最坏总等待约 40 秒，远小于 15 分钟扫描间隔。
RETRY_DELAYS = (10, 30)
RETRY_AFTER_CAP = 60
# 限流提示文本的开头：被限流时接口以 200 返回这段纯文本而不是 JSON。
THROTTLE_PREFIX = "Please limit requests"


def _retry_after(resp: requests.Response) -> int | None:
    """从 429 响应里读 Retry-After（秒），读不到或不合法返回 None。"""
    raw = resp.headers.get("Retry-After") if resp.headers else None
    if raw is None:
        return None
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return None


def fetch(query: str = "", max_records: int = 50, timeout: int = 15) -> list[dict]:
    """查询 GDELT Doc API，返回归一化 Item dict 列表。

    429 / 限流文本 / 5xx 时退避重试，最多 3 次；仍失败返回 []（不抛异常，
    由 collector 记 counts）。其他网络错误（DNS、连接失败等）不重试，
    直接返回 []。
    """
    if not query:
        logger.info("GDELT 未启用：config.yaml 中未配置查询词")
        return []
    params = {"query": query, "mode": "artlist", "maxrecords": max_records, "format": "json"}
    headers = {"User-Agent": USER_AGENT}

    last_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(API_URL, params=params, headers=headers, timeout=timeout)
            status = resp.status_code
            if status == 429:
                wait = _retry_after(resp)
                last_error = f"HTTP 429（限流，Retry-After={wait}）"
            elif 500 <= status < 600:
                last_error = f"HTTP {status}"
                resp.raise_for_status()  # 走到 except 里按 5xx 处理
            else:
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data.get("articles") is not None:
                    return _to_items(data)
                # 200 但正文不是预期的 JSON 结构：最常见的是限流提示文本。
                body = (resp.text or "")[:80]
                if body.startswith(THROTTLE_PREFIX):
                    last_error = "被限流：接口返回限流提示文本而非 JSON"
                else:
                    logger.warning("GDELT 返回了无法识别的正文（前 80 字）: %s", body)
                    return []
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 429:
                wait = _retry_after(exc.response)
                last_error = f"HTTP 429（限流，Retry-After={wait}）"
            elif status is not None and 500 <= status < 600:
                last_error = f"HTTP {status}"
            else:
                logger.warning("GDELT 采集失败: %s", exc)
                return []
        except ValueError:
            # resp.json() 解析失败：正文是纯文本（限流提示）而非 JSON。
            body = ""
            try:
                body = (resp.text or "")[:80]
            except Exception:
                pass
            if body.startswith(THROTTLE_PREFIX):
                last_error = "被限流：接口返回限流提示文本而非 JSON"
            else:
                logger.warning("GDELT 返回了无法解析的正文（前 80 字）: %s", body)
                return []
        except requests.RequestException as exc:
            logger.warning("GDELT 采集失败: %s", exc)
            return []

        if attempt < MAX_ATTEMPTS:
            wait = _retry_after(resp) if status == 429 else None
            delay = min(wait, RETRY_AFTER_CAP) if wait is not None else RETRY_DELAYS[attempt - 1]
            logger.warning(
                "GDELT %s，第 %d/%d 次尝试失败，%d 秒后重试",
                last_error,
                attempt,
                MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)

    logger.warning("GDELT 采集失败（已重试 %d 次）: %s", MAX_ATTEMPTS, last_error)
    return []


def _to_items(data: dict) -> list[dict]:
    results: list[dict] = []
    for article in data.get("articles", []):
        title = article.get("title", "")
        url = article.get("url", "")
        if not title or not url:
            continue
        results.append(
            Item(
                platform="gdelt",
                title=str(title).strip(),
                url=str(url).strip(),
                published_at=article.get("seendate"),
                first_seen=datetime.now(timezone.utc).isoformat(),
                source=article.get("domain", "gdelt"),
                language=article.get("language", ""),
            ).to_dict()
        )
    logger.info("GDELT: 采集到 %d 条", len(results))
    return results
