"""调度器接线测试：create_scheduler 加了 15 分钟采集 + 每日质量简报。

系统 python3 没有 apscheduler（见 README：冒烟用 slim venv）时整类
跳过——与 Streamlit AppTest 同一个做法；CI 装了 requirements 后
真实跑。
"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_APSCHEDULER_SPEC = importlib.util.find_spec("apscheduler")


@unittest.skipUnless(_APSCHEDULER_SPEC is not None, "apscheduler 未安装，跳过调度器接线测试")
class TestCreateScheduler(unittest.TestCase):
    def _jobs(self, config):
        from run_scheduler import create_scheduler

        scheduler = create_scheduler(config)
        return scheduler.get_jobs()

    def test_collect_and_digest_jobs(self):
        from trend_radar.quality_digest import send_quality_digest

        jobs = self._jobs({})
        self.assertEqual(len(jobs), 2)
        funcs = {j.func.__name__ if hasattr(j.func, "__name__") else str(j.func) for j in jobs}
        self.assertIn("collect_once", funcs)
        self.assertIn("send_quality_digest", funcs)

    def test_digest_defaults_to_8am(self):
        from trend_radar.quality_digest import send_quality_digest

        jobs = self._jobs({})
        digest = [j for j in jobs if j.func is send_quality_digest]
        self.assertEqual(len(digest), 1)
        trigger = digest[0].trigger
        # APScheduler CronTrigger 的字段：hour 一定是 '8'。
        fields = {f.name: str(f) for f in trigger.fields}
        self.assertEqual(fields.get("hour"), "8")

    def test_digest_hour_from_config(self):
        from trend_radar.quality_digest import send_quality_digest

        jobs = self._jobs({"quality_digest": {"enabled": True, "hour": 20}})
        digest = [j for j in jobs if j.func is send_quality_digest]
        self.assertEqual(len(digest), 1)
        fields = {f.name: str(f) for f in digest[0].trigger.fields}
        self.assertEqual(fields.get("hour"), "20")

    def test_digest_disabled_adds_no_job(self):
        jobs = self._jobs({"quality_digest": {"enabled": False}})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].func.__name__, "collect_once")

    def test_collect_interval_from_config(self):
        jobs = self._jobs({"scan_interval_minutes": 30})
        collect = [j for j in jobs if j.func.__name__ == "collect_once"]
        self.assertEqual(len(collect), 1)
        # IntervalTrigger 的 interval 是 timedelta。
        self.assertEqual(collect[0].trigger.interval.total_seconds(), 1800)


if __name__ == "__main__":
    unittest.main()
