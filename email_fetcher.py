"""Fetch emails from 163 IMAP, filter by CMB senders, track processed UIDs."""

import logging
import json
import os
import email as email_module
from email.header import decode_header

from imapclient import IMAPClient

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


def load_state() -> dict[str, int]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_state(state: dict[str, int]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _decode_header_value(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _get_email_body(msg: email_module.message.Message) -> str:
    html_body = ""
    plain_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if content_type == "text/html":
                html_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
            elif content_type == "text/plain":
                plain_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            if content_type == "text/html":
                html_body = payload.decode("utf-8", errors="replace")
            elif content_type == "text/plain":
                plain_body = payload.decode("utf-8", errors="replace")
        else:
            payload = msg.get_payload()
            if content_type == "text/html":
                html_body = str(payload)
            elif content_type == "text/plain":
                plain_body = str(payload)

    return html_body or plain_body


def fetch_emails(host: str, port: int, user: str, password: str,
                 folder: str, senders: list[str], full_sync: bool = False) -> list[dict]:
    state = load_state()
    folder_key = f"{user}:{folder}"
    last_uid = state.get(folder_key, 0) if not full_sync else 0

    emails: list[dict] = []

    logger.info("Connecting to %s:%d as %s", host, port, user)
    with IMAPClient(host=host, port=port, ssl=True) as client:
        client.login(user, password)
        try:
            client.id_({"name": "accounting-buddy"})
        except Exception:
            logger.debug("IMAP ID not supported")

        client.select_folder(folder, readonly=full_sync)

        if full_sync:
            messages = client.search([b"ALL"])
        else:
            search_criteria = [b"UNSEEN"]
            from_criteria = []
            for sender in senders:
                from_criteria.extend([b"FROM", sender.encode()])
            if len(from_criteria) > 2:
                # Build nested OR: OR(OR(FROM A, FROM B), OR(FROM C, FROM D))
                def build_or(left, right):
                    return [b"OR"] + left + right
                groups = [from_criteria[i:i+2] for i in range(0, len(from_criteria), 2)]
                while len(groups) > 1:
                    groups = [build_or(groups[i], groups[i+1]) if i + 1 < len(groups) else groups[i]
                              for i in range(0, len(groups), 2)]
                criteria = search_criteria + groups[0]
            else:
                criteria = search_criteria + from_criteria
            try:
                messages = client.search(criteria)
            except Exception:
                logger.warning("IMAP OR search failed, fetching all unseen")
                messages = client.search(search_criteria)

        label = "all" if full_sync else "unread"
        logger.info("Found %d %s messages", len(messages), label)

        target_msgs = [uid for uid in messages if uid > last_uid]
        logger.info("%d messages after last UID %d", len(target_msgs), last_uid)

        if not target_msgs:
            return []

        response = client.fetch(target_msgs, [b"RFC822"])

        max_uid = last_uid
        for uid, data in response.items():
            max_uid = max(max_uid, uid)
            raw_bytes = data.get(b"RFC822")
            if not raw_bytes:
                continue

            try:
                msg = email_module.message_from_bytes(raw_bytes)
            except Exception as e:
                logger.error("Failed to parse email UID %d: %s", uid, e)
                continue

            sender = _decode_header_value(msg.get("From", ""))
            subject = _decode_header_value(msg.get("Subject", ""))

            if senders and not any(s in sender for s in senders):
                continue

            body = _get_email_body(msg)
            if not body:
                continue

            if not full_sync:
                client.set_flags(uid, [b"\\Seen"])

            emails.append({
                "uid": uid,
                "sender": sender,
                "subject": subject,
                "html_body": body,
            })

        if not full_sync:
            state[folder_key] = max_uid
            save_state(state)

    logger.info("Fetched %d CMB emails", len(emails))
    return emails
