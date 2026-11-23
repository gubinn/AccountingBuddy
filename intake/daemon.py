"""Long-polling Telegram intake daemon with preview confirmation.

Every parsed message becomes a preview first; nothing is written until the
user taps 确认入账 / 标为待确认. Run as a systemd user service. It never listens
on a port: outbound calls go to api.telegram.org, BudgetPilot and the local
Ollama instance only.
"""

import argparse
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from api_client import APIClient

from .service import (IntakeError, IntakeService, deserialize_result,
                      serialize_result)
from .state import StateStore
from .telegram import (CallbackQuery, TelegramClient, TelegramError,
                       parse_callback, parse_update)

logger = logging.getLogger("intake")
ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.yaml"
STATE_FILE = ROOT / "intake_state.json"

EMPTY_KEYBOARD = {"inline_keyboard": []}
_stop = False


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()],
    )
    # httpx logs full request URLs at INFO, and the Telegram token is part of
    # the URL path. Keep HTTP client logs out of the journal unless debugging.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_config() -> dict:
    with open(CONFIG_FILE) as handle:
        return yaml.safe_load(handle) or {}


def confirm_ttl(config: dict) -> int:
    try:
        minutes = int((config.get("intake") or {}).get("confirm_ttl_minutes", 30))
    except (TypeError, ValueError):
        minutes = 30
    return max(60, minutes * 60)


def allowed_chat_ids() -> set[int]:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("Ignoring invalid chat id in TELEGRAM_ALLOWED_CHAT_IDS: %r", part)
    return ids


def _command(text: str) -> str:
    first = (text or "").strip().split()[0] if (text or "").strip() else ""
    return first.split("@", 1)[0].lower()


def preview_keyboard(token: str) -> dict:
    return {"inline_keyboard": [
        [
            {"text": "✅ 确认入账", "callback_data": f"cf:{token}"},
            {"text": "🕓 标为待确认", "callback_data": f"pd:{token}"},
            {"text": "❌ 取消", "callback_data": f"cx:{token}"},
        ],
        [{"text": "✏️ 修改内容", "callback_data": f"ed:{token}"}],
    ]}


def _edit_or_send(client: TelegramClient, chat_id: int | str, message_id,
                  text: str, reply_markup: dict | None = None) -> None:
    if message_id is not None:
        try:
            client.edit_message_text(chat_id, message_id, text, reply_markup)
            return
        except Exception as exc:  # noqa: BLE001 - fall back to a new message
            logger.debug("editMessageText failed, sending a new message: %s", exc)
    client.send_message(chat_id, text, reply_markup)


def send_preview(client: TelegramClient, service: IntakeService,
                 state: StateStore, chat_id: int, message_id: int | str,
                 result, ttl: int) -> str | None:
    text = service.format_preview(result)
    usable = [item for item in result.created if not item.error and item.payload]
    if not usable:
        client.send_message(chat_id, text)
        return None
    token = state.add_pending(chat_id, message_id, serialize_result(result), ttl,
                              source_message_id=message_id)
    sent_id = client.send_message(chat_id, text, reply_markup=preview_keyboard(token))
    if sent_id:
        state.set_pending_message_id(token, sent_id)
    return token


def commit_pending(client: TelegramClient, service: IntakeService,
                   state: StateStore, chat_id: int, token: str, entry: dict,
                   confirmed: bool) -> None:
    result = deserialize_result(entry.get("result") or {})
    service.commit(result, confirmed=confirmed)
    tx_ids = [item.tx_id for item in result.created if item.tx_id]
    if tx_ids:
        state.remember(chat_id, tx_ids)

    text = service.format_result(result)
    failed = any(item.error for item in result.created)
    if failed:
        state.update_pending(token, serialize_result(result))
        text += "\n\n点按钮可重试失败项。"
        _edit_or_send(client, chat_id, entry.get("message_id"), text,
                      preview_keyboard(token))
    else:
        state.remove_pending(token)
        _edit_or_send(client, chat_id, entry.get("message_id"), text,
                      EMPTY_KEYBOARD)


