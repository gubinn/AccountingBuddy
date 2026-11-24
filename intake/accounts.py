"""Resolve which BudgetPilot account a chat transaction belongs to.

The message rarely names a bank account. Resolution order:

1. An explicit alias from ``intake.account_aliases`` (e.g. ``微信 -> 1``).
2. A card suffix with a label (``尾号050``, ``卡号1234``, ``(1234)``) or a
   full card number, matched against the suffix in the account name.
3. An account whose display name or bank/wallet short name appears in the
   message (``汇丰`` matches ``汇丰中国(5555/066)``, ``微信`` matches
   ``微信余额``).
4. A standalone 4-digit suffix that matches an account name.
5. ``account_mapping`` (the email-link suffix map) by suffix.
6. ``intake.default_account_id`` / ``default_account_id``.
7. The first active account, so a single-account setup needs no config.

Bank and wallet short names matter because users write "汇丰" or "微信", not
the full display name "汇丰中国(5555/066)" / "微信余额".
"""

import logging
import re

logger = logging.getLogger(__name__)

_CONTEXT_SUFFIX = re.compile(
    r"(?:尾号|尾號|卡号|卡號|账号|帐号|帳號|账號"
    r"|账户|帐户|帳戶|賬戶|卡)\s*[（(]?\s*(\d{3,4})(?!\d)")
_PAREN_SUFFIX = re.compile(r"[（(]\s*(\d{3,4})\s*[)）]")
_STANDALONE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_LONG_CARD = re.compile(r"(?<!\d)(\d{12,19})(?!\d)")
_SUFFIX_TOKEN = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")
_PAREN_SCOPE = re.compile(r"[（(]([^）)]*)[)）]")

# Bank short names / abbreviations users actually type. Full bank names are
# included where the short form is ambiguous. Keep partial words such as
# "交通" or "建设" out of the list; they appear in ordinary sentences.
_BANK_ALIASES = (
    ("招商银行", ("招商银行", "招行")),
    ("中国银行", ("中国银行", "中行")),
    ("工商银行", ("工商银行", "工行")),
    ("建设银行", ("建设银行", "建行")),
    ("农业银行", ("农业银行", "农行")),
    ("交通银行", ("交通银行", "交行")),
    ("浦发银行", ("浦发银行", "浦发")),
    ("中信银行", ("中信银行", "中信")),
    ("民生银行", ("民生银行", "民生")),
    ("光大银行", ("光大银行", "光大")),
    ("兴业银行", ("兴业银行", "兴业")),
    ("平安银行", ("平安银行",)),
    ("邮储银行", ("邮储银行", "邮政储蓄", "邮储")),
    ("广发银行", ("广发银行", "广发")),
    ("华夏银行", ("华夏银行",)),
    ("汇丰", ("汇丰", "匯豐", "汇丰银行", "匯豐銀行", "hsbc")),
    ("匯豐", ("汇丰", "匯豐", "汇丰银行", "匯豐銀行", "hsbc")),
    ("恒生", ("恒生", "恒生银行")),
    ("渣打", ("渣打", "渣打银行")),
    ("东亚银行", ("东亚银行", "東亞銀行")),
    ("花旗", ("花旗", "花旗银行", "citi")),
    ("中银香港", ("中银香港", "中銀香港", "中银", "中銀")),
    ("众安", ("众安", "za bank")),
    ("天星", ("天星",)),
)

_WALLET_ALIASES = (
    ("微信", ("微信", "微信支付", "wechat", "weixin")),
    ("支付宝", ("支付宝", "alipay")),
    ("云闪付", ("云闪付",)),
    ("现金", ("现金",)),
)

