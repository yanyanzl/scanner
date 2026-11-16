"""标题文本小工具：多个模块共用的纯函数（不读库、不联网）。

这里放的是“标题长什么样”的判定逻辑：翻译模块（translate.py）
和排名模块（event_ranking.py）都要用，提出来避免两处各写一份
（之前 _strip_untranslatable_tokens 只活在 translate.py 里，
排名要用 hashtag 占比时只能再抄一份）。

- strip_untranslatable_tokens：去掉 URL、@提及、#hashtag。
- hashtag_ratio：标题里 hashtag 占的字符比例（0-1）。
"""
from __future__ import annotations

import re

# 没有可翻译内容的标记：URL、@提及、#hashtag。顺序有讲究：URL 在先——
# URL 里可能带 # 片段（如 .../page#section），先去 hashtag 会只去掉
# 片段、留下带字母的 URL 本体。
_UNTRANSLATABLE_TOKEN_RE = re.compile(r"https?://\S+|www\.\S+|@\w+|#\S+")

_HASHTAG_RE = re.compile(r"#\S+")


def strip_untranslatable_tokens(text: str) -> str:
    """去掉文本里的 URL、@提及、#hashtag，剩下的才是可能要翻/要看的内容。"""
    return _UNTRANSLATABLE_TOKEN_RE.sub("", text or "")


def hashtag_ratio(text: str) -> float:
    """标题里 hashtag 占的字符比例（0-1）。

    hashtag 词按 # 开头、到空白为止的一段算（含 # 本身），无空格连写
    的 #a#b#c 是一整个词。空标题返回 0.0。分母是 hashtag 字符数 +
    去掉 hashtag 后剩下的非空白字符数——分隔用的空白不算内容，所以
    纯 hashtag 标题占比 1.0；“Clown's Artificial Intelligence#cosplay
    #shorts #SpiderMan#kpop”约 0.51；无 hashtag 时 0.0。
    """
    text = (text or "").strip()
    if not text:
        return 0.0
    tagged = sum(len(m) for m in _HASHTAG_RE.findall(text))
    if tagged == 0:
        return 0.0
    rest = _HASHTAG_RE.sub("", text)
    content = len("".join(rest.split()))
    return tagged / (tagged + content)
