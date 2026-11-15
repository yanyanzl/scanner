"""下载文件自动清理测试：只删 data/downloads/ 下超过 1 小时的文件。"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar.downloads import cleanup_downloads


class TestCleanupDownloads(unittest.TestCase):
    def _make_file(self, directory, name, age_seconds):
        path = Path(directory) / name
        path.write_text("x", encoding="utf-8")
        old = time.time() - age_seconds
        os.utime(path, (old, old))
        return path

    def test_old_file_removed_fresh_file_kept(self):
        with tempfile.TemporaryDirectory() as d:
            downloads = Path(d) / "downloads"
            downloads.mkdir()
            old = self._make_file(downloads, "old.zip", 7200)
            fresh = self._make_file(downloads, "fresh.zip", 10)
            deleted = cleanup_downloads(downloads, max_age_seconds=3600)
            self.assertEqual(deleted, 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_other_directories_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            downloads = Path(d) / "downloads"
            downloads.mkdir()
            other = Path(d) / "data"
            other.mkdir()
            old_download = self._make_file(downloads, "old.zip", 7200)
            old_elsewhere = self._make_file(other, "keep.zip", 7200)
            deleted = cleanup_downloads(downloads, max_age_seconds=3600)
            self.assertEqual(deleted, 1)
            self.assertFalse(old_download.exists())
            self.assertTrue(old_elsewhere.exists())

    def test_subdirectory_not_recursed(self):
        with tempfile.TemporaryDirectory() as d:
            downloads = Path(d) / "downloads"
            sub = downloads / "sub"
            sub.mkdir(parents=True)
            inner = self._make_file(sub, "inner.zip", 7200)
            deleted = cleanup_downloads(downloads, max_age_seconds=3600)
            self.assertEqual(deleted, 0)
            self.assertTrue(sub.exists())
            self.assertTrue(inner.exists())

    def test_missing_directory_returns_zero(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cleanup_downloads(Path(d) / "nope"), 0)

    def test_empty_directory_returns_zero(self):
        with tempfile.TemporaryDirectory() as d:
            downloads = Path(d) / "downloads"
            downloads.mkdir()
            self.assertEqual(cleanup_downloads(downloads), 0)


if __name__ == "__main__":
    unittest.main()