_BANK_WORDS = (
    "招商", "中国银行", "工商", "建设", "浦发", "农业", "交通", "中信",
    "民生", "光大", "兴业", "平安", "邮储", "广发", "华夏",
    "汇丰", "恒生", "渣打", "东亚", "花旗", "中银",
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

        mapping = config.get("account_mapping") or {}
        self.account_mapping = {
            str(key).strip(): int(value)
            for key, value in mapping.items()
            if value is not None
        }
        self.account_ids = {int(account["id"]) for account in self.accounts}
        # Reason for the most recent resolution, useful in logs when a user
        # reports that the account was guessed wrong.
        self.last_reason = "none"

    def resolve(self, hint: str = "", text: str = "") -> int | None:
        haystack = f"{hint or ''} {text or ''}".strip().lower()
        self.last_reason = "none"

        for alias in sorted(self.aliases, key=len, reverse=True):
            if alias and alias in haystack:
                self.last_reason = f"alias:{alias}"
                return self.aliases[alias]

        # A labelled suffix is the strongest signal: the user copied the card
        # tail from the payment notification. Support 3-digit tails used by
        # some Hong Kong accounts.
        for suffix in self._strong_suffixes(haystack):
            matched = self._candidate_by_suffix(suffix, haystack)
            if matched is not None:
                self.last_reason = f"suffix:{suffix}"
                return matched

        # Bank/wallet short names. If several accounts share an alias (for
        # example three 招商 cards), try to narrow by a suffix in the message;
        # otherwise keep the first candidate rather than falling back to a
        # completely unrelated default account.
        candidates = self._named_candidates(haystack)
        if candidates:
            if len(candidates) == 1:
                self.last_reason = "name"
                return int(candidates[0]["id"])
            narrowed = self._narrow_by_any_suffix(candidates, haystack)
            if narrowed is not None:
                return narrowed
            self.last_reason = "name:ambiguous"
            return int(self._prefer_bank(candidates, haystack)["id"])

        # A standalone 4-digit group is a suffix if it is not an amount, year
        # or date. Do this after bank names so "招商银行 1234" keeps the bank.
        for suffix in self._standalone_suffixes(haystack):
            matched = self._candidate_by_suffix(suffix, haystack)
            if matched is not None:
                self.last_reason = f"standalone:{suffix}"
                return matched

        # Reuse the email-link suffix map when the account display name does
        # not contain the suffix.
        for suffix in self._all_suffixes(haystack):
            mapped = self.account_mapping.get(suffix)
            if mapped in self.account_ids:
                self.last_reason = f"mapping:{suffix}"
                return mapped

        if self.default_id:
            self.last_reason = f"default:{self.default_id}"
            return self.default_id
        if self.accounts:
            self.last_reason = f"first:{self.accounts[0]['id']}"
            return int(self.accounts[0]["id"])
        return None

    @property
    def used_fallback(self) -> bool:
        """True when no message signal selected an account."""
        return self.last_reason.startswith(("default", "first", "none"))

    def _named_candidates(self, haystack: str) -> list[dict]:
        candidates: list[dict] = []
        for account in self.accounts:
            if any(alias in haystack for alias in self._aliases_for(account)):
                candidates.append(account)
        return candidates

    @staticmethod
    def _aliases_for(account: dict) -> list[str]:
        name = str(account.get("name") or "").strip().lower()
        if not name:
            return []
        aliases = {name}
        base = _PAREN_SCOPE.sub("", name).strip()
        if len(base) >= 2:
            aliases.add(base)
        for key, words in _BANK_ALIASES + _WALLET_ALIASES:
            if key.lower() in name:
                aliases.update(word.lower() for word in words)
        return [alias for alias in aliases if len(alias) >= 2]

    def _narrow_by_any_suffix(self, candidates: list[dict],
                              haystack: str) -> int | None:
        for suffix in self._all_suffixes(haystack):
            matched = [
                account for account in candidates
                if suffix in self._suffixes_of(account)
            ]
            if len(matched) == 1:
                self.last_reason = f"name+suffix:{suffix}"
                return int(matched[0]["id"])
            if matched:
                self.last_reason = f"name+suffix:{suffix}:ambiguous"
                return int(self._prefer_bank(matched, haystack)["id"])
        return None

    def _candidate_by_suffix(self, suffix: str, haystack: str) -> int | None:
        candidates = [
            account for account in self.accounts
            if suffix in self._suffixes_of(account)
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return int(candidates[0]["id"])
        return int(self._prefer_bank(candidates, haystack)["id"])

    @classmethod
    def _suffixes_of(cls, account: dict) -> list[str]:
        name = str(account.get("name") or "")
        scope = _PAREN_SCOPE.search(name)
        # Prefer the parenthesised card tail; fall back to the whole name for
        # accounts named like "招商储蓄卡8338" without parentheses.
        haystack = scope.group(1) if scope else name
        return _SUFFIX_TOKEN.findall(haystack)

    @staticmethod
    def _prefer_bank(candidates: list[dict], haystack: str) -> dict:
        for account in candidates:
            name = str(account.get("name") or "").lower()
            if any(word.lower() in haystack and word.lower() in name
                   for word in _BANK_WORDS):
                return account
        return candidates[0]

    @staticmethod
    def _strong_suffixes(haystack: str) -> list[str]:
        found: list[str] = []
        for pattern in (_CONTEXT_SUFFIX, _PAREN_SUFFIX):
            for match in pattern.finditer(haystack):
                if match.group(1) not in found:
                    found.append(match.group(1))
        # A full card number is also explicit; only its last four digits matter.
        for match in _LONG_CARD.finditer(haystack):
            tail = match.group(0)[-4:]
            if tail not in found:
                found.append(tail)
        return found

    @staticmethod
    def _standalone_suffixes(haystack: str) -> list[str]:
        found: list[str] = []
        for match in _STANDALONE.finditer(haystack):
            tail = haystack[match.end():match.end() + 2]
            if re.match(r"[.．元块\-/年月日:]", tail):
                continue
            found.append(match.group(1))
        return found

    @classmethod
    def _all_suffixes(cls, haystack: str) -> list[str]:
        found = cls._strong_suffixes(haystack)
        for suffix in cls._standalone_suffixes(haystack):
            if suffix not in found:
                found.append(suffix)
        return found

    def name_of(self, account_id: int | None) -> str:
        for account in self.accounts:
            if int(account["id"]) == account_id:
                return str(account.get("name") or f"#{account_id}")
        return f"#{account_id}" if account_id else "-"