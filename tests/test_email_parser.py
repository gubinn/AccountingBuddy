import unittest
from datetime import datetime

from email_parser import parse_email


class ParserTests(unittest.TestCase):
    def test_standard_consumption(self):
        html = ("<html><body><p>您账户8338于04月15日01:34在财付通-微信支付-"
                "与秀同行图书快捷支付49.00元，余额3769.48</p></body></html>")
        txs = parse_email(html, ref_date=datetime(2026, 4, 15, 10, 0))
        self.assertEqual(len(txs), 1)
        tx = txs[0]
        self.assertEqual(tx.account_suffix, "8338")
        self.assertEqual(tx.date, "2026-04-15")
        self.assertEqual(tx.time, "01:34:00")
        self.assertEqual(tx.amount, 49.0)
        self.assertEqual(tx.tx_type, 1)

    def test_cross_year_uses_email_year(self):
        # Email arrives on 2026-01-05 describing a 12-31 transaction
        html = "<html><body>您账户0611于12月31日23:10在美团消费30.00元，余额100.00</body></html>"
        txs = parse_email(html, ref_date=datetime(2026, 1, 5, 8, 0))
        self.assertEqual(txs[0].date, "2025-12-31")

    def test_income(self):
        html = ("<html><body>您账户0611于04月10日09:15入账工资，人民币8000.00，"
                "余额人民币9000.00</body></html>")
        txs = parse_email(html, ref_date=datetime(2026, 4, 10, 10, 0))
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0].tx_type, 2)
        self.assertEqual(txs[0].amount, 8000.0)

    def test_deduction_has_second_precision(self):
        html = ("<html><body>您账户3416于2026年04月01日03:04:05扣款人民币120.00，"
                "商户：某某保险，余额人民币5000.00</body></html>")
        txs = parse_email(html, ref_date=datetime(2026, 4, 2, 8, 0))
        self.assertEqual(txs[0].time, "03:04:05")
        self.assertEqual(txs[0].merchant, "某某保险")

    def test_auto_transfer_uses_email_datetime(self):
        html = ("<html><body>您账户0062定制的自动转账执行成功（收款人张三，"
                "收款账号6222000011112222，金额500.00），账户余额1000.00</body></html>")
        txs = parse_email(html, ref_date=datetime(2026, 4, 19, 12, 34, 56))
        self.assertEqual(txs[0].date, "2026-04-19")
        self.assertEqual(txs[0].time, "12:34:56")

    def test_payload_truncates_note_and_merchant(self):
        html = ("<html><body>您账户0611于04月10日09:15入账工资，人民币8000.00，"
                "余额人民币9000.00" + "备注" * 200 + "</body></html>")
        txs = parse_email(html, ref_date=datetime(2026, 4, 10, 10, 0))
        payload = txs[0].to_api_payload(1, 2, external_id="cmb:9:0")
        self.assertLessEqual(len(payload["note"]), 200)
        self.assertEqual(payload["externalId"], "cmb:9:0")
        self.assertNotIn("merchantName", payload)  # income does not send merchant

    def test_outgoing_and_incoming_transfers(self):
        out = ("<html><body>您账户0611于02月28日15:54转账汇款人民币2000.00，"
               "余额253598.42，收款人：顾斌，请以收款人实际入账为准</body></html>")
        tx = parse_email(out, ref_date=datetime(2026, 2, 28, 16, 0))[0]
        self.assertEqual((tx.tx_type, tx.amount, tx.merchant), (1, 2000.0, "转账-顾斌"))

        incoming = ("<html><body>您账户8338于02月28日收到本行转入人民币2000.00，"
                    "余额5000.00，付方顾斌（0611），备注：转账</body></html>")
        tx = parse_email(incoming, ref_date=datetime(2026, 2, 28, 16, 0))[0]
        self.assertEqual((tx.tx_type, tx.amount, tx.merchant), (2, 2000.0, "转入-顾斌"))

        interbank = ("<html><body>您账户0611于05月12日他行实时转入人民币14600.00，"
                     "余额349050.98，付方苏商银行瓜子汽车服务</body></html>")
        tx = parse_email(interbank, ref_date=datetime(2026, 5, 12, 12, 0))[0]
        self.assertEqual((tx.tx_type, tx.amount), (2, 14600.0))

    def test_credit_card_repayment_is_transfer(self):
        html = ("<html><body>您账户0611于03月26日15:49向尾号为0062的信用卡还款"
                "人民币3627.50元，余额298911.41元。</body></html>")
        tx = parse_email(html, ref_date=datetime(2026, 3, 26, 16, 0))[0]
        self.assertEqual(tx.tx_type, 3)
        self.assertEqual(tx.target_account_suffix, "0062")
        self.assertEqual(tx.amount, 3627.5)

    def test_unionpay_and_generic_credit(self):
        up_in = ("<html><body>您账户0611于04月10日11:05银联入账人民币20836.57元，"
                 "余额261441.98元（顾斌）</body></html>")
        tx = parse_email(up_in, ref_date=datetime(2026, 4, 10, 12, 0))[0]
        self.assertEqual((tx.tx_type, tx.merchant), (2, "银联入账-顾斌"))

        up_out = ("<html><body>您账户0611于04月25日13:12银联扣款人民币3400.00元，"
                  "余额283313.14元（银联在线支付）</body></html>")
        tx = parse_email(up_out, ref_date=datetime(2026, 4, 25, 14, 0))[0]
        self.assertEqual((tx.tx_type, tx.amount), (1, 3400.0))

        credit = ("<html><body>您账户0611于07月03日14:36入账30000.00元，"
                  "余额580763.79元（云闪付转账/手机转账）</body></html>")
        tx = parse_email(credit, ref_date=datetime(2026, 7, 3, 15, 0))[0]
        self.assertEqual((tx.tx_type, tx.amount), (2, 30000.0))

    def test_deduction_with_purpose(self):
        html = ("<html><body>您账户0611于05月16日21:35扣款人民币25.49元，"
                "用于数字人民币随用随充消费， 余额348824.19元。</body></html>")
        tx = parse_email(html, ref_date=datetime(2026, 5, 16, 22, 0))[0]
        self.assertEqual(tx.amount, 25.49)
        self.assertIn("数字人民币", tx.merchant)

    def test_credit_card_refund_is_income(self):
        html = ("<html><body><p>您的消费明细如下：</p>"
                "<p>01:10:12</p><p>CNY -2.00</p>"
                "<p>尾号0886 退货 支付宝-北京三快在线科技有限公司</p>"
                "<p>12:00:00</p><p>CNY 10.00</p><p>尾号0886 消费 美团</p>"
                "</body></html>")
        txs = parse_email(html, ref_date=datetime(2026, 7, 13, 8, 0))
        self.assertEqual(len(txs), 2)
        self.assertEqual((txs[0].tx_type, txs[0].amount), (2, 2.0))
        self.assertEqual((txs[1].tx_type, txs[1].amount), (1, 10.0))

    def test_apple_pay_payment(self):
        html = ("<html><body>您账户8338（设备卡号0763）于09月15日17:00线上支付人民币128.59，"
                "余额77.20，Apple Pay, 线上支付消费，Octopus Cards Limited</body></html>")
        tx = parse_email(html, ref_date=datetime(2026, 9, 15, 18, 0))[0]
        self.assertEqual(tx.date, "2026-09-15")
        self.assertEqual(tx.amount, 128.59)
        self.assertEqual(tx.tx_type, 1)
        self.assertIn("Octopus", tx.merchant)

    def test_asset_movements(self):
        cd = ("<html><body>您账户0611大额存单于06月05日到期转入活期，"
              "人民币本金200000.00元、利息8719.44元，活期余额511661.17元，"
              "产品代码CMBR20240005。</body></html>")
        txs = parse_email(cd, ref_date=datetime(2026, 6, 5, 9, 0))
        self.assertEqual(len(txs), 2)
        self.assertEqual((txs[0].tx_type, txs[0].account_suffix,
                          txs[0].target_account_suffix, txs[0].amount),
                         (3, "INVEST", "0611", 200000.0))
        self.assertEqual((txs[1].tx_type, txs[1].merchant, txs[1].amount),
                         (2, "大额存单利息", 8719.44))

        to_sec = ("<html><body>您账户0611于08月18日发生银行活期转证券保证金"
                  "人民币650000.00，余额1569.23</body></html>")
        tx = parse_email(to_sec, ref_date=datetime(2026, 8, 18, 10, 0))[0]
        self.assertEqual((tx.tx_type, tx.target_account_suffix, tx.amount),
                         (3, "INVEST", 650000.0))

        from_sec = ("<html><body>您账户0611于09月29日09:41发生证券保证金转银行活期"
                    "人民币200573.13，余额215307.79</body></html>")
        tx = parse_email(from_sec, ref_date=datetime(2026, 9, 29, 10, 0))[0]
        self.assertEqual((tx.tx_type, tx.account_suffix, tx.target_account_suffix),
                         (3, "INVEST", "0611"))

        fx = ("<html><body>您账户0611于09月26日10:52发生自助购汇，购汇港币现汇1000.00，"
              "本次购汇人民币减少857.80，人民币余额43213.66</body></html>")
        tx = parse_email(fx, ref_date=datetime(2026, 9, 26, 11, 0))[0]
        self.assertEqual((tx.tx_type, tx.target_account_suffix, tx.amount),
                         (3, "INVEST", 857.80))

    def test_payload_omits_invalid_time(self):
        html = ("<html><body>您账户0062定制的自动转账执行成功（收款人张三，"
                "收款账号6222000011112222，金额500.00），账户余额1000.00</body></html>")
        tx = parse_email(html, ref_date=datetime(2026, 4, 19, 12, 34, 56))[0]
        tx.time = ""
        payload = tx.to_api_payload(1, 261)
        self.assertNotIn("transactionTime", payload)


if __name__ == "__main__":
    unittest.main()