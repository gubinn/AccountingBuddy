import unittest
from unittest.mock import patch

from intake.models import DraftTransaction
from intake.service import (IntakeService, deserialize_result,
                            serialize_result)


class FakeAPI:
    def __init__(self, existing=None):
        self.payloads = []
        self.deleted = []
        self.existing = existing or []

    def get_accounts(self):
        return [
            {"id": 1, "name": "招商银行储蓄卡(1111)"},
            {"id": 2, "name": "微信钱包"},
            {"id": 22, "name": "中国银行储蓄卡(1234)"},
        ]

    def get_categories(self):
        return [
            {"id": 10, "name": "餐饮", "type": 1, "parentId": 0, "children": []},
            {"id": 11, "name": "其他支出", "type": 1, "parentId": 0, "children": []},
            {"id": 12, "name": "其他收入", "type": 2, "parentId": 0, "children": []},
            {"id": 1324, "name": "房租收入", "type": 2, "parentId": 179,
             "children": []},
        ]

    def get_merchants(self):
        return []

    def create_transaction_with_retry(self, payload):
        self.payloads.append(payload)
        return {"id": 100 + len(self.payloads)}

    def get_transactions(self, **params):
        return {"list": self.existing}

    def delete_transaction(self, tx_id):
        self.deleted.append(tx_id)


def config():
    return {
        "category_mapping": {},
        "fallback_expense_category_id": 11,
        "fallback_income_category_id": 12,
        "intake": {
            "account_aliases": {"微信": 2},
            "duplicate_check": True,
            "duplicate_lookback_days": 1,
        },
    }


class ProcessTests(unittest.TestCase):
    def test_creates_confirmed_transaction(self):
        api = FakeAPI()
        service = IntakeService(api, config())

        result = service.process("午饭 35 微信", channel="telegram",
                                 chat_id="42", message_id="100")

        self.assertEqual(len(result.created), 1)
        created = result.created[0]
        self.assertTrue(created.confirmed)
        self.assertEqual(created.account_id, 2)
        self.assertEqual(created.category_id, 11)
        self.assertEqual(created.tx_id, 101)

        payload = api.payloads[0]
        self.assertEqual(payload["externalId"], "chat:telegram:42:100")
        self.assertEqual((payload["type"], payload["amount"]), (1, 35.0))
        self.assertEqual(payload["accountId"], 2)
        self.assertEqual(payload["categoryId"], 11)
        self.assertTrue(payload["isConfirmed"])
        self.assertEqual(payload["merchantName"], "午饭")
        self.assertEqual(payload["metadata"]["message_id"], "100")

        reply = service.format_result(result)
        self.assertIn("已入账", reply)
        self.assertIn("#101", reply)
        self.assertIn("微信钱包", reply)

    def test_prepare_does_not_write(self):
        api = FakeAPI()
        service = IntakeService(api, config())

        result = service.prepare("午饭 35 微信", channel="telegram",
                                 chat_id="42", message_id="99")

        self.assertEqual(api.payloads, [])
        self.assertEqual(len(result.created), 1)
        self.assertEqual(result.created[0].tx_id, None)
        self.assertEqual(result.created[0].payload["externalId"],
                         "chat:telegram:42:99")
        preview = service.format_preview(result)
        self.assertIn("待确认", preview)
        self.assertIn("确认后才会写入账本", preview)
        self.assertNotIn("#", preview)

    def test_duplicate_is_warned_but_explicit_confirmation_wins(self):
        api = FakeAPI(existing=[{
            "id": 900, "type": 1, "amount": 35.0, "accountId": 2,
            "transactionDate": "2026-11-23",
        }])
        service = IntakeService(api, config())

        prepared = service.prepare("午饭 35 微信", channel="telegram",
                                   chat_id="42", message_id="101")
        self.assertEqual(prepared.created[0].duplicate_of, 900)
        self.assertEqual(api.payloads, [])
        self.assertIn("已有相似交易 #900", service.format_preview(prepared))

        result = service.commit(prepared, confirmed=True)
        self.assertTrue(api.payloads[0]["isConfirmed"])
        self.assertEqual(result.created[0].tx_id, 101)
        self.assertIn("金额相同", service.format_result(result))

    def test_commit_pending_marks_transaction_unconfirmed(self):
        api = FakeAPI()
        service = IntakeService(api, config())
        prepared = service.prepare("午饭 35 微信", channel="telegram",
                                   chat_id="42", message_id="102")

        result = service.commit(prepared, confirmed=False)

        self.assertFalse(api.payloads[0]["isConfirmed"])
        self.assertFalse(result.created[0].confirmed)
        self.assertIn("已记为待确认", service.format_result(result))

    def test_multiple_drafts_get_indexed_external_ids(self):
        api = FakeAPI()
        service = IntakeService(api, config())
        drafts = [
            DraftTransaction(tx_type=1, amount=10, date="2026-11-23", merchant="A"),
            DraftTransaction(tx_type=1, amount=20, date="2026-11-23", merchant="B"),
        ]

        with patch("intake.service.extract", return_value=drafts):
            result = service.process("两张小票", channel="telegram",
                                     chat_id="42", message_id="102")

        self.assertEqual([p["externalId"] for p in api.payloads], [
            "chat:telegram:42:102:0",
            "chat:telegram:42:102:1",
        ])
        self.assertEqual(len(result.created), 2)

    def test_no_transaction_reply(self):
        service = IntakeService(FakeAPI(), config())
        result = service.process("今天天气不错", channel="telegram",
                                 chat_id="42", message_id="103")
        self.assertEqual(result.created, [])
        self.assertIn("没识别出交易", service.format_result(result))

    def test_card_suffix_and_explicit_income_category(self):
        api = FakeAPI()
        service = IntakeService(api, config())

        result = service.prepare("房租收入 1000 卡号1234", channel="telegram",
                                 chat_id="42", message_id="105")

        item = result.created[0]
        self.assertEqual(item.account_id, 22)
        self.assertEqual(item.category_id, 1324)
        self.assertEqual(item.draft.tx_type, 2)
        self.assertEqual(item.draft.amount, 1000.0)

    def test_caption_merchant_fallback(self):
        self.assertEqual(
            IntakeService._caption_merchant("聊天记账", "房租收入"), "房租")
        self.assertEqual(
            IntakeService._caption_merchant("聊天记账", "房租收入 1000"), "房租")
        self.assertEqual(
            IntakeService._caption_merchant("聊天记账", "今天在星巴克花了32块"),
            "聊天记账")
        self.assertEqual(
            IntakeService._caption_merchant("星巴克", "房租收入"), "星巴克")

    def test_prepared_result_roundtrips_through_state_json(self):
        service = IntakeService(FakeAPI(), config())
        result = service.prepare("午饭 35 微信", channel="telegram",
                                 chat_id="42", message_id="104")

        restored = deserialize_result(serialize_result(result))

        self.assertEqual(restored.text, result.text)
        self.assertEqual(len(restored.created), 1)
        item = restored.created[0]
        self.assertEqual(item.external_id, "chat:telegram:42:104")
        self.assertEqual(item.payload["amount"], 35.0)
        self.assertEqual(item.account_name, "微信钱包")
        self.assertEqual(item.draft.merchant, "午饭")

    def test_undo_deletes_ids(self):
        api = FakeAPI()
        service = IntakeService(api, config())
        deleted, failed = service.undo([1, 2])
        self.assertEqual(deleted, [1, 2])
        self.assertEqual(failed, [])
        self.assertEqual(api.deleted, [1, 2])


if __name__ == "__main__":
    unittest.main()