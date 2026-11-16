"""标题自动翻译：把非中文标题翻成中文，供 Dashboard 与 Telegram 提醒显示。

主力：Microsoft Azure AI Translator（官方 API，免费 F0 层每月 200 万
字符，见 docs/TRANSLATION.md）。备用：DeepL API（一次性 100 万字符）。
两个都是官方、有文档的服务；key 只出现在请求头里，日志里绝不出现。

翻译范围很窄（用户定）：只翻 Top 5 事件的名字、触发 Telegram 提醒
的事件名字、以及这些事件各自分数最高的成员标题。其他内容保持英文，
不做大批量翻译。翻译跑在每轮的最后一步——聚类、排名、爆发检测之后，
发提醒之前，这样提醒里当轮就能用上中文事件名（见 collector.py）。

已删除：Google 免费 gtx 端点（2026-11-15 深挖结论：它是网页版 Google
翻译的非官方内部端点，429 是 IP 声誉 + 程序化客户端识别造成，降速、
换参数、加账号/key 都修不好，已彻底放弃；调用代码与测试一并删除）。

缓存：翻译结果存在 SQLite（items.title_zh / events.name_zh），同一标题
只翻一次。失败的翻译绝不写入——失败标题下轮再试，不会永久失去机会。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

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


# ---- Azure AI Translator（主力）----
# 免费 F0 层：每月 2,000,000 字符（按月重置），超额需手动切 S1（$10/百万字符）。
# 端点：POST https://api.cognitive.microsofttranslator.com/translate
#     ?api-version=3.0&to=zh-Hans
# 注意：不传 from 参数，让 Azure 自动检测源语言——要翻的标题里有英文、
# 马拉雅拉姆语、印地语、俄语等多种语言，强制 from=en 会翻错小语种标题。
# 请求体是 JSON 数组 [{"Text": "..."}, ...]，一次请求可带多条（单请求上限
# 50,000 字符，本项目每轮远不到这个数）。认证是请求头
# Ocp-Apim-Subscription-Key，区域资源还要加 Ocp-Apim-Subscription-Region。
# F0 超额时返回 429/403（第三方说法：只报错、不自动扣费，未找到官方逐字
# 确认，见 docs/TRANSLATION.md）。
AZURE_TRANSLATE_URL = "https://api.cognitive.microsofttranslator.com/translate"

# 每轮 Azure 字符硬上限：600 字/轮 × 96 轮/天（15 分钟一轮）× 30 天
# = 1,728,000 字/月；31 天的月份是 1,785,600 字——都低于用户定的
# 1,800,000 字/月上限（见 docs/TRANSLATION.md 的算术）。光靠这个
# 每轮上限，24/7 满负荷跑也烧不穿月度上限。实际用量还低于上限
# （每轮约 10-15 条 × 约 60 字 ≈ 600-900 字，超 600 的部分留到下一轮）。
# 月度总上限另有 data/.azure_usage.json 计数兜底（见下）。
AZURE_MAX_CHARS_PER_ROUND = 600

# Azure 月度字符上限（默认 1,800,000 = F0 每月 200 万的 90%，用户定）：
# 见 load_azure_usage / record_azure_usage。每月按 UTC 月份切分。
AZURE_MONTHLY_CAP_CHARS = 1800000
AZURE_USAGE_FILENAME = ".azure_usage.json"


class AzureUnavailable(Exception):
    """Azure 本轮不可用（429/403/401/网络错/返回异常）：调用方改走 DeepL。"""


def azure_usage_month(now: datetime | None = None) -> str:
    """当月（UTC）的用量月份键，形如 "2026-11"。"""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def load_azure_usage(path) -> tuple[str, int]:
    """读 Azure 月度用量文件（{"month": "2026-11", "chars": N}）。

    返回 (当月, 当月已用字符数)。文件缺失、内容损坏、字段不对时按
    0 字处理（损坏记一条 warning）；存的是上个月时当月用量算 0
    （跨月归零，不用等到下一次成功调用才重置）。
    """
    month = azure_usage_month()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        stored_month = data["month"]
        chars = data["chars"]
        if (
            not isinstance(stored_month, str)
            or not re.fullmatch(r"\d{4}-\d{2}", stored_month)
            or not isinstance(chars, int)
            or isinstance(chars, bool)
            or chars < 0
        ):
            raise ValueError("用量文件字段不对")
    except FileNotFoundError:
        return month, 0
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Azure 月度用量文件损坏（%s），按 0 字处理", path)
        return month, 0
    if stored_month != month:
        return month, 0
    return month, chars


def record_azure_usage(path, chars: int) -> int | None:
    """把一次成功 Azure 请求的字符数累加进当月用量，原子写入。

    只在调用方确认请求成功后调用（失败的请求不计）。跨月时先归零
    再累加。写文件用临时文件 + os.replace，轮次中途崩溃也不会把
    用量文件写坏。返回累加后当月总数；写失败时记一条 warning 并
    返回 None（不影响本轮翻译结果，只影响用量统计）。
    """
    path = Path(path)
    month, used = load_azure_usage(path)
    total = used + max(0, int(chars))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"month": month, "chars": total}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Azure 月度用量文件写入失败（%s）：%s", path, exc)
        return None
    return total


def _parse_azure_response(data, texts: list[str]) -> dict[str, str]:
    """从 Azure 返回的 JSON 里取译文，按输入顺序一一对应。

    成功时形如 [{"translations": [{"text": "...", "to": "zh-Hans"}]}]，
    与请求体的 texts 一一对应。某条解析不出或译文为空时跳过该条
    （调用方不存、下一轮再试）；译文与原文相同也跳过（等于没翻出来）。
    """
    results: dict[str, str] = {}
    if not isinstance(data, list):
        return results
    for text, entry in zip(texts, data):
        try:
            translated = str(entry["translations"][0]["text"]).strip()
        except (TypeError, KeyError, IndexError, AttributeError):
            continue
        if translated and translated != text:
            results[text] = translated
    return results


def azure_translate_many(
    texts: list[str],
    api_key: str,
    region: str | None = None,
    max_texts: int = 15,
    max_chars: int = AZURE_MAX_CHARS_PER_ROUND,
    target_lang: str = "zh-Hans",
    timeout: int = 15,
) -> tuple[dict[str, str], int, bool]:
    """批量用 Azure 翻译：最多 max_texts 条、源文总字符不超过 max_chars。

    全部文本一次请求发完（远低于单请求 50,000 字符上限）。返回
    (results, chars_used, budget_exhausted)：results 只含成功翻出的
    {原文: 译文}，chars_used 是本批已提交的源文字符数（失败的请求记 0），
    budget_exhausted 为 True 表示本轮条数/字符预算已满、剩余文本没发。
    不传 from 参数：让 Azure 自动检测源语言（标题里有多种语言）。
    任何失败（429/403/401/网络错/返回异常）都只记一条 warning 后抛
    AzureUnavailable——调用方改走 DeepL。429/403 不断言是月额度还是
    频率限制（响应里没说就不猜）。key 只出现在请求头里，日志和异常
    信息里绝不出现——原异常的文本可能夹带请求细节，所以这里只用固定
    文案，不拼原异常。
    """
    clean: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text or not text.strip() or text in seen:
            continue
        seen.add(text)
        clean.append(text)
    batch: list[str] = []
    chars_used = 0
    for text in clean:
        if len(batch) >= max_texts or chars_used + len(text) > max_chars:
            break  # 再加这条就超本轮预算：停掉，留到下一轮
        batch.append(text)
        chars_used += len(text)
    exhausted = len(batch) < len(clean)
    if not batch or not api_key:
        return {}, 0, exhausted
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    if (region or "").strip():
        headers["Ocp-Apim-Subscription-Region"] = region.strip()
    try:
        resp = requests.post(
            AZURE_TRANSLATE_URL,
            params={"api-version": "3.0", "to": target_lang},
            headers=headers,
            json=[{"Text": text} for text in batch],
            timeout=timeout,
        )
        if resp.status_code in (429, 403):
            logger.warning(
                "Azure 翻译受限（HTTP %s）：本轮停止 Azure，改走 DeepL"
                "（可能是月额度或频率限制，见 docs/TRANSLATION.md）",
                resp.status_code,
            )
            raise AzureUnavailable(f"Azure 翻译受限（HTTP {resp.status_code}）")
        if resp.status_code == 401:
            logger.warning(
                "Azure 翻译认证失败（HTTP 401）：请检查 AZURE_TRANSLATOR_KEY "
                "是否正确，本轮改走 DeepL"
            )
            raise AzureUnavailable("Azure 翻译认证失败（HTTP 401）")
        if resp.status_code != 200:
            logger.warning(
                "Azure 翻译失败（HTTP %s）：本轮改走 DeepL", resp.status_code
            )
            raise AzureUnavailable(f"Azure 翻译失败（HTTP {resp.status_code}）")
        results = _parse_azure_response(resp.json(), batch)
        if not results:
            # 2026-11-16 连续两轮都栽在这里：HTTP 200 但解析不出可用
            # 译文（返回不是列表、或每条都没 translations[0].text、
            # 或译文为空/与原文相同，见 _parse_azure_response）。只报
            # 一句“没有可用译文”没法定位——把状态码和返回体截断（200
            # 字符，不含 key，返回体是 Azure 的回复、不含请求密钥）
            # 一起记下来，下次从日志就能看出来。
            logger.warning(
                "Azure 翻译失败（HTTP %s，返回里没有可用译文）：本轮改走 DeepL；返回体截断：%s",
                resp.status_code,
                resp.text[:200],
            )
            raise AzureUnavailable("Azure 翻译返回里没有可用译文")
        return results, chars_used, exhausted
    except AzureUnavailable:
        raise
    except Exception:
        # 固定文案：原异常的文本可能夹带请求细节（甚至 key），绝不记、绝不拼。
        logger.warning("Azure 翻译请求失败：本轮改走 DeepL")
        raise AzureUnavailable("Azure 翻译请求失败") from None


# ---- DeepL（备用）----
# Azure 不可用（没配 key、429/403、请求失败、月度用量用完）时才走
# DeepL。DeepL 按字符计费，免费额度有限——新注册只能选 Developer
# 计划：一次性 100 万字符，用完不恢复（旧版每月 50 万字符的 API
# Free 已停售）。所以 DeepL 同样只翻优先内容，**每小时最多 1 次
# 轮次**（见 collector.py；Azure 每轮都跑，DeepL 不跟——每小时
# 跑一次都够把 100 万总额在约 5 周内烧完）。额度与注册步骤见
# docs/TRANSLATION.md。
DEEPL_FREE_HOST = "api-free.deepl.com"
DEEPL_PRO_HOST = "api.deepl.com"
DEEPL_TRANSLATE_PATH = "/v2/translate"

# 每轮 DeepL 字符硬上限：1200 字/轮 × 24 轮/天（每小时最多 1 次）
# × 30 天 = 864,000 字/月。DeepL 只在 Azure 不可用时跑，实际用量
# 远低于这个上限；但注意它只有一次性 100 万字符总额——如果 Azure
# 长期不可用、DeepL 每轮拉满，约 5 周就烧完（见 docs/TRANSLATION.md）。
DEEPL_MAX_CHARS_PER_ROUND = 1200


class DeepLQuotaExhausted(Exception):
    """DeepL 免费额度用完（HTTP 456）：本轮停止 DeepL 调用。"""


def deepl_host(api_key: str) -> str:
    """按 key 后缀选 host：以 :fx 结尾的免费 key 走 api-free，否则走 api.deepl.com。

    key 发错 host 会 403——免费 key 必须走 api-free，付费 key 必须走 api.deepl.com。
    """
    return DEEPL_FREE_HOST if api_key.strip().endswith(":fx") else DEEPL_PRO_HOST


def deepl_target_lang(target_lang: str) -> str:
    """把配置的语言代码（zh-CN）换成 DeepL 的代码（ZH）。"""
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
    返回 ""——调用方把该标题留给下一轮再试。
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
    {原文: 译文}，失败的原文不在里面（调用方不存、下一轮再试）；
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
