"""Fetch emails from 163 IMAP by UID range, filter by CMB senders.

Delivery model:
  - Incremental runs scan UID (last_uid + 1):* so emails already read in another
    mail client are still processed.
  - Emails are NOT marked \\Seen here. main.py calls mark_processed() only after
    every transaction of the email has been recorded successfully, so a failed
    API call is retried on the next run (server-side externalId makes retries
    idempotent).
  - On the very first run (no state entry) the cursor is initialised to the
    current newest UID to avoid replaying the whole mailbox. Use --full-sync to
    import history explicitly.
"""

import email as email_module
import email.message
import email.utils
import json
import logging
import os
from datetime import datetime
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
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return str(raw) if raw is not None else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _get_email_body(msg: email_module.message.Message) -> str:
    html_body = ""
    plain_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if content_type == "text/html" and not html_body:
                html_body = _decode_part(part)
            elif content_type == "text/plain" and not plain_body:
                plain_body = _decode_part(part)
    else:
        content_type = msg.get_content_type()
        body = _decode_part(msg)
        if content_type == "text/html":
            html_body = body
        elif content_type == "text/plain":
            plain_body = body

    return html_body or plain_body


def _parse_email_date(msg: email_module.message.Message) -> datetime | None:
    """Return the Date header as a local datetime, or None if unavailable."""
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt


def _open_client(host: str, port: int, user: str, password: str) -> IMAPClient:
    client = IMAPClient(host=host, port=port, ssl=True)
    try:
        client.login(user, password)
        try:
            client.id_({"name": "accounting-buddy"})
        except Exception:
            logger.debug("IMAP ID not supported")
    except Exception:
        client.logout()
        raise
    return client


def fetch_emails(host: str, port: int, user: str, password: str,
                 folder: str, senders: list[str],
                 full_sync: bool = False, dry_run: bool = False) -> tuple[list[dict], int]:
    """Fetch new CMB emails without modifying mailbox flags or state.

    Returns (emails, max_scanned_uid). max_scanned_uid is the highest UID
    inspected in this run, so the caller can advance the cursor even when all
    new messages were non-CMB mail.
    """
    state = load_state()
    folder_key = f"{user}:{folder}"

    logger.info("Connecting to %s:%d as %s", host, port, user)
    with _open_client(host, port, user, password) as client:
        client.select_folder(folder, readonly=True)

        if full_sync:
            last_uid = 0
            criteria = [b"ALL"]
        else:
            if folder_key not in state:
                all_ids = client.search([b"ALL"])
                current_max = max(all_ids, default=0)
                logger.warning(
                    "首次运行：state.json 无 %s 记录，从当前最新 UID %d 开始；"
                    "如需导入历史邮件请运行 --full-sync", folder_key, current_max)
                if not dry_run:
                    state[folder_key] = current_max
                    save_state(state)
                return [], current_max
            last_uid = state[folder_key]
            criteria = [b"UID", f"{last_uid + 1}:*".encode()]

        messages = client.search(criteria)
        target_msgs = sorted(uid for uid in messages if uid > last_uid)
        label = "all" if full_sync else "new"
        logger.info("Found %d %s messages after UID %d", len(target_msgs), label, last_uid)

        if not target_msgs:
            return [], last_uid

        max_scanned_uid = max(target_msgs)
        response = client.fetch(target_msgs, [b"RFC822"])

        emails: list[dict] = []
        for uid, data in sorted(response.items()):
            raw_bytes = data.get(b"RFC822")
            if not raw_bytes:
                continue

            try:
                msg = email_module.message_from_bytes(raw_bytes)
            except Exception as e:
                logger.error("Failed to parse email UID %d: %s", uid, e)
                continue

            sender = _decode_header_value(msg.get("From", ""))
            if senders and not any(s.lower() in sender.lower() for s in senders):
                continue

            body = _get_email_body(msg)
            if not body:
                logger.debug("Email UID %d has no text body, skipped", uid)
                continue

            email_dt = _parse_email_date(msg)
            emails.append({
                "uid": uid,
                "sender": sender,
                "subject": _decode_header_value(msg.get("Subject", "")),
                "html_body": body,
                "date": email_dt.isoformat() if email_dt else None,
            })

    logger.info("Fetched %d CMB emails (scanned up to UID %d)", len(emails), max_scanned_uid)
    return emails, max_scanned_uid


def mark_processed(host: str, port: int, user: str, password: str,
                   folder: str, uids: list[int], state_uid: int) -> None:
    """Mark emails as \\Seen and advance the UID cursor.

    Only call this after the corresponding transactions were recorded (or
    confirmed as duplicates by the server). state_uid must be the highest UID
    that forms a contiguous successfully processed prefix.
    """
    if not uids and state_uid <= 0:
        return

    folder_key = f"{user}:{folder}"
    state = load_state()
    current_uid = state.get(folder_key, 0)
    new_uid = max(current_uid, state_uid)

    if uids:
        logger.info("Marking %d emails as seen (state UID -> %d)", len(uids), new_uid)
        with _open_client(host, port, user, password) as client:
            client.select_folder(folder, readonly=False)
            client.set_flags(sorted(uids), [b"\\Seen"])
    else:
        logger.info("No CMB emails to mark, advancing state UID -> %d", new_uid)

    if new_uid > current_uid:
        state[folder_key] = new_uid
        save_state(state)