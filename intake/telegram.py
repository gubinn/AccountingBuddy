"""Minimal Telegram Bot API client using long polling.

Long polling needs no public port or webhook, which keeps the accounting host
off the internet. Only messages are requested; edits and channel posts are
ignored because they are not new transactions.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4000


class TelegramError(Exception):
    pass


@dataclass
class ParsedUpdate:
    update_id: int
    chat_id: int
    message_id: int
    text: str = ""
    caption: str = ""
    image_file_id: str | None = None
    from_bot: bool = False
    sent_at: int = 0


@dataclass
class CallbackQuery:
    id: str
    chat_id: int
    message_id: int
    data: str = ""
    from_bot: bool = False


def parse_callback(update: dict) -> CallbackQuery | None:
    query = update.get("callback_query")
    if not query:
        return None
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("id") is None or message.get("message_id") is None:
        return None
    return CallbackQuery(
        id=str(query.get("id") or ""),
        chat_id=int(chat["id"]),
        message_id=int(message["message_id"]),
        data=str(query.get("data") or ""),
        from_bot=bool((query.get("from") or {}).get("is_bot")),
    )


def parse_update(update: dict) -> ParsedUpdate | None:
    message = update.get("message")
    if not message:
        return None
    chat = message.get("chat") or {}
    if chat.get("id") is None or message.get("message_id") is None:
        return None

    image_file_id = None
    photos = message.get("photo") or []
    if photos:
        largest = max(photos, key=lambda p: (p.get("width", 0) * p.get("height", 0),
                                             p.get("file_size", 0)))
        image_file_id = largest.get("file_id")
    document = message.get("document") or {}
    if not image_file_id and str(document.get("mime_type", "")).startswith("image/"):
        image_file_id = document.get("file_id")

    return ParsedUpdate(
        update_id=int(update.get("update_id", 0)),
        chat_id=int(chat["id"]),
        message_id=int(message["message_id"]),
        text=str(message.get("text") or "").strip(),
        caption=str(message.get("caption") or "").strip(),
        image_file_id=image_file_id,
        from_bot=bool((message.get("from") or {}).get("is_bot")),
        sent_at=int(message.get("date") or 0),
    )


class TelegramClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is empty")
        self._token = token
        self._base = base_url.rstrip("/")

    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token}/{method}"

    def _safe(self, error: Exception) -> str:
        """Never let the token leak into logs through an exception message."""
        return str(error).replace(self._token, "***")

    @staticmethod
    def _body(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(f"non-JSON response ({response.status_code})") from exc
        if not data.get("ok"):
            description = data.get("description") or f"HTTP {response.status_code}"
            raise TelegramError(description)
        return data.get("result")

    def get_updates(self, offset: int | None = None, timeout: int = 50) -> list[dict]:
        params = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset:
            params["offset"] = offset
        try:
            with httpx.Client(timeout=timeout + 15) as client:
                response = client.get(self._url("getUpdates"), params=params)
        except httpx.HTTPError as exc:
            raise TelegramError(f"getUpdates failed: {self._safe(exc)}") from exc
        return list(self._body(response) or [])

    def send_message(self, chat_id: int | str, text: str,
                     reply_markup: dict | None = None) -> int | None:
        chunks = _chunks(text or "", MAX_MESSAGE_LENGTH)
        message_id = None
        for index, chunk in enumerate(chunks):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            # Attach the keyboard to the last chunk only.
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            try:
                with httpx.Client(timeout=20) as client:
                    response = client.post(self._url("sendMessage"), json=payload)
            except httpx.HTTPError as exc:
                raise TelegramError(f"sendMessage failed: {self._safe(exc)}") from exc
            sent = self._body(response)
            if isinstance(sent, dict) and sent.get("message_id") is not None:
                message_id = int(sent["message_id"])
        return message_id

    def edit_message_text(self, chat_id: int | str, message_id: int | str,
                          text: str, reply_markup: dict | None = None) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text or "",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(self._url("editMessageText"), json=payload)
            self._body(response)
        except TelegramError as exc:
            # Clicking the same button twice can hit an already-edited message.
            if "message is not modified" in str(exc).lower():
                return
            raise
        except httpx.HTTPError as exc:
            raise TelegramError(f"editMessageText failed: {self._safe(exc)}") from exc

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        try:
            with httpx.Client(timeout=10) as client:
                self._body(client.post(self._url("answerCallbackQuery"), json=payload))
        except Exception as exc:  # noqa: BLE001 - the button must not crash the loop
            logger.warning("answerCallbackQuery failed: %s", self._safe(exc))

    def download_file(self, file_id: str, dest_dir: str | Path) -> Path:
        try:
            with httpx.Client(timeout=60) as client:
                info = self._body(client.get(self._url("getFile"), params={"file_id": file_id}))
                file_path = (info or {}).get("file_path")
                if not file_path:
                    raise TelegramError("getFile returned no file_path")
                suffix = Path(file_path).suffix or ".jpg"
                destination = Path(dest_dir)
                destination.mkdir(parents=True, exist_ok=True)
                target = destination / f"tg_{file_id[:24]}{suffix}"
                response = client.get(f"{self._base}/file/bot{self._token}/{file_path}")
                if response.status_code != 200:
                    # Do not use raise_for_status(): its message contains the
                    # URL, and that URL contains the bot token.
                    raise TelegramError(f"download failed: HTTP {response.status_code}")
                target.write_bytes(response.content)
                return target
        except httpx.HTTPError as exc:
            raise TelegramError(f"download failed: {self._safe(exc)}") from exc

    def set_commands(self) -> None:
        """Best-effort command list so /help and /undo are discoverable."""
        commands = [
            {"command": "help", "description": "查看记账用法"},
            {"command": "confirm", "description": "确认最新预览"},
            {"command": "cancel", "description": "取消最新预览"},
            {"command": "edit", "description": "修改预览内容"},
            {"command": "undo", "description": "撤销上一条已入账消息"},
        ]
        try:
            with httpx.Client(timeout=15) as client:
                client.post(self._url("setMyCommands"), json={"commands": commands})
        except Exception as exc:  # noqa: BLE001 - cosmetic only
            logger.debug("setMyCommands failed: %s", self._safe(exc))


def _chunks(text: str, size: int) -> list[str]:
    if not text:
        return [""]
    return [text[i:i + size] for i in range(0, len(text), size)]