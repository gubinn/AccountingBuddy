import unittest

from annotations import annotations_for, derive_tags, detect_channel
from email_parser import Transaction


class ChannelTests(unittest.TestCase):
    def test_channels(self):
        cases = {
            "支付宝-北京三快在线科技有限公司": "支付宝",
            "财付通-微信支付-美团快捷支付": "微信支付",
            "京东支付-京东自营": "京东支付",
            "Apple Pay, 线上支付消费，Octopus Cards Limited": "Apple Pay",
            "银联扣款-银联在线支付": "银联",
            "程支付-携程旅行网": "程支付",
            "抖音支付-某商户": "抖音支付",
            "转账-张三": None,
        }
        for merchant, expected in cases.items():
            self.assertEqual(detect_channel(merchant), expected, merchant)

    def test_channel_falls_back_to_note(self):
        self.assertEqual(detect_channel("", "支付宝-某商户"), "支付宝")


class TagTests(unittest.TestCase):
    def test_transfer_tags(self):
        self.assertEqual(
            derive_tags(3, "资金归集(尾号3416->尾号0611)", "资金归集执行成功"),
            ["内部转账", "资金归集"],
        )
        self.assertEqual(
            derive_tags(3, "信用卡还款(尾号0062)", "向尾号为0062的信用卡还款"),
            ["内部转账", "信用卡还款"],
        )
        self.assertEqual(derive_tags(3, "转账-张三", "转账汇款"), ["内部转账"])

    def test_method_tags(self):
        self.assertEqual(
            derive_tags(1, "转账-李四", "自动转账执行成功（收款人李四）"),
            ["自动转账"],
        )
        self.assertEqual(
            derive_tags(1, "某保险", "扣款人民币120.00，商户：某保险"),
            ["自动扣款"],
        )
        # 银联扣款是主动支付，不是自动扣款
        self.assertEqual(
            derive_tags(1, "银联扣款-某商户", "银联扣款人民币27.92元"),
            [],
        )

    def test_no_duplicate_tags(self):
        self.assertEqual(derive_tags(3, "信用卡还款", "信用卡还款 信用卡还款"),
                         ["内部转账", "信用卡还款"])


class AnnotationForTests(unittest.TestCase):
    def test_annotations_for_transaction(self):
        tx = Transaction(
            account_suffix="0611", date="2026-11-06", time="18:29:00",
            merchant="资金归集(尾号3416->尾号0611)", amount=8724.0,
            balance=None, raw_text="资金归集执行成功", tx_type=3,
        )
        tags, ext = annotations_for(tx)
        self.assertEqual(tags, ["内部转账", "资金归集"])
        self.assertEqual(ext, {})


if __name__ == "__main__":
    unittest.main()