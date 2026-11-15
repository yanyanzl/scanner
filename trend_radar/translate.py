"""标题自动翻译：把非中文标题翻成中文，供 Dashboard 与 Telegram 提醒显示。

用 Google 免费翻译接口的非官方 client=gtx 端点（无需 key）：
GET https://translate.googleapis.com/translate_a/single
    ?client=gtx&sl=auto&tl=zh-CN&dt=t&q=<text>
返回 JSON 如 [[["你好世界","Hello world",null,null,3]],null,"en"]，
data[0] 是 [译文, 原文, ...] 的分段列表，拼接各段译文即得全文。

注意：这是非官方端点，没有任何可用性保证——可能被限流或屏蔽。
所以本模块的设计原则和 Reddit / YouTube 没有凭证时一样：失败时
安静地回退到原标题，只记一条 warning，绝不抛异常、不中断采集。
翻译结果缓存在 SQLite（items.title_zh / events.name_zh），同一标题
只翻一次，每轮还有数量上限（见 collector.py），不会每轮都去调接口。
如果 Google 哪天彻底屏蔽这个端点，备选方案见 docs/TRANSLATION.md。
"""
from __future__ import annotations

import logging
import re

import requests

logger = logging.getLogger(__name__)

GTX_URL = "https://translate.googleapis.com/translate_a/single"

# 该端点是 translate.google.com 网页版用的接口，期望浏览器客户端；
# 用浏览器 User-Agent 而不是默认的 python-requests 可以少被限流。
# 这不是绕过认证（接口本身无 key、无登录），只是按它期望的客户端发请求。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 中文算“已经是中文”的阈值：CJK 字占 CJK+拉丁字母的比例 >= 0.9 时不翻译。
# 数字、标点、emoji、空格不计入分母——纯数字标题（如 "15.11.2026"）
# 没有字母，直接判为不需要翻译。
CJK_ALREADY_CHINESE_RATIO = 0.9

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def needs_translation(text: str) -> bool:
    """标题是否需要翻译：空、纯中文（CJK 占比 >= 0.9）、无字母的返回 False，
    其他（英文、马拉雅拉姆语、中英混合等）返回 True。

    字母用 str.isalpha() 判断，不只算拉丁字母——否则纯马拉雅拉姆语、
    纯阿拉伯语、纯俄语等标题（一个拉丁/CJK 字母都没有）会被误判为
    “没有字母”而跳过翻译。"""
    if not text or not text.strip():
        return False
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    cjk = sum(1 for ch in letters if _CJK_RE.match(ch))
    return cjk / len(letters) < CJK_ALREADY_CHINESE_RATIO


def _parse_gtx_response(data) -> str:
    """从 gtx 返回的 JSON 里拼接译文；解析不出译文时返回 ""。"""
    try:
        segments = data[0]
    except (TypeError, IndexError, KeyError):
        return ""
    parts: list[str] = []
    for seg in segments or []:
        try:
            translated = seg[0]
        except (TypeError, IndexError):
            continue
        if translated:
            parts.append(str(translated))
    return "".join(parts).strip()


def translate_text(text: str, target_lang: str = "zh-CN", timeout: int = 15) -> str:
    """把 text 翻译成目标语言（默认简体中文）。

    任何失败（网络错误、非 200、JSON 解析失败、返回里没有译文）
    都返回原文本，不抛异常。标题是公开新闻标题，记 warning 时可以带上。
    """
    if not text or not text.strip():
        return text
    try:
        resp = requests.get(
            GTX_URL,
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": target_lang,
                "dt": "t",
                "q": text,
            },
            headers={"User-Agent": BROWSER_USER_AGENT},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise ValueError(f"翻译接口返回 HTTP {resp.status_code}")
        translated = _parse_gtx_response(resp.json())
        if not translated:
            raise ValueError("翻译接口返回里没有译文")
        return translated
    except Exception as exc:
        logger.warning("翻译失败（用原标题）: %s | %s", exc, text[:80])
        return text


def translate_titles(
    titles: list[str], target_lang: str = "zh-CN", timeout: int = 15
) -> dict[str, str]:
    """批量翻译标题，返回 {原文: 译文}。

    不需要翻译的标题直接映射到自身，不调接口；相同标题只翻一次。
    单个标题失败不影响其他标题（translate_text 本身不抛异常）。
    """
    result: dict[str, str] = {}
    for title in titles:
        if title in result:
            continue
        if not needs_translation(title):
            result[title] = title
        else:
            result[title] = translate_text(title, target_lang=target_lang, timeout=timeout)
    return result
