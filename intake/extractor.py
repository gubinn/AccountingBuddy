"""Extract transactions from free-form chat text or OCR output.

Two tiers:

1. A strict fast path for short commands such as ``午饭 35 微信``.
2. A local Ollama JSON extraction for bank SMS, payment screenshots and any
   other free-form message. The model is only asked to structure the text; the
   category is decided later by :mod:`category_mapper`.

If the model is unavailable the extractor falls back to a conservative regex
parse so a simple message still works.
"""

import json
import logging
import re
from datetime import date, datetime, timedelta

import httpx

from .models import DraftTransaction

logger = logging.getLogger(__name__)

CURRENCIES = ("CNY", "HKD", "USD", "EUR", "GBP", "JPY", "SGD", "THB", "KRW")

_RE_REFUND = re.compile(
    r"^(?:退款|退货|退回)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)?\s*(.*)$")
_RE_EXPENSE = re.compile(
    r"^(?:支出|消费|花了|付款|支付|付)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)?\s*(.*)$")
_RE_INCOME = re.compile(
    r"^(?:收入|收到|入账|到账|工资|奖金|报销)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)?\s*(.*)$")
_RE_AMOUNT_FIRST = re.compile(
    r"^[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)?\s+(.+)$")
_RE_MERCHANT_AMOUNT = re.compile(
    r"^(.+?)\s+[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)?(?:\s+(.+))?$")

_RE_ACCOUNT_HINT = re.compile(
    r"(尾号\s*\d{4}|卡号\s*\d{4}|[（(]\s*\d{4}\s*[)）]"
    r"|微信|支付宝|云闪付|现金"
    r"|招商银行|招行|中国银行|中行|工商银行|工行|建设银行|建行"
    r"|浦发银行|浦发|农业银行|农行|交通银行|交行|中信银行|民生银行"
    r"|光大银行|兴业银行|平安银行|邮储银行|广发银行|华夏银行"
    r"|信用卡|储蓄卡)")
_RE_AMOUNT = re.compile(r"(?:¥|￥)?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(?:元|块)?")
_RE_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

INCOME_WORDS = ("收入", "入账", "到账", "收到", "工资", "奖金", "报销", "退款到账")
REFUND_WORDS = ("退款", "退货", "退回")
BALANCE_WORDS = ("余额", "可用额度", "剩余额度")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now()


