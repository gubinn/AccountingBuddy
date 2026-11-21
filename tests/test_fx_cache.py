import os
import tempfile
import unittest

import fx_cache


class FxCacheTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        os.unlink(self.path)
        self.old_path = fx_cache.CACHE_FILE
        fx_cache.CACHE_FILE = self.path

    def tearDown(self):
        fx_cache.CACHE_FILE = self.old_path
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_pairs_most_recent_same_amount(self):
        fx_cache.record_buy(1000, 850.00, "2026-09-01", "a")
        fx_cache.record_buy(1000, 857.80, "2026-09-26", "b")

        self.assertEqual(fx_cache.take_cost(1000), 857.80)
        self.assertEqual(fx_cache.take_cost(1000), 850.00)
        self.assertIsNone(fx_cache.take_cost(1000))

    def test_no_matching_buy(self):
        fx_cache.record_buy(500, 430.00, "2026-09-01", "a")
        self.assertIsNone(fx_cache.take_cost(1000))

    def test_deduplicates_by_external_id(self):
        fx_cache.record_buy(1000, 857.80, "2026-09-26", "b")
        fx_cache.record_buy(1000, 857.80, "2026-09-26", "b")

        self.assertEqual(fx_cache.take_cost(1000), 857.80)
        self.assertIsNone(fx_cache.take_cost(1000))

    def test_dry_run_does_not_consume(self):
        fx_cache.record_buy(1000, 857.80, "2026-09-26", "b")

        self.assertEqual(fx_cache.take_cost(1000, consume=False), 857.80)
        self.assertEqual(fx_cache.take_cost(1000), 857.80)


if __name__ == "__main__":
    unittest.main()