def handle_callback(client: TelegramClient, service: IntakeService,
                    state: StateStore, allowed: set[int],
                    callback: CallbackQuery, ttl: int) -> None:
    if callback.chat_id not in allowed:
        logger.warning("Rejected callback from chat %s", callback.chat_id)
        client.answer_callback_query(callback.id, "无权限")
        return

    action, _, token = callback.data.partition(":")
    entry = state.get_pending(token, ttl)
    if entry is None:
        client.answer_callback_query(callback.id, "预览已失效或已处理")
        _edit_or_send(client, callback.chat_id, callback.message_id,
                      "⌛ 这条预览已失效，请重新发送。", EMPTY_KEYBOARD)
        return
    if str(entry.get("chat_id")) != str(callback.chat_id):
        client.answer_callback_query(callback.id, "无权限")
        return

    # Answer immediately so the button spinner stops before the API call.
    client.answer_callback_query(callback.id)

    if action == "cx":
        state.remove_pending(token)
        _edit_or_send(client, callback.chat_id, entry.get("message_id"),
                      "❌ 已取消，未写入账本。", EMPTY_KEYBOARD)
        return

    if action == "ed":
        state.remove_pending(token)
        state.set_editing(callback.chat_id,
                          entry.get("source_message_id") or entry.get("message_id"))
        _edit_or_send(
            client, callback.chat_id, entry.get("message_id"),
            "✏️ 已取消原预览。\n请发送修正后的内容，例如：35 星巴克咖啡",
            EMPTY_KEYBOARD)
        return

    if action not in ("cf", "pd"):
        logger.warning("Unknown callback action: %s", action)
        return

    commit_pending(client, service, state, callback.chat_id, token, entry,
                   confirmed=(action == "cf"))


def _latest_or_reply(client: TelegramClient, state: StateStore,
                     chat_id: int, ttl: int) -> tuple[str, dict] | None:
    latest = state.latest_pending(chat_id, ttl)
    if latest is None:
        client.send_message(chat_id, "没有待确认的预览。")
    return latest


def handle_message(client: TelegramClient, service: IntakeService,
                   state: StateStore, allowed: set[int], parsed,
                   ttl: int) -> None:
    if parsed.from_bot:
        return
    if parsed.chat_id not in allowed:
        logger.warning(
            "Rejected message from chat %s; add it to TELEGRAM_ALLOWED_CHAT_IDS",
            parsed.chat_id)
        return

    command = _command(parsed.text)
    if command in ("/start", "/help") or parsed.text in ("帮助", "用法"):
        client.send_message(parsed.chat_id, service.help_text())
        return
    if command in ("/undo", "撤销", "撤回"):
        tx_ids = state.take_last(parsed.chat_id)
        if not tx_ids:
            client.send_message(parsed.chat_id, "没有可撤销的聊天记账。")
            return
        deleted, failed = service.undo(tx_ids)
        if deleted and not failed:
            reply = "已撤销：" + "、".join(f"#{i}" for i in deleted)
        elif deleted:
            reply = ("已撤销：" + "、".join(f"#{i}" for i in deleted)
                     + "\n失败：" + "、".join(f"#{i}" for i in failed))
        else:
            reply = "撤销失败，交易可能已被删除。"
        client.send_message(parsed.chat_id, reply)
        return

    if command in ("/confirm", "确认"):
        latest = _latest_or_reply(client, state, parsed.chat_id, ttl)
        if latest:
            token, entry = latest
            commit_pending(client, service, state, parsed.chat_id, token, entry,
                           confirmed=True)
        return

    if command in ("/cancel", "取消"):
        state.set_editing(parsed.chat_id, None)
        latest = _latest_or_reply(client, state, parsed.chat_id, ttl)
        if latest:
            token, entry = latest
            state.remove_pending(token)
            _edit_or_send(client, parsed.chat_id, entry.get("message_id"),
                          "❌ 已取消，未写入账本。", EMPTY_KEYBOARD)
        return

    # /edit [text] — re-parse corrected content against the original message id
    if command in ("/edit", "修改"):
        parts = parsed.text.split(maxsplit=1)
        corrected = parts[1].strip() if len(parts) > 1 else ""
        original_mid = state.editing_message_id(parsed.chat_id)
        if original_mid is None:
            latest = state.latest_pending(parsed.chat_id, ttl)
            if latest is not None:
                token, entry = latest
                state.remove_pending(token)
                original_mid = entry.get("source_message_id") or entry.get("message_id")
                _edit_or_send(client, parsed.chat_id, entry.get("message_id"),
                              "✏️ 已取消原预览。", EMPTY_KEYBOARD)
        if not corrected:
            state.set_editing(parsed.chat_id, original_mid or parsed.message_id)
            client.send_message(
                parsed.chat_id,
                "请发送修正后的内容，例如：35 星巴克咖啡")
            return
        state.set_editing(parsed.chat_id, None)
        result = service.prepare(
            text=corrected, channel="telegram", chat_id=str(parsed.chat_id),
            message_id=str(original_mid or parsed.message_id))
        send_preview(client, service, state, parsed.chat_id,
                     original_mid or parsed.message_id, result, ttl)
        return

    if not parsed.text and not parsed.image_file_id:
        return

    # If the user tapped 修改内容, the next message is the corrected version.
    original_mid = state.editing_message_id(parsed.chat_id)
    if original_mid is not None:
        state.set_editing(parsed.chat_id, None)
    message_id = original_mid or parsed.message_id

    with tempfile.TemporaryDirectory(prefix="intake-") as tmpdir:
        image_path = None
        if parsed.image_file_id:
            image_path = str(client.download_file(parsed.image_file_id, tmpdir))
        text = parsed.caption or parsed.text
        result = service.prepare(
            text=text,
            image_path=image_path,
            channel="telegram",
            chat_id=str(parsed.chat_id),
            message_id=str(message_id),
        )
    send_preview(client, service, state, parsed.chat_id, message_id, result, ttl)


