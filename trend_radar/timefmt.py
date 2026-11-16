"""时间显示：把库里的 ISO 时间串转成本机当地时间的紧凑格式。

全项目唯一的时间格式化入口——面板各节（dashboard_rows）、提醒
正文（alerts）、简报（quality_digest）都调这里，不各自拼格式。

- 输入：ISO 时间串（带 Z、带时区偏移、无时区）、RFC822、时间戳、
  datetime。无时区视为 UTC（库里老数据常见）。
- 输出：本机系统当地时间的 "MM-DD HH:MM"（如 11-16 20:28）。
  时区不硬编码：默认用运行机器的系统本地时区（Steven 的 Mac 是
  伦敦时间）；`tz` 参数只供测试传固定时区断言精确输出。
- 空、None、解析失败一律返回 ""（表格里显示空白，不把原始脏串
  透传给用户看）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from trend_radar.scoring import parse_dt


def format_local_time(value: Any, tz=None) -> str:
    """把一个时间值格式化成本机当地时间的 "MM-DD HH:MM"。

    value 为空、None、或 parse_dt 解析失败时返回 ""。
    tz 为 None 时用系统本地时区；传 tzinfo 时转到该时区（测试用）。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = parse_dt(value)
        except Exception:
            return ""
        if dt is None:
            return ""
    target = tz if tz is not None else datetime.now().astimezone().tzinfo
    return dt.astimezone(target).strftime("%m-%d %H:%M")
