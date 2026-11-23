import json
import tempfile
import unittest
from pathlib import Path

from intake.state import StateStore
from intake.telegram import parse_callback, parse_update


class ParseUpdateTests(unittest.TestCase):
    def test_text_message(self):
        parsed = parse_update({
            "update_id": 7,
            "message": {
                "message_id": 11,
                "date": 1763900000,
                "chat": {"id": 42},
                "from": {"is_bot": False},
                "text": "午饭 35",
            },
        })
        self.assertEqual(parsed.update_id, 7)
        self.assertEqual(parsed.chat_id, 42)
        self.assertEqual(parsed.message_id, 11)
        self.assertEqual(parsed.text, "午饭 35")
        self.assertFalse(parsed.from_bot)

    def test_photo_picks_largest(self):
        parsed = parse_update({
            "update_id": 8,
            "message": {
                "message_id": 12,
                "chat": {"id": 42},
                "from": {"is_bot": False},
                "caption": "午饭",
                "photo": [
                    {"file_id": "small", "width": 90, "height": 90},
                    {"file_id": "large", "width": 800, "height": 800},
                ],
            },
        })
        self.assertEqual(parsed.image_file_id, "large")
        self.assertEqual(parsed.caption, "午饭")

    def test_image_document(self):
        parsed = parse_update({
            "update_id": 9,
            "message": {
                "message_id": 13,
                "chat": {"id": 42},
                "from": {"is_bot": False},
                "document": {"file_id": "doc", "mime_type": "image/png"},
            },
        })
        self.assertEqual(parsed.image_file_id, "doc")

    def test_non_message_update_is_ignored(self):
        self.assertIsNone(parse_update({"update_id": 10, "edited_message": {}}))


class ParseCallbackTests(unittest.TestCase):
    def test_callback_query(self):
        callback = parse_callback({
            "update_id": 11,
            "callback_query": {
                "id": "cb-1",
                "from": {"is_bot": False},
                "data": "cf:abc123",
                "message": {"message_id": 21, "chat": {"id": 42}},
            },
        })
        self.assertEqual(callback.id, "cb-1")
        self.assertEqual(callback.chat_id, 42)
        self.assertEqual(callback.message_id, 21)
        self.assertEqual(callback.data, "cf:abc123")

    def test_non_callback_update_is_ignored(self):
        self.assertIsNone(parse_callback({"update_id": 12, "message": {}}))


class StateStoreTests(unittest.TestCase):
    def test_roundtrip_offset_and_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path)
            store.set_offset(15)
            store.remember(42, [1, 2])

            reloaded = StateStore(path)
            self.assertEqual(reloaded.offset, 15)
            self.assertEqual(reloaded.take_last(42), [1, 2])
            self.assertEqual(reloaded.take_last(42), [])
            self.assertEqual(json.loads(path.read_text())["telegram_offset"], 15)

    def test_pending_roundtrip_and_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            first = store.add_pending(42, 100, {"text": "a"})
            second = store.add_pending(42, 101, {"text": "b"})

            self.assertEqual(store.get_pending(first)["message_id"], "100")
            token, entry = store.latest_pending(42)
            self.assertEqual(token, second)
            self.assertEqual(entry["result"]["text"], "b")

            store.remove_pending(second)
            self.assertIsNone(store.get_pending(second))

    def test_pending_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            token = store.add_pending(42, 100, {"text": "a"}, ttl_seconds=-1)
            self.assertIsNone(store.get_pending(token, ttl_seconds=-1))
            self.assertIsNone(store.latest_pending(42, ttl_seconds=-1))

    def test_pending_is_bounded_per_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            tokens = [store.add_pending(42, i, {"text": str(i)}, max_per_chat=2)
                      for i in range(3)]
            self.assertIsNone(store.get_pending(tokens[0]))
            self.assertIsNotNone(store.get_pending(tokens[1]))
            self.assertIsNotNone(store.get_pending(tokens[2]))

    def test_editing_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            store.set_editing(42, 100)
            self.assertEqual(store.editing_message_id(42), "100")
            store.set_editing(42, None)
            self.assertIsNone(store.editing_message_id(42))


if __name__ == "__main__":
    unittest.main()