def run(once: bool = False) -> int:
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set in %s", ROOT / ".env")
        return 2

    api_base = os.getenv("API_BASE_URL", "").strip()
    api_key = os.getenv("API_KEY", "").strip()
    if not api_base or not api_key:
        logger.error("API_BASE_URL / API_KEY are not set in %s", ROOT / ".env")
        return 2

    allowed = allowed_chat_ids()
    if not allowed:
        logger.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS is empty; every message will be rejected. "
            "Send /start to the bot, read the chat id from this log, then add it.")

    config = load_config()
    ttl = confirm_ttl(config)
    api = APIClient(api_base, api_key)
    service = IntakeService(api, config)
    client = TelegramClient(token)
    state = StateStore(STATE_FILE)

    logger.info("Intake ready: %d allowed chat(s), %d account(s), offset=%s, ttl=%ds",
                len(allowed), len(service.accounts), state.offset, ttl)
    client.set_commands()

    while not _stop:
        state.cleanup_pending(ttl)
        try:
            updates = client.get_updates(state.offset, timeout=0 if once else 50)
        except TelegramError as exc:
            logger.error("Telegram poll failed: %s", exc)
            if once:
                return 1
            time.sleep(5)
            continue
        except Exception as exc:  # noqa: BLE001 - keep the daemon alive
            logger.error("Unexpected poll error: %s", exc)
            if once:
                return 1
            time.sleep(5)
            continue

        for update in updates:
            callback = parse_callback(update)
            parsed = parse_update(update) if callback is None else None
            target_chat = callback.chat_id if callback else (
                parsed.chat_id if parsed else None)
            next_offset = int(update.get("update_id", 0)) + 1
            try:
                if callback is not None:
                    handle_callback(client, service, state, allowed, callback, ttl)
                elif parsed is not None:
                    handle_message(client, service, state, allowed, parsed, ttl)
            except IntakeError as exc:
                logger.error("Intake error: %s", exc)
                if target_chat is not None:
                    try:
                        client.send_message(target_chat, f"❌ {exc}")
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop intake
                logger.exception("Failed to handle update: %s", exc)
                if target_chat is not None:
                    try:
                        client.send_message(target_chat, "❌ 处理失败，请稍后重试")
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                state.set_offset(next_offset)
        if once:
            break
    return 0


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    logger.info("Signal %s received, stopping after the current poll", signum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram chat intake for BudgetPilot")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    return run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())