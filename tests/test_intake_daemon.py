import tempfile
import unittest
from pathlib import Path

from intake.daemon import handle_callback, handle_message
from intake.service import IntakeService
from intake.state import StateStore
from intake.telegram import CallbackQuery, ParsedUpdate


class FakeAPI:
    def __init__(self):
        self.payloads = []
        self.ids = {}

    def get_accounts(self):
        return [{"id": 1, "name": "储蓄卡"}, {"id": 2, "name": "微信钱包"}]

    def get_categories(self):
        return [
            {"id": 10, "name": "餐饮", "type": 1, "parentId": 0, "children": []},
            {"id": 11, "name": "其他支出", "type": 1, "parentId": 0, "children": []},
            {"id": 12, "name": "其他收入", "type": 2, "parentId": 0, "children": []},
        ]

    def get_merchants(self):
        return []

    def create_transaction_with_retry(self, payload):
        self.payloads.append(payload)
        external_id = payload.get("externalId")
        if external_id in self.ids:
            return {"id": self.ids[external_id]}
        tx_id = 100 + len(self.ids) + 1
        self.ids[external_id] = tx_id
        return {"id": tx_id}

    def get_transactions(self, **params):
        return {"list": []}

    def delete_transaction(self, tx_id):
        pass


class FakeClient:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self._message_id = 500

    def send_message(self, chat_id, text, reply_markup=None):
        self._message_id += 1
        self.sent.append({
            "chat_id": chat_id, "text": text,
            "reply_markup": reply_markup, "message_id": self._message_id,
        })
        return self._message_id

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({
            "chat_id": chat_id, "message_id": message_id,
            "text": text, "reply_markup": reply_markup,
        })

    def answer_callback_query(self, callback_id, text=""):
        self.answers.append((callback_id, text))


def config():
    return {
        "category_mapping": {},
        "fallback_expense_category_id": 11,
        "fallback_income_category_id": 12,
        "intake": {
            "account_aliases": {"微信": 2},
            "duplicate_check": True,
            "confirm_ttl_minutes": 30,
        },
    }


def token_from(client):
    markup = client.sent[-1]["reply_markup"]
    return markup["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]


class DaemonFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.json")
        self.api = FakeAPI()
        self.service = IntakeService(self.api, config())
        self.client = FakeClient()

    def tearDown(self):
        self.tmp.cleanup()

    def preview(self, text="午饭 35 微信", message_id=100):
        parsed = ParsedUpdate(update_id=1, chat_id=42, message_id=message_id, text=text)
        handle_message(self.client, self.service, self.state, {42}, parsed, 1800)
        return token_from(self.client)

    def test_message_previews_without_creating(self):
        token = self.preview()

        self.assertEqual(self.api.payloads, [])
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("待确认", self.client.sent[0]["text"])
        buttons = self.client.sent[0]["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["text"] for b in buttons[0]],
                         ["✅ 确认入账", "🕓 标为待确认", "❌ 取消"])

        entry = self.state.get_pending(token)
        self.assertEqual(entry["source_message_id"], "100")
        self.assertEqual(entry["message_id"], "501")

    def test_confirm_callback_creates_and_removes_preview(self):
        token = self.preview()
        callback = CallbackQuery(id="cb1", chat_id=42, message_id=501,
                                 data=f"cf:{token}")

        handle_callback(self.client, self.service, self.state, {42}, callback, 1800)

        self.assertEqual(len(self.api.payloads), 1)
        self.assertTrue(self.api.payloads[0]["isConfirmed"])
        self.assertIsNone(self.state.get_pending(token))
        self.assertIn("已入账", self.client.edits[-1]["text"])
        self.assertEqual(self.client.edits[-1]["reply_markup"], {"inline_keyboard": []})
        self.assertIn(("cb1", ""), self.client.answers)

    def test_pending_callback_creates_unconfirmed(self):
        token = self.preview()
        callback = CallbackQuery(id="cb2", chat_id=42, message_id=501,
                                 data=f"pd:{token}")

        handle_callback(self.client, self.service, self.state, {42}, callback, 1800)

        self.assertFalse(self.api.payloads[0]["isConfirmed"])
        self.assertIn("已记为待确认", self.client.edits[-1]["text"])
        self.assertIn("不影响余额", self.client.edits[-1]["text"])

    def test_cancel_callback_writes_nothing(self):
        token = self.preview()
        self.state.set_editing(42, "100")
        callback = CallbackQuery(id="cb3", chat_id=42, message_id=501,
                                 data=f"cx:{token}")

        handle_callback(self.client, self.service, self.state, {42}, callback, 1800)

        self.assertEqual(self.api.payloads, [])
        self.assertIsNone(self.state.get_pending(token))
        self.assertIsNone(self.state.editing_message_id(42))
        self.assertIn("已取消", self.client.edits[-1]["text"])

    def test_edit_callback_keeps_source_message_id(self):
        token = self.preview()
        callback = CallbackQuery(id="cb4", chat_id=42, message_id=501,
                                 data=f"ed:{token}")
        handle_callback(self.client, self.service, self.state, {42}, callback, 1800)

        self.assertEqual(self.state.editing_message_id(42), "100")
        self.assertIn("请发送修正后的内容", self.client.edits[-1]["text"])

        corrected = ParsedUpdate(update_id=2, chat_id=42, message_id=101,
                                 text="午饭 36 微信")
        handle_message(self.client, self.service, self.state, {42}, corrected, 1800)

        new_token = token_from(self.client)
        entry = self.state.get_pending(new_token)
        payload = entry["result"]["items"][0]["payload"]
        self.assertEqual(payload["externalId"], "chat:telegram:42:100")
        self.assertEqual(payload["amount"], 36.0)

    def test_confirm_command(self):
        self.preview()
        command = ParsedUpdate(update_id=2, chat_id=42, message_id=102, text="/confirm")
        handle_message(self.client, self.service, self.state, {42}, command, 1800)

        self.assertEqual(len(self.api.payloads), 1)
        self.assertIn("已入账", self.client.edits[-1]["text"])

    def test_second_confirm_is_rejected(self):
        token = self.preview()
        callback = CallbackQuery(id="cb5", chat_id=42, message_id=501,
                                 data=f"cf:{token}")
        handle_callback(self.client, self.service, self.state, {42}, callback, 1800)

        again = CallbackQuery(id="cb6", chat_id=42, message_id=501,
                              data=f"cf:{token}")
        handle_callback(self.client, self.service, self.state, {42}, again, 1800)

        self.assertEqual(len(self.api.payloads), 1)
        self.assertEqual(self.client.answers[-1], ("cb6", "预览已失效或已处理"))
        self.assertIn("已失效", self.client.edits[-1]["text"])

    def test_non_allowlisted_chat_is_ignored(self):
        parsed = ParsedUpdate(update_id=1, chat_id=99, message_id=1, text="午饭 35")
        handle_message(self.client, self.service, self.state, {42}, parsed, 1800)

        self.assertEqual(self.api.payloads, [])
        self.assertEqual(self.client.sent, [])


if __name__ == "__main__":
    unittest.main()