import unittest
from datetime import datetime
from unittest.mock import patch

from intake.extractor import (_fast_parse, _fallback_parse, _llm_extract,
                              normalize_date, normalize_time)

NOW = datetime(2026, 11, 23, 12, 0, 0)


class FastPathTests(unittest.TestCase):
    def test_merchant_amount_account(self):
        drafts = _fast_parse("午饭 35 微信", NOW)
        self.assertEqual(len(drafts), 1)
        draft = drafts[0]
        self.assertEqual((draft.tx_type, draft.amount), (1, 35.0))
        self.assertEqual(draft.merchant, "午饭")
        self.assertEqual(draft.account_hint, "微信")

    def test_explicit_expense(self):
        drafts = _fast_parse("支出 35 打车", NOW)
        self.assertEqual(len(drafts), 1)
        self.assertEqual((drafts[0].tx_type, drafts[0].amount), (1, 35.0))
        self.assertEqual(drafts[0].merchant, "打车")

    def test_income(self):
        drafts = _fast_parse("收入 12000 工资", NOW)
        self.assertEqual(len(drafts), 1)
        self.assertEqual((drafts[0].tx_type, drafts[0].amount), (2, 12000.0))
        self.assertEqual(drafts[0].merchant, "工资")

    def test_amount_first(self):
        drafts = _fast_parse("¥35 星巴克", NOW)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].merchant, "星巴克")

    def test_refund(self):
        drafts = _fast_parse("退款 25 京东", NOW)
        self.assertEqual(len(drafts), 1)
        self.assertEqual((drafts[0].tx_type, drafts[0].amount), (4, 25.0))

    def test_balance_only_is_ignored(self):
        self.assertEqual(_fast_parse("余额 3769.48", NOW), [])

    def test_long_text_skips_fast_path(self):
        self.assertEqual(_fast_parse("星巴克 32\n" + "x" * 80, NOW), [])


class DateTests(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(normalize_date("昨天", NOW), "2026-11-22")
        self.assertEqual(normalize_date("今天", NOW), "2026-11-23")

    def test_month_day_same_year(self):
        self.assertEqual(normalize_date("11月20日", NOW), "2026-11-20")

    def test_future_month_day_falls_back_to_last_year(self):
        self.assertEqual(normalize_date("12月31日", NOW), "2025-12-31")

    def test_full_date(self):
        self.assertEqual(normalize_date("2026/01/05", NOW), "2026-01-05")
        self.assertEqual(normalize_date("2026-01-05 18:30:00", NOW), "2026-01-05")

    def test_invalid_returns_today(self):
        self.assertEqual(normalize_date("不是日期", NOW), "2026-11-23")

    def test_time(self):
        self.assertEqual(normalize_time("9:5"), "")
        self.assertEqual(normalize_time("9:05"), "09:05:00")
        self.assertEqual(normalize_time("23:59:58"), "23:59:58")


class LlmParseTests(unittest.TestCase):
    def test_parses_transactions(self):
        content = (
            '{"transactions":[{"type":1,"amount":"32.50","currency":"CNY",'
            '"date":"2026-11-22","time":"18:30","merchant":"星巴克",'
            '"account_hint":"支付宝","confidence":0.9},'
            '{"type":4,"amount":8,"currency":"CNY","date":"昨天",'
            '"merchant":"便利店","confidence":0.8}]}'
        )

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"message": {"content": content}}

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, *args, **kwargs):
                return Response()

        with patch("intake.extractor.httpx.Client", Client):
            drafts = _llm_extract("星巴克 32.5，便利店退款 8", {"intake": {}}, NOW)

        self.assertEqual(len(drafts), 2)
        self.assertEqual((drafts[0].tx_type, drafts[0].amount), (1, 32.5))
        self.assertEqual(drafts[0].date, "2026-11-22")
        self.assertEqual(drafts[0].time, "18:30:00")
        self.assertEqual(drafts[0].account_hint, "支付宝")
        self.assertEqual((drafts[1].tx_type, drafts[1].amount), (4, 8.0))
        self.assertEqual(drafts[1].date, "2026-11-22")


class FallbackTests(unittest.TestCase):
    def test_finds_amount_without_currency_word(self):
        drafts = _fallback_parse("今天星巴克32", NOW)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].amount, 32.0)
        self.assertEqual(drafts[0].tx_type, 1)

    def test_empty_without_amount(self):
        self.assertEqual(_fallback_parse("今天天气不错", NOW), [])


if __name__ == "__main__":
    unittest.main()