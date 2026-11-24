import unittest

from intake.accounts import AccountResolver

ACCOUNTS = [
    {"id": 1, "name": "招商银行储蓄卡(1111)"},
    {"id": 2, "name": "招商银行储蓄卡(2222/3333)"},
    {"id": 22, "name": "中国银行储蓄卡(1234)"},
    {"id": 24, "name": "汇丰中国(5555/066)"},
    {"id": 19, "name": "微信余额"},
]


def resolver(aliases=None, mapping=None):
    return AccountResolver(ACCOUNTS, {
        "intake": {
            "default_account_id": 1,
            "account_aliases": aliases or {},
        },
        "account_mapping": mapping or {},
    })


class AccountResolverTests(unittest.TestCase):
    def test_contextual_suffix(self):
        self.assertEqual(resolver().resolve("", "尾号1234 收入 1000"), 22)
        self.assertEqual(resolver().resolve("", "卡号1234"), 22)

    def test_parenthesized_suffix(self):
        self.assertEqual(resolver().resolve("", "中国银行储蓄卡(1234)"), 22)

    def test_standalone_suffix(self):
        self.assertEqual(resolver().resolve("", "付款成功 1000.00 1234"), 22)

    def test_full_card_number_uses_last_four(self):
        self.assertEqual(
            resolver().resolve("", "卡号 6217000012341234 收入 1000"), 22)

    def test_amount_and_date_do_not_match(self):
        self.assertEqual(resolver().resolve("", "付款成功 1000.00"), 1)
        self.assertEqual(resolver().resolve("", "2026-11-23 消费 50"), 1)

    def test_alias_wins(self):
        self.assertEqual(resolver({"微信": 2}).resolve("微信", "尾号1234"), 2)

    def test_shared_name_suffix(self):
        self.assertEqual(resolver().resolve("", "尾号3333"), 2)

    def test_bank_short_name_matches(self):
        self.assertEqual(resolver().resolve("", "汇丰 消费 100"), 24)
        self.assertEqual(resolver().resolve("", "hsbc 100"), 24)

    def test_three_digit_contextual_suffix(self):
        self.assertEqual(resolver().resolve("", "汇丰 尾号066 消费 100"), 24)
        self.assertEqual(resolver().resolve("", "尾号066"), 24)
        self.assertEqual(resolver().resolve("", "匯豐 尾號066 消費 100"), 24)

    def test_wallet_short_name_matches(self):
        self.assertEqual(resolver().resolve("", "午饭 35 微信"), 19)

    def test_bank_name_and_suffix_together(self):
        self.assertEqual(resolver().resolve("", "招商银行 尾号2222"), 2)

    def test_account_mapping_fallback(self):
        self.assertEqual(
            resolver(mapping={"9999": 22}).resolve("", "尾号9999"), 22)

    def test_default_when_nothing_matches(self):
        self.assertEqual(resolver().resolve("", "午饭 35"), 1)


if __name__ == "__main__":
    unittest.main()