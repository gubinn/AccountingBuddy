"""Resolve which BudgetPilot account a chat transaction belongs to.

The message rarely names a bank account. Resolution order:

1. An explicit alias from ``intake.account_aliases`` (e.g. ``微信 -> 1``).
2. An account whose display name appears in the message.
3. A card suffix (``尾号1234``, ``卡号1234``, ``(1234)``, or a standalone
   4-digit number that matches an account name).
4. ``intake.default_account_id`` / ``default_account_id``.
5. The first active account, so a single-account setup needs no config.
"""

import logging
import re

logger = logging.getLogger(__name__)

_CONTEXT_SUFFIX = re.compile(
    r"(?:尾号|卡号|账号|账户|卡)\s*[（(]?\s*(\d{4})(?!\d)")
_PAREN_SUFFIX = re.compile(r"[（(]\s*(\d{4})\s*[)）]")
_STANDALONE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_LONG_CARD = re.compile(r"(?<!\d)(\d{12,19})(?!\d)")
_BANK_WORDS = (
    "招商", "中国银行", "工商", "建设", "浦发", "农业", "交通", "中信",
    "民生", "光大", "兴业", "平安", "邮储", "广发", "华夏",
)


class AccountResolver:
    def __init__(self, accounts: list[dict], config: dict):
        self.accounts = [a for a in (accounts or []) if a.get("id")]
        intake = config.get("intake") or {}
        aliases = intake.get("account_aliases") or {}
        self.aliases = {
            str(key).lower(): int(value)
            for key, value in aliases.items()
            if value is not None
        }
        default = intake.get("default_account_id") or config.get("default_account_id")
        self.default_id = int(default) if default else None

    def resolve(self, hint: str = "", text: str = "") -> int | None:
        haystack = f"{hint or ''} {text or ''}".lower()
        for alias in sorted(self.aliases, key=len, reverse=True):
            if alias and alias in haystack:
                return self.aliases[alias]

        for account in self.accounts:
            name = str(account.get("name") or "").strip().lower()
            if name and name in haystack:
                return int(account["id"])

        for suffix in self._contextual_suffixes(haystack):
            matched = self._by_suffix(suffix, haystack)
            if matched:
                return matched

        # A full card number may be printed; only its last four digits matter.
        for match in _LONG_CARD.finditer(haystack):
            matched = self._by_suffix(match.group(1)[-4:], haystack)
            if matched:
                return matched

        # OCR often prints the card suffix without a label. A standalone
        # 4-digit group is a suffix if it is not an amount, year or date.
        for match in _STANDALONE.finditer(haystack):
            tail = haystack[match.end():match.end() + 2]
            if re.match(r"[.．元块\-/年月日:]", tail):
                continue
            matched = self._by_suffix(match.group(1), haystack)
            if matched:
                return matched

        if self.default_id:
            return self.default_id
        if self.accounts:
            return int(self.accounts[0]["id"])
        return None

    @staticmethod
    def _contextual_suffixes(haystack: str) -> list[str]:
        found: list[str] = []
        for pattern in (_CONTEXT_SUFFIX, _PAREN_SUFFIX):
            found.extend(m.group(1) for m in pattern.finditer(haystack))
        return found

    def _by_suffix(self, suffix: str, haystack: str) -> int | None:
        candidates = [
            account for account in self.accounts
            if suffix and suffix in str(account.get("name") or "")
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return int(candidates[0]["id"])
        # Prefer the account whose bank name is also mentioned.
        for account in candidates:
            name = str(account.get("name") or "")
            if any(word in haystack and word in name for word in _BANK_WORDS):
                return int(account["id"])
        return int(candidates[0]["id"])

    def name_of(self, account_id: int | None) -> str:
        for account in self.accounts:
            if int(account["id"]) == account_id:
                return str(account.get("name") or f"#{account_id}")
        return f"#{account_id}" if account_id else "-"