def normalize_date(value, now: datetime | None = None) -> str:
    """Best-effort conversion of model/OCR date output to ``YYYY-MM-DD``."""
    today = _now(now).date()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return today.isoformat()

    text = str(value).strip()
    # Accept "2026-11-23 12:30:45" and ISO datetime strings from the model.
    text = re.split(r"[ T]", text, maxsplit=1)[0]
    if not text:
        return today.isoformat()
    if text in ("今天", "今日", "today"):
        return today.isoformat()
    if text in ("昨天", "昨日", "yesterday"):
        return (today - timedelta(days=1)).isoformat()
    if text in ("前天",):
        return (today - timedelta(days=2)).isoformat()

    match = re.match(r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?$", text)
    if match:
        year, month, day = (int(x) for x in match.groups())
    else:
        match = re.match(r"^(\d{1,2})[-/月](\d{1,2})日?$", text)
        if not match:
            return today.isoformat()
        month, day = (int(x) for x in match.groups())
        year = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return today.isoformat()
    # Notifications never describe the future. A bare month/day later than
    # today almost always belongs to last year (cross-year backlog).
    if not re.match(r"^\d{4}", text) and candidate > today + timedelta(days=1):
        try:
            candidate = date(year - 1, month, day)
        except ValueError:
            pass
    return candidate.isoformat()


def normalize_time(value) -> str:
    if not value:
        return ""
    match = _RE_TIME.match(str(value).strip())
    if not match:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def normalize_currency(value, text: str = "") -> str:
    if value:
        currency = str(value).strip().upper()
        if currency in CURRENCIES:
            return currency
    haystack = text or ""
    if re.search(r"HKD|港币|港元", haystack, re.I):
        return "HKD"
    if re.search(r"USD|美元|\$", haystack, re.I):
        return "USD"
    if re.search(r"EUR|欧元|€", haystack, re.I):
        return "EUR"
    if re.search(r"GBP|英镑|£", haystack, re.I):
        return "GBP"
    if re.search(r"JPY|日元|円", haystack, re.I):
        return "JPY"
    return "CNY"


def _account_hint(*values: str) -> str:
    hints: list[str] = []
    for value in values:
        for match in _RE_ACCOUNT_HINT.finditer(value or ""):
            hint = re.sub(r"\s+", "", match.group(1))
            if hint and hint not in hints:
                hints.append(hint)
    return " ".join(hints)


GENERIC_MERCHANTS = {
    "储蓄卡", "信用卡", "银行卡", "借记卡", "卡", "消费", "付款", "支付",
    "转账", "入账", "微信支付", "支付宝", "账户", "本人账户",
}


def _clean_merchant(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ，,。.;；:：-—")
    text = re.sub(r"^(?:商户|收款方|商家|付款给)\s*[:：]?\s*", "", text)
    if text in GENERIC_MERCHANTS:
        return ""
    return text[:50]


def _fallback_merchant(text: str, amount_text: str) -> str:
    """Strip boilerplate from a message when no model is available."""
    cleaned = text.replace(amount_text, " ")
    cleaned = re.sub(r"(今天|昨天|前天|今早|今晚|上午|中午|下午|晚上)", " ", cleaned)
    cleaned = re.sub(r"(花了|消费|付款|支付|买了|用了|收到|入账)", " ", cleaned)
    cleaned = re.sub(r"(用的是|用的|用)\s*(微信|支付宝|现金|信用卡|储蓄卡)", " ", cleaned)
    cleaned = re.sub(r"[，,。.;；:：!！?？]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return _clean_merchant(cleaned)


def _amount(value) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    number = round(abs(number), 2)
    if number <= 0 or number >= 10_000_000:
        return None
    return number


def _tx_type(value, text: str = "") -> int:
    if isinstance(value, int) and value in (1, 2, 4):
        return value
    lowered = str(value or "").strip().lower()
    if lowered in ("2", "income", "收入", "入账", "credit"):
        return 2
    if lowered in ("4", "refund", "退款", "退货"):
        return 4
    if lowered in ("1", "expense", "支出", "消费", "debit"):
        return 1
    if any(word in text for word in REFUND_WORDS):
        return 4
    if any(word in text for word in INCOME_WORDS):
        return 2
    return 1


def _fast_parse(text: str, now: datetime | None = None) -> list[DraftTransaction]:
    compact = " ".join(text.split())
    if len(compact) > 60 or "\n" in text.strip():
        return []
    if any(word in compact for word in BALANCE_WORDS) and not re.match(
            r"^(?:支出|消费|付款|支付|收入|收到|入账)", compact):
        return []

    for pattern, tx_type in ((_RE_REFUND, 4), (_RE_EXPENSE, 1), (_RE_INCOME, 2)):
        match = pattern.match(compact)
        if match:
            amount = _amount(match.group(1))
            if amount is None:
                return []
            rest = (match.group(2) or "").strip()
            merchant = _clean_merchant(rest) or "聊天记账"
            hint = _account_hint(rest)
            return [DraftTransaction(
                tx_type=tx_type,
                amount=amount,
                currency=normalize_currency("", compact),
                date=normalize_date(None, now),
                merchant=merchant,
                note=compact,
                account_hint=hint,
                confidence=0.95,
            )]

    match = _RE_AMOUNT_FIRST.match(compact)
    if match:
        amount = _amount(match.group(1))
        if amount is None:
            return []
        rest = match.group(2).strip()
        return [DraftTransaction(
            tx_type=_tx_type(None, compact),
            amount=amount,
            currency=normalize_currency("", compact),
            date=normalize_date(None, now),
            merchant=_clean_merchant(rest) or "聊天记账",
            note=compact,
            account_hint=_account_hint(rest),
            confidence=0.95,
        )]

    match = _RE_MERCHANT_AMOUNT.match(compact)
    if match:
        amount = _amount(match.group(2))
        if amount is None:
            return []
        merchant = _clean_merchant(match.group(1))
        tail = (match.group(3) or "").strip()
        hint = _account_hint(tail)
        if tail and not hint and len(tail) <= 10:
            merchant = _clean_merchant(f"{merchant} {tail}")
        return [DraftTransaction(
            tx_type=_tx_type(None, compact),
            amount=amount,
            currency=normalize_currency("", compact),
            date=normalize_date(None, now),
            merchant=merchant or "聊天记账",
            note=compact,
            account_hint=hint,
            confidence=0.9,
        )]
    return []


def _build_prompt(text: str, now: datetime) -> str:
    # Small local models follow a short prompt with one worked example much
    # more reliably than a long rule list.
    today = f"{now:%Y-%m-%d}"
    return f"""你是记账助手。从输入文本中提取所有真实交易，只输出 JSON。

字段：type（1=支出，2=收入，4=退款）、amount（正数）、date（YYYY-MM-DD）、time（HH:MM:SS 或空）、merchant（商户简称）、account_hint（微信/支付宝/现金/尾号1234，或空）、note（可空）、currency（默认 CNY）、confidence（0-1）。

今天是 {today}。
"付款成功""支付成功""消费""扣款""转账""入账""退款"且带金额的都是交易；忽略余额、优惠、积分、广告和问候。
金额前面的负号表示支出，输出仍用正数。
消息可能是支付截图 OCR 文本，有错字或噪声也要尽量提取；一张账单有多条交易时全部输出。
没有交易时输出 {{"transactions":[]}}。

示例输入：付款成功 -35.00元 星巴克咖啡 2026-11-23 12:30:45
示例输出：{{"transactions":[{{"type":1,"amount":35.0,"date":"2026-11-23","time":"12:30:45","merchant":"星巴克咖啡","account_hint":"","note":"","currency":"CNY","confidence":0.95}}]}}

输入：
---
{text[:4000]}
---
输出："""


def _llm_extract(text: str, config: dict, now: datetime) -> list[DraftTransaction]:
    intake = config.get("intake") or {}
    url = str(config.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/")
    model = str(config.get("ollama_model", "qwen2.5-7b"))
    timeout = float(intake.get("llm_timeout", config.get("llm_timeout", 120)))
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _build_prompt(text, now)}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 512},
    }
    if config.get("ollama_disable_thinking"):
        payload["think"] = False

    started = datetime.now()
    with httpx.Client(timeout=timeout) as client:
        response = client.post(f"{url}/api/chat", json=payload)
        response.raise_for_status()
        content = response.json()["message"]["content"]
    logger.info("Intake LLM parsed message in %.1fs",
                (datetime.now() - started).total_seconds())

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("transactions")
        if items is None and data.get("amount") is not None:
            items = [data]
        elif items is None:
            items = []
    else:
        return []
    if not isinstance(items, list):
        return []

    drafts: list[DraftTransaction] = []
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        amount = _amount(item.get("amount"))
        if amount is None:
            continue
        merchant = _clean_merchant(item.get("merchant") or item.get("payee") or "")
        note = str(item.get("note") or "").strip()[:200]
        tx_type = _tx_type(item.get("type") or item.get("tx_type"), f"{merchant} {note}")
        # BudgetPilot represents refunds as type 4 (offset the original expense)
        # rather than income; correct the model when it says "income".
        context = f"{merchant} {note} {text}"
        if tx_type == 2 and any(word in context for word in REFUND_WORDS):
            tx_type = 4
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        drafts.append(DraftTransaction(
            tx_type=tx_type,
            amount=amount,
            currency=normalize_currency(item.get("currency"), f"{merchant} {note}"),
            date=normalize_date(item.get("date"), now),
            time=normalize_time(item.get("time")),
            merchant=merchant or "聊天记账",
            note=note,
            account_hint=_account_hint(str(item.get("account_hint") or ""), text),
            confidence=max(0.0, min(1.0, confidence)),
        ))
    return drafts


def _fallback_parse(text: str, now: datetime | None = None) -> list[DraftTransaction]:
    """No model available: only accept a message with an explicit amount."""
    if any(word in text for word in BALANCE_WORDS) and not re.search(
            r"(?:支付|消费|付款|支出|收入|入账|退款)", text):
        return []
    matches = list(_RE_AMOUNT.finditer(text))
    if not matches:
        return []
    # Prefer an amount carrying a currency marker.
    match = next((m for m in matches if re.search(r"[¥￥元块]", m.group(0))), matches[0])
    amount = _amount(match.group(1))
    if amount is None:
        return []
    merchant = _fallback_merchant(text, match.group(0))
    return [DraftTransaction(
        tx_type=_tx_type(None, text),
        amount=amount,
        currency=normalize_currency("", text),
        date=normalize_date(None, now),
        merchant=merchant or "聊天记账",
        note=text[:200],
        account_hint=_account_hint(text),
        confidence=0.4,
    )]


def extract(text: str, config: dict | None = None,
            now: datetime | None = None) -> list[DraftTransaction]:
    """Extract drafts from one message (text or OCR output)."""
    config = config or {}
    now = _now(now)
    raw = (text or "").strip()
    if not raw:
        return []

    drafts = _fast_parse(raw, now)
    if drafts:
        return drafts

    intake = config.get("intake") or {}
    use_llm = intake.get("use_llm", config.get("llm_enabled", True))
    if use_llm:
        try:
            drafts = _llm_extract(raw, config, now)
            if drafts:
                return drafts
        except Exception as exc:  # noqa: BLE001 - fall back, never fail the daemon
            logger.warning("Intake LLM failed: %s", exc)

    return _fallback_parse(raw, now)