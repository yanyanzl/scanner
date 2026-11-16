"""定时调度：按 config.yaml 的间隔（默认 15 分钟）循环采集。

先立即跑一轮，再按间隔执行。保持电脑开机、不进入睡眠，
否则定时任务无法执行。

单次扫描模式（用于命令行冒烟测试和 GitHub Actions）：
    python run_scheduler.py --once
只跑一轮采集就退出，不启动阻塞的 15 分钟循环。
"""
from __future__ import annotations

import argparse
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from trend_radar.collector import collect_once, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def create_scheduler(config: dict) -> BlockingScheduler:
    """建调度器（不启动）：15 分钟一轮采集 + 每天一次质量简报。

    质量简报是 cron 任务（默认每天 8 点，调度器本地时间，见
    trend_radar/quality_digest.py）；quality_digest.enabled 为 false
    时不加这个任务。抽成函数是为了能单测接线（任务存在、时间对），
    main() 里只负责启动。
    """
    from trend_radar.quality_digest import load_digest_config, send_quality_digest

    minutes = int(config.get("scan_interval_minutes", 15))
    scheduler = BlockingScheduler()
    scheduler.add_job(collect_once, "interval", minutes=minutes, kwargs={"config": config})
    digest_cfg = load_digest_config(config)
    if digest_cfg.get("enabled", True):
        scheduler.add_job(
            send_quality_digest,
            "cron",
            hour=int(digest_cfg.get("hour", 8)),
            kwargs={"config": config},
        )
    return scheduler


def main() -> None:
    parser = argparse.ArgumentParser(description="Global Trend Radar 定时采集")
    parser.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮采集就退出，不启动 15 分钟循环（用于冒烟测试 / CI）",
    )
    args = parser.parse_args()

    config = load_config()
    if args.once:
        logger.info("单次扫描模式：跑一轮采集后退出")
        print(collect_once(config))
        return
    minutes = int(config.get("scan_interval_minutes", 15))
    logger.info("Global Trend Radar 启动：每 %d 分钟扫描一次，先立即跑一轮", minutes)
    collect_once(config)
    scheduler = create_scheduler(config)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度已停止")


if __name__ == "__main__":
    main()
