"""HTML 实体解码测试：标题入库时解一次，库里就是干净文本。

2026-11-16 20:28 截图：Dashboard 里 "Today&#39;s Breaking News LIVE"
原样显示——YouTube 等来源的标题带 HTML 实体。修复点是
db.upsert_item（所有入库写入的唯一 choke 点）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod


def _item(title, url="https://example.com/1", title_zh=None):
    item = {
        "platform": "youtube",
        "title": title,
        "url": url,
        "published_at": None,
        "first_seen": None,
        "source": "test",
        "language": "en",
        "summary_zh": "",
        "title_zh": title_zh,
    }
    return item


class TestHtmlEntityDecoding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.sqlite3"
        self.conn = dbmod.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _stored_title(self, url="https://example.com/1"):
        row = self.conn.execute(
            "SELECT title, title_zh FROM items WHERE url = ?", (url,)
        ).fetchone()
        return row[0], row[1]

    def test_apostrophe_decoded(self):
        dbmod.upsert_item(self.conn, _item("Today&#39;s Breaking News LIVE"), {"score": 0.4})
        title, _ = self._stored_title()
        self.assertEqual(title, "Today's Breaking News LIVE")

    def test_amp_quot_lt_gt_decoded(self):
        dbmod.upsert_item(
            self.conn,
            _item("A &amp; B &quot;quoted&quot; &lt;tag&gt; &#x27;x&#x27;"),
            {"score": 0.4},
        )
        title, _ = self._stored_title()
        self.assertEqual(title, "A & B \"quoted\" <tag> 'x'")

    def test_title_zh_decoded_too(self):
        dbmod.upsert_item(
            self.conn,
            _item("Hello", title_zh="你好&#39;世界"),
            {"score": 0.4},
        )
        _, title_zh = self._stored_title()
        self.assertEqual(title_zh, "你好'世界")

    def test_clean_title_unchanged(self):
        dbmod.upsert_item(self.conn, _item("Breaking news, no entities"), {"score": 0.4})
        title, _ = self._stored_title()
        self.assertEqual(title, "Breaking news, no entities")

    def test_no_double_decoding(self):
        # 双重编码的 "&amp;#39;" 只解一遍成 "&#39;"，不解成 "'"；
        # 已经干净的标题重复入库也不会被改坏。
        dbmod.upsert_item(self.conn, _item("Tom &amp;#39;s show"), {"score": 0.4})
        title, _ = self._stored_title()
        self.assertEqual(title, "Tom &#39;s show")
        dbmod.upsert_item(self.conn, _item("Tom &amp;#39;s show"), {"score": 0.5})
        title, _ = self._stored_title()
        self.assertEqual(title, "Tom &#39;s show")

    def test_reupsert_redecodes(self):
        # 老库里已有的带实体标题，下次采到同一 URL 时会被干净标题覆盖。
        self.conn.execute(
            "INSERT INTO items (platform, title, url, collected_at) "
            "VALUES ('youtube', 'Old&#39;s title', 'https://example.com/1', '2026-11-16T00:00:00+00:00')"
        )
        self.conn.commit()
        dbmod.upsert_item(self.conn, _item("Old&#39;s title"), {"score": 0.4})
        title, _ = self._stored_title()
        self.assertEqual(title, "Old's title")


if __name__ == "__main__":
    unittest.main()
