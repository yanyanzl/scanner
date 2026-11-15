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


# ---- DeepL（官方 API，重要标题专用）----
# gtx 端点 2026-11-15 已确认对用户不可用：云服务器 IP 与用户家里宽带 IP
# 上全部返回 HTTP 429。DeepL 是官方、有文档的替代：免费额度内不花钱，
# 但字符要省着用——所以 DeepL 只在每小时最多 1 次的轮次里翻“最重要的”
# 标题（分数最高的内容标题 + 事件名），其余仍走 gtx（它一旦恢复就能
# 覆盖大批量；恢复不了时大批量标题就保持英文，不影响采集和排名）。
# 额度与注册步骤见 docs/TRANSLATION.md。
DEEPL_FREE_HOST = "api-free.deepl.com"
DEEPL_PRO_HOST = "api.deepl.com"
DEEPL_TRANSLATE_PATH = "/v2/translate"

# 每轮 DeepL 字符硬上限：660 字/轮 × 24 轮/天 × 31 天 = 491,040 字，
# 无论标题多长，一个月都超不过免费额度（见 docs/TRANSLATION.md 的算术）。
DEEPL_MAX_CHARS_PER_ROUND = 660


class DeepLQuotaExhausted(Exception):
    """DeepL 免费额度用完（HTTP 456）：本轮停止 DeepL 调用。"""


def deepl_host(api_key: str) -> str:
    """按 key 后缀选 host：以 :fx 结尾的免费 key 走 api-free，否则走 api.deepl.com。

    key 发错 host 会 403——免费 key 必须走 api-free，付费 key 必须走 api.deepl.com。
    """
    return DEEPL_FREE_HOST if api_key.strip().endswith(":fx") else DEEPL_PRO_HOST


def deepl_target_lang(target_lang: str) -> str:
    """把 gtx 的语言代码（zh-CN）换成 DeepL 的代码（ZH）。"""
    lang = (target_lang or "zh-CN").strip().lower()
    if lang.startswith("zh"):
        return "ZH"
    return lang.split("-")[0].upper()


def _parse_deepl_response(data) -> str:
    """从 DeepL 返回的 JSON 里取译文；解析不出时返回 ""。

    成功时形如 {"translations": [{"detected_source_language": "EN", "text": "..."}]}。
    """
    try:
        return str(data["translations"][0]["text"]).strip()
    except (TypeError, KeyError, IndexError, AttributeError):
        return ""


def deepl_translate(text: str, api_key: str, target_lang: str = "ZH", timeout: int = 15) -> str:
    """用 DeepL 翻一条文本。成功返回译文；失败返回 ""（不抛异常）。

    只有额度用完（HTTP 456）时抛 DeepLQuotaExhausted，让调用方停掉本轮
    的 DeepL 调用。其他失败（401/403/429/网络错/返回无译文）只记 warning，
    返回 ""——调用方会把该标题留给 gtx 或下一轮再试。
    key 只出现在 Authorization 请求头里，日志和异常信息里绝不出现。
    """
    if not text or not text.strip() or not api_key:
        return ""
    try:
        resp = requests.post(
            f"https://{deepl_host(api_key)}{DEEPL_TRANSLATE_PATH}",
            data={"text": text, "target_lang": target_lang},
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            timeout=timeout,
        )
        if resp.status_code == 456:
            raise DeepLQuotaExhausted("DeepL 免费额度已用完（HTTP 456）")
        if resp.status_code != 200:
            logger.warning("DeepL 翻译失败（HTTP %s）: %s", resp.status_code, text[:80])
            return ""
        translated = _parse_deepl_response(resp.json())
        if not translated:
            logger.warning("DeepL 翻译失败（返回里没有译文）: %s", text[:80])
            return ""
        return translated
    except DeepLQuotaExhausted:
        raise
    except Exception as exc:
        logger.warning("DeepL 翻译失败（用原标题）: %s | %s", exc, text[:80])
        return ""


def deepl_translate_many(
    texts: list[str],
    api_key: str,
    max_texts: int,
    max_chars: int,
    target_lang: str = "ZH",
    timeout: int = 15,
) -> tuple[dict[str, str], int, bool]:
    """批量用 DeepL 翻译：最多 max_texts 条、源文总字符不超过 max_chars。

    返回 (results, chars_used, quota_exhausted)：results 只含成功翻出的
    {原文: 译文}，失败的原文不在里面（调用方不存、留给 gtx 或下一轮）；
    chars_used 是本批已提交的源文字符数（按 DeepL 的计费口径，提交即算，
    失败的也算，宁可多算不少算）；quota_exhausted 为 True 表示中途撞上
    HTTP 456，剩余文本不再调接口。
    456 出现时记一条额度用完的 warning（只记一次），不再重复记。
    """
    results: dict[str, str] = {}
    chars_used = 0
    quota_exhausted = False
    attempted = 0
    seen: set[str] = set()
    for text in texts:
        if not text or not text.strip() or text in seen:
            continue
        seen.add(text)
        if attempted >= max_texts:
            break
        if chars_used + len(text) > max_chars:
            break  # 再加这条就超本轮字符上限：停掉，留到下一轮
        attempted += 1
        chars_used += len(text)
        try:
            translated = deepl_translate(
                text, api_key, target_lang=target_lang, timeout=timeout
            )
        except DeepLQuotaExhausted:
            quota_exhausted = True
            logger.warning(
                "DeepL 免费额度已用完（HTTP 456）：本轮停止 DeepL，未翻标题保持英文（见 docs/TRANSLATION.md）"
            )
            break
        if translated and translated != text:
            results[text] = translated
    return results, chars_used, quota_exhausted
