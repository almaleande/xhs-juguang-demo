"""演示日期必须覆盖本地关键词数据，不能随系统日期漂移取空。"""
from datetime import datetime
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import kwdata, ranges


class DemoRangesTest(unittest.TestCase):
    def test_yesterday_has_keywords_even_when_presented_in_future(self):
        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2027, 3, 15)

        with patch.dict(os.environ, {}, clear=True), patch.object(ranges, 'datetime', FutureClock):
            start, end = ranges.bounds('latest')
            snap = kwdata.keyword_snapshot(str(start), str(end), '昨日')
        self.assertTrue(snap['keywords'], '演示昨日不能落在本地日库之外')
        self.assertEqual(str(start), '2026-10-23')
        self.assertEqual(str(end), '2026-10-23')

    def test_all_demo_ranges_have_keywords(self):
        with patch.dict(os.environ, {}, clear=True):
            for key, label in ranges.RANGES:
                with self.subTest(range=key):
                    start, end = ranges.bounds(key)
                    snap = kwdata.keyword_snapshot(str(start), str(end), label)
                    self.assertTrue(snap['keywords'])
                    self.assertGreater(snap['totals']['fee'], 0)

    def test_explicit_reference_date_and_date_argument_are_honored(self):
        with patch.dict(os.environ, {'KB_REF_DATE': '2026-10-22'}):
            self.assertEqual(str(ranges.bounds('latest')[0]), '2026-10-21')
            self.assertEqual(str(ranges.bounds('latest', today=datetime(2026, 10, 20).date())[0]), '2026-10-19')


if __name__ == '__main__':
    unittest.main()
