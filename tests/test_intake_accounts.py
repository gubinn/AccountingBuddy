import unittest

from intake.accounts import AccountResolver

ACCOUNTS = [
    {"id": 1, "name": "招商银行储蓄卡(1111)"},
    {"id": 2, "name": "招商银行储蓄卡(2222/3333)"},
    {"id": 22, "name": "中国银行储蓄卡(1234)"},
]


def resolver(aliases=None):
    return AccountResolver(ACCOUNTS, {
        "intake": {
            "default_account_id": 1,
            "account_aliases": aliases or {},
        }
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

    def test_default_when_nothing_matches(self):
        self.assertEqual(resolver().resolve("", "午饭 35"), 1)


if __name__ == "__main__":
    unittest.main()