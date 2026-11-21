import unittest

from email_parser import Transaction
from main import _resolve_account, build_entries


def _transfer(suffix: str, target: str, amount: float = 8724.0) -> Transaction:
    return Transaction(
        account_suffix=suffix, target_account_suffix=target,
        date="2026-11-06", time="18:29:00", merchant="资金归集",
        amount=amount, balance=77972.84, tx_type=3,
    )


class AccountResolveTests(unittest.TestCase):
    def test_fixed_deposit_suffix(self):
        self.assertEqual(
            _resolve_account("FIXED_DEPOSIT", {}, None, 6, 15), 15)

    def test_invest_suffix(self):
        self.assertEqual(_resolve_account("INVEST", {}, None, 6, 15), 6)

    def test_card_mapping(self):
        self.assertEqual(
            _resolve_account("0611", {"0611": 2}, None, 6, 15), 2)


class ProvidentFundTests(unittest.TestCase):
    def test_synthesizes_income_before_sweep(self):
        entries = build_entries([_transfer("3416", "0611")], 100, "3416")

        self.assertEqual([e[0] for e in entries], ["cmb:100:pf:0", "cmb:100:0"])
        pf = entries[0][1]
        self.assertEqual((pf.tx_type, pf.account_suffix, pf.amount), (2, "3416", 8724.0))
        self.assertEqual(pf.merchant, "公积金入账")
        self.assertEqual(entries[1][1].tx_type, 3)

    def test_synthesizes_once_per_transfer(self):
        txs = [_transfer("3416", "0611", 100.0), _transfer("3416", "0611", 200.0)]
        entries = build_entries(txs, 101, "3416")

        self.assertEqual([e[0] for e in entries],
                         ["cmb:101:pf:0", "cmb:101:0", "cmb:101:pf:1", "cmb:101:1"])

    def test_does_not_apply_to_other_cards(self):
        entries = build_entries([_transfer("0611", "0886")], 102, "3416")
        self.assertEqual([e[0] for e in entries], ["cmb:102:0"])

    def test_ignores_non_transfer_on_pf_card(self):
        tx = Transaction(
            account_suffix="3416", date="2026-11-06", time="09:00:00",
            merchant="消费", amount=10.0, balance=0.0, tx_type=1,
        )
        entries = build_entries([tx], 103, "3416")
        self.assertEqual([e[0] for e in entries], ["cmb:103:0"])


if __name__ == "__main__":
    unittest.main()