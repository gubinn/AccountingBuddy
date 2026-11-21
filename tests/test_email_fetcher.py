import json
import os
import tempfile
import unittest
from email.message import EmailMessage
from unittest import mock

import email_fetcher

CMB_RAW = None


def _build_cmb_email() -> bytes:
    msg = EmailMessage()
    msg["From"] = "95555@message.cmbchina.com"
    msg["Subject"] = "消费通知"
    msg["Date"] = "Sun, 19 Apr 2026 12:34:56 +0800"
    msg.set_content("您账户8338于04月19日12:30在美团消费30.00元，余额100.00")
    return msg.as_bytes()


class FakeClient:
    def __init__(self, search_batches=None, fetch_response=None):
        self.search_batches = list(search_batches or [])
        self.fetch_response = fetch_response or {}
        self.search_calls = []
        self.flags = []
        self.readonly = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        pass

    def id_(self, info):
        pass

    def select_folder(self, folder, readonly=False):
        self.readonly = readonly

    def search(self, criteria):
        self.search_calls.append(criteria)
        return self.search_batches.pop(0) if self.search_batches else []

    def fetch(self, uids, parts):
        return self.fetch_response

    def set_flags(self, uids, flags):
        self.flags.append((list(uids), flags))


class HeaderDecodeTests(unittest.TestCase):
    def test_decodes_rfc2047_string_and_bytes(self):
        encoded = "=?utf-8?B?5q+P5pel5L+h55So566h5a62?="
        self.assertEqual(email_fetcher._decode_header_value(encoded), "每日信用管家")
        self.assertEqual(email_fetcher._decode_header_value(encoded.encode()), "每日信用管家")

    def test_plain_header_passthrough(self):
        self.assertEqual(email_fetcher._decode_header_value("95555@cmbchina.com"),
                         "95555@cmbchina.com")


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.patch = mock.patch.object(email_fetcher, "STATE_FILE", self.state_path)
        self.patch.start()
        self.user = "me@163.com"
        self.folder = "INBOX"
        self.key = f"{self.user}:{self.folder}"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _write_state(self, uid):
        with open(self.state_path, "w") as f:
            json.dump({self.key: uid}, f)

    def _read_state(self):
        with open(self.state_path) as f:
            return json.load(f)

    def _fetch(self, client, full_sync=False, dry_run=False):
        with mock.patch.object(email_fetcher, "IMAPClient", return_value=client):
            return email_fetcher.fetch_emails(
                host="imap.163.com", port=993, user=self.user, password="x",
                folder=self.folder, senders=["95555@message.cmbchina.com"],
                full_sync=full_sync, dry_run=dry_run,
            )

    def test_first_run_initialises_cursor_without_replay(self):
        client = FakeClient(search_batches=[[100, 101, 102]])
        emails, max_uid = self._fetch(client)

        self.assertEqual(emails, [])
        self.assertEqual(max_uid, 102)
        self.assertEqual(self._read_state()[self.key], 102)
        # Must scan ALL once to find the current maximum
        self.assertEqual(client.search_calls, [[b"ALL"]])

    def test_incremental_uses_uid_range_and_returns_metadata(self):
        self._write_state(102)
        raw = _build_cmb_email()
        client = FakeClient(search_batches=[[103]], fetch_response={103: {b"RFC822": raw}})

        emails, max_uid = self._fetch(client)

        self.assertEqual(max_uid, 103)
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0]["uid"], 103)
        self.assertIn("2026-04-19T12:34:56", emails[0]["date"])
        self.assertEqual(client.search_calls, [[b"UID", b"103:*"]])
        self.assertTrue(client.readonly)
        self.assertEqual(client.flags, [])  # fetch must not mark anything

    def test_dry_run_does_not_initialise_state(self):
        client = FakeClient(search_batches=[[100, 101]])
        emails, max_uid = self._fetch(client, dry_run=True)

        self.assertEqual(emails, [])
        self.assertEqual(max_uid, 101)
        self.assertFalse(os.path.exists(self.state_path))

    def test_mark_processed_advances_cursor_and_sets_seen(self):
        self._write_state(102)
        client = FakeClient(search_batches=[[]])
        with mock.patch.object(email_fetcher, "IMAPClient", return_value=client):
            email_fetcher.mark_processed(
                host="imap.163.com", port=993, user=self.user, password="x",
                folder=self.folder, uids=[103, 104], state_uid=104,
            )

        self.assertEqual(self._read_state()[self.key], 104)
        self.assertEqual(client.flags, [([103, 104], [b"\\Seen"])])
        self.assertFalse(client.readonly)

    def test_mark_processed_without_uids_only_advances_cursor(self):
        self._write_state(102)
        with mock.patch.object(email_fetcher, "IMAPClient") as imap:
            email_fetcher.mark_processed(
                host="imap.163.com", port=993, user=self.user, password="x",
                folder=self.folder, uids=[], state_uid=110,
            )

        self.assertEqual(self._read_state()[self.key], 110)
        imap.assert_not_called()


if __name__ == "__main__":
    unittest.main()