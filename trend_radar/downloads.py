"""下载文件自动清理：data/downloads/ 里超过 1 小时的文件删掉。

规则（用户要求）：扫描器下载的任何文件（以后接 GDELT GKG bulk 文件
或别的批量下载时），在下载 + 分析完成 1 小时后自动删除。实现是每轮
collect_once 跑一次清理：只扫 data/downloads/ 这一个目录，按文件
mtime 判断，超过 max_age_seconds（默认 3600 秒）就删。

安全边界：只删该目录下的文件，不递归进子目录，绝不碰别的目录；
目录不存在时直接返回 0；单个文件删失败只记 warning，不抛异常。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS = 3600


def cleanup_downloads(
    downloads_dir: Path | str,
    max_age_seconds: int = 3600,
    now: float | None = None,
) -> int:
    """删除 downloads_dir 下超过 max_age_seconds 的文件，返回删除数。

    只看该目录顶层的文件（mtime 早于 now - max_age_seconds），子目录
    不动。目录不存在返回 0，不抛异常。
    """
    directory = Path(downloads_dir)
    if not directory.is_dir():
        return 0
    now = time.time() if now is None else now
    cutoff = now - max_age_seconds
    deleted = 0
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        logger.warning("下载目录清理失败（无法列出 %s）: %s", directory, exc)
        return 0
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                deleted += 1
        except OSError as exc:
            logger.warning("下载目录清理失败（无法删除 %s）: %s", entry, exc)
            continue
    if deleted:
        logger.info("下载目录清理：删除 %d 个超过 %d 秒的文件", deleted, max_age_seconds)
    return deleted
