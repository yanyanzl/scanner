"""标题文本小工具：多个模块共用的纯函数（不读库、不联网）。

这里放的是“标题长什么样”的判定逻辑：翻译模块（translate.py）
和排名模块（event_ranking.py）都要用，提出来避免两处各写一份
（之前 _strip_untranslatable_tokens 只活在 translate.py 里，
排名要用 hashtag 占比时只能再抄一份）。

- strip_untranslatable_tokens：去掉 URL、@提及、#hashtag。
- hashtag_ratio：标题里 hashtag 占的字符比例（0-1）。
- is_generic_title：标题是不是通用标题（去掉通用词后一个字母都不剩）。
"""
from __future__ import annotations

import re

# 没有可翻译内容的标记：URL、@提及、#hashtag。顺序有讲究：URL 在先——
# URL 里可能带 # 片段（如 .../page#section），先去 hashtag 会只去掉
# 片段、留下带字母的 URL 本体。
_UNTRANSLATABLE_TOKEN_RE = re.compile(r"https?://\S+|www\.\S+|@\w+|#\S+")

_HASHTAG_RE = re.compile(r"#\S+")

# 通用标题词：本身几乎没有信息量的标题标记。判定见 is_generic_title：
# 去掉这些词 + URL/@/# 后，一个字母都不剩的标题就是通用标题（如
# 光秃秃的“突发新闻”）。中文词直接子串去掉；英文词组用正则（词边界
# + 词间空白，大小写不敏感），避免把 "BREAKING NEWSROOM" 里的
# "BREAKING NEWS" 也抠掉一半。括号里的通用词不用特殊处理——括号是
# 标点，去掉通用词后括号里什么都不剩（如 [突发新闻]）。
_GENERIC_WORDS_ZH = ("突发新闻", "最新消息", "最新资讯", "今日头条")
_GENERIC_WORDS_EN = (
    re.compile(r"\bbreaking\s+news\b", re.IGNORECASE),
    re.compile(r"\blatest\s+news\b", re.IGNORECASE),
    re.compile(r"\bjust\s+in\b", re.IGNORECASE),
)


def is_generic_title(text: str) -> bool:
    """标题是不是通用标题：去掉通用词、URL、@提及、#hashtag 后，
    一个字母（汉字/拉丁字母，str.isalpha）都不剩。

    “突发新闻：赛义德·霍孔被捕”有实质内容（去掉通用词后还剩
    “赛义德霍孔被捕”），不算；光秃秃的“突发新闻！！”去掉通用词
    和标点后什么都不剩，算。数字不算有效字符（“突发新闻 1”仍算
    通用），emoji、标点、空格、括号也不算。空标题算通用。
    """
    text = strip_untranslatable_tokens(text or "")
    for word in _GENERIC_WORDS_ZH:
        text = text.replace(word, "")
    for pattern in _GENERIC_WORDS_EN:
        text = pattern.sub("", text)
    return not any(ch.isalpha() for ch in text)


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
