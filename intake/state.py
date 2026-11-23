"""Small JSON state file for the chat intake daemon.

Tracks the Telegram update offset, the transaction ids created by the last
message in each chat, pending previews waiting for a button press, and the
per-chat "waiting for corrected text" flag. Contains transaction ids and parsed
fields only; never raw chat attachments.
"""

import json
import logging
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30 * 60
MAX_PENDING_PER_CHAT = 5


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {
            "telegram_offset": None,
            "last_transactions": {},
            "pending": {},
            "editing": {},
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path) as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load intake state: %s", exc)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to save intake state: %s", exc)

    @property
    def offset(self) -> int | None:
        value = self.data.get("telegram_offset")
        return int(value) if value is not None else None

    def set_offset(self, offset: int) -> None:
        self.data["telegram_offset"] = int(offset)
        self.save()

    def remember(self, chat_id: int | str, tx_ids: list[int]) -> None:
        self.data.setdefault("last_transactions", {})[str(chat_id)] = {
            "tx_ids": [int(i) for i in tx_ids],
            "at": int(time.time()),
        }
        self.save()

    def take_last(self, chat_id: int | str) -> list[int]:
        entry = (self.data.get("last_transactions") or {}).pop(str(chat_id), None)
        if entry is not None:
            self.save()
        return [int(i) for i in (entry or {}).get("tx_ids", [])]

    # ---------- pending previews ----------

    def add_pending(self, chat_id: int | str, message_id: int | str,
                    result_data: dict, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                    max_per_chat: int = MAX_PENDING_PER_CHAT,
                    source_message_id: int | str | None = None) -> str:
        self.cleanup_pending(ttl_seconds)
        chat = str(chat_id)
        pending = self.data.setdefault("pending", {})

        # Bound memory: drop the oldest previews of this chat.
        mine = sorted(
            ((token, entry) for token, entry in pending.items()
             if str(entry.get("chat_id")) == chat),
            key=lambda pair: pair[1].get("created_at", 0),
        )
        while len(mine) >= max_per_chat:
            token, _ = mine.pop(0)
            pending.pop(token, None)

        token = secrets.token_hex(4)
        while token in pending:
            token = secrets.token_hex(4)
        pending[token] = {
            "chat_id": chat,
            # Bot's preview message, used by editMessageText.
            "message_id": str(message_id),
            # Original user message, used for the idempotent externalId.
            "source_message_id": str(source_message_id if source_message_id is not None
                                     else message_id),
            # Float keeps ordering when two previews arrive in the same second.
            "created_at": time.time(),
            "result": result_data,
        }
        self.save()
        return token

    def set_pending_message_id(self, token: str, message_id: int | str) -> None:
        entry = (self.data.get("pending") or {}).get(token)
        if entry is None:
            return
        entry["message_id"] = str(message_id)
        self.save()

    def get_pending(self, token: str,
                    ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict | None:
        entry = (self.data.get("pending") or {}).get(token)
        if entry is None:
            return None
        if self._expired(entry, ttl_seconds):
            self.remove_pending(token)
            return None
        return entry

    def latest_pending(self, chat_id: int | str,
                       ttl_seconds: int = DEFAULT_TTL_SECONDS) -> tuple[str, dict] | None:
        self.cleanup_pending(ttl_seconds)
        chat = str(chat_id)
        mine = [
            (token, entry)
            for token, entry in (self.data.get("pending") or {}).items()
            if str(entry.get("chat_id")) == chat
        ]
        if not mine:
            return None
        return max(mine, key=lambda pair: pair[1].get("created_at", 0))

    def remove_pending(self, token: str) -> dict | None:
        entry = (self.data.get("pending") or {}).pop(token, None)
        if entry is not None:
            self.save()
        return entry

    def cleanup_pending(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> list[str]:
        pending = self.data.get("pending") or {}
        expired = [token for token, entry in pending.items()
                   if self._expired(entry, ttl_seconds)]
        for token in expired:
            pending.pop(token, None)
        if expired:
            self.save()
        return expired

    @staticmethod
    def _expired(entry: dict, ttl_seconds: int) -> bool:
        return float(entry.get("created_at", 0)) + ttl_seconds < time.time()

    # ---------- edit mode ----------

    def set_editing(self, chat_id: int | str, message_id: int | str | None) -> None:
        """Remember which original message the corrected text belongs to."""
        key = str(chat_id)
        flags = self.data.setdefault("editing", {})
        if message_id is None:
            flags.pop(key, None)
        else:
            flags[key] = str(message_id)
        self.save()

    def editing_message_id(self, chat_id: int | str) -> str | None:
        value = (self.data.get("editing") or {}).get(str(chat_id))
        return str(value) if value is not None else None

    def update_pending(self, token: str, result_data: dict) -> None:
        entry = (self.data.get("pending") or {}).get(token)
        if entry is None:
            return
        entry["result"] = result_data
        self.save()