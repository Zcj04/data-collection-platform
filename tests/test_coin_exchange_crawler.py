import unittest
from unittest.mock import patch

from crawlers import coin_exchange_crawler as crawler


class CoinExchangeCrawlerTests(unittest.TestCase):
    def test_other_months_do_not_read_or_reuse_data(self):
        with patch.object(crawler, "_fetch_kdocs_amounts") as online, patch.object(
            crawler, "_load_data_file"
        ) as local:
            for date in ("2026-07-31", "2026-09-01", "2025-08-01", "2027-08-01"):
                for mode in ("online", "file", "auto"):
                    with self.subTest(date=date, mode=mode), patch.object(
                        crawler, "FETCH_MODE", mode
                    ):
                        self.assertEqual(crawler.main(date), [])
                        self.assertEqual(crawler.main(date, [100, 200]), [])
            online.assert_not_called()
            local.assert_not_called()

    def test_august_keeps_cumulative_amounts(self):
        for day, total in ((1, 100), (2, 300), (31, 600)):
            with self.subTest(day=day):
                self.assertEqual(
                    crawler.main("2026-08-{:02d}".format(day), [100, 200, 300]),
                    [{"场地": crawler.DEFAULT_VENUE, "兑币机收款": total}],
                )

    def test_august_still_reads_online_and_file(self):
        with patch.object(crawler, "FETCH_MODE", "online"), patch.object(
            crawler, "_fetch_kdocs_amounts", return_value=[100, 200]
        ) as online:
            self.assertEqual(crawler.main("2026-08-02")[0]["兑币机收款"], 300)
            online.assert_called_once_with()
        with patch.object(crawler, "FETCH_MODE", "file"), patch.object(
            crawler, "_load_data_file", return_value=[100, 200]
        ) as local:
            self.assertEqual(crawler.main("2026-08-02")[0]["兑币机收款"], 300)
            local.assert_called_once_with()
