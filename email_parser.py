"""Parse CMB bank emails into structured transaction records.

Supported formats:
  1. Plain-text consumption notification
  2. Plain-text transfer notification (资金归集)
  3. Plain-text income notification (工资入账)
  4. Plain-text deduction notification (扣款/保险)
  5. Plain-text auto-transfer to external account (自动转账)
  6. Credit card monthly statement HTML with multiple transactions
"""

import re
import logging
from datetime import datetime
from dataclasses import dataclass

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class Transaction:
    account_suffix: str
    date: str
    time: str
    merchant: str
    amount: float
    balance: float | None
    is_credit_card: bool = False
    raw_text: str = ""
    tx_type: int = 1  # 1=expense, 2=income, 3=transfer
    target_account_suffix: str = ""

    def to_api_payload(self, account_id: int, category_id: int | None,
                       target_account_id: int | None = None,
                       note_extra: str = "", confirmed: bool = True) -> dict:
        payload = {
            "type": self.tx_type,
            "amount": round(self.amount, 2),
            "accountId": account_id,
            "transactionDate": self.date,
            "transactionTime": self.time,
            "note": self.raw_text + (note_extra if note_extra else ""),
            "isConfirmed": confirmed,
        }
        if self.tx_type == 1:
            payload["merchantName"] = self.merchant
        if self.tx_type == 3 and target_account_id:
            payload["targetAccountId"] = target_account_id
        if self.tx_type in (1, 2) and category_id:
            payload["categoryId"] = category_id
        return payload


# Format 1: Standard consumption
# 您账户8338于04月15日01:34在财付通-微信支付-与秀同行图书快捷支付49.00元，余额3769.48
RE_FMT1 = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})\s*在(.+?)(?=\d+\.\d{2}元，余额)(\d+\.\d{2})元，余额(\d+\.\d{2})"
)
# Transfer: 资金归集
RE_FMT1_TRANSFER = re.compile(
    r"您账户(\d+)\s*定制的资金归集执行成功，尾号(\d+)向中心账户\1转账人民币(\d+\.\d{2})元，截至(\d{2})月(\d{2})日(\d{2}):(\d{2})，账户余额(\d+\.\d{2})"
)
# Income: 工资入账
RE_FMT1_INCOME = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})\s*入账(.+?)，人民币(\d+\.\d{2})，余额人民币(\d+\.\d{2})"
)
# Deduction: 扣款/保险
RE_FMT1_DEDUCTION = re.compile(
    r"您账户(\d+)\s*于(\d{4})年(\d{2})月(\d{2})日(\d{2}):(\d{2}):(\d{2})\s*扣款人民币(\d+\.\d{2})，商户：(.+?)，余额人民币(\d+\.\d{2})"
)
# Auto-transfer to external account
RE_FMT1_AUTO_TRANSFER = re.compile(
    r"您账户(\d+)\s*定制的自动转账执行成功（收款人(.+?)，收款账号(\d+)，金额(\d+\.\d{2})），账户余额(\d+\.\d{2})"
)


def parse_format1(text: str, year: int | None = None) -> list[Transaction]:
    year = year or datetime.now().year
    txs = []

    # Standard consumption
    for line in text.strip().splitlines():
        m = RE_FMT1.search(line)
        if m:
            card_tail, month, day, hour, minute, merchant, amount, balance = m.groups()
            txs.append(Transaction(
                account_suffix=card_tail,
                date=f"{year}-{int(month):02d}-{int(day):02d}",
                time=f"{hour}:{minute}:00",
                merchant=merchant.rstrip(),
                amount=float(amount), balance=float(balance),
                is_credit_card=False, raw_text=line.strip(),
            ))

    # Transfer (资金归集)
    m = RE_FMT1_TRANSFER.search(text)
    if m:
        card_tail, from_tail, amount, month, day, hour, minute, balance = m.groups()
        txs.append(Transaction(
            account_suffix=from_tail,
            target_account_suffix=card_tail,
            date=f"{year}-{int(month):02d}-{int(day):02d}",
            time=f"{hour}:{minute}:00",
            merchant=f"资金归集(尾号{from_tail}->尾号{card_tail})",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=3,
        ))

    # Income (工资入账)
    m = RE_FMT1_INCOME.search(text)
    if m:
        card_tail, month, day, hour, minute, category, amount, balance = m.groups()
        txs.append(Transaction(
            account_suffix=card_tail,
            date=f"{year}-{int(month):02d}-{int(day):02d}",
            time=f"{hour}:{minute}:00",
            merchant=category.rstrip(),
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=2,
        ))

    # Deduction (扣款/保险)
    m = RE_FMT1_DEDUCTION.search(text)
    if m:
        card_tail, year_f, month, day, hour, minute, second, amount, merchant, balance = m.groups()
        txs.append(Transaction(
            account_suffix=card_tail,
            date=f"{year_f}-{int(month):02d}-{int(day):02d}",
            time=f"{hour}:{minute}:{second}",
            merchant=merchant.strip(),
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(),
        ))

    # Auto-transfer to external account (自动转账)
    m = RE_FMT1_AUTO_TRANSFER.search(text)
    if m:
        card_tail, payee_name, payee_account, amount, balance = m.groups()
        today = datetime.now()
        txs.append(Transaction(
            account_suffix=card_tail,
            date=today.strftime("%Y-%m-%d"),
            time="",
            merchant=f"转账-{payee_name.strip()}",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(),
            tx_type=1,
        ))

    # Fallback
    if not txs:
        m = RE_FMT1.search(text)
        if m:
            card_tail, month, day, hour, minute, merchant, amount, balance = m.groups()
            txs.append(Transaction(
                account_suffix=card_tail,
                date=f"{year}-{int(month):02d}-{int(day):02d}",
                time=f"{hour}:{minute}:00",
                merchant=merchant.rstrip(),
                amount=float(amount), balance=float(balance),
                is_credit_card=False, raw_text=text.strip(),
            ))
    return txs


# Format 2: Credit card statement HTML
RE_FMT2_AMOUNT = re.compile(r"CNY\s+(\d+\.\d{2})")
RE_FMT2_MERCHANT = re.compile(r"尾号(\d+)\s+消费\s+(.+)")
RE_FMT2_TIME = re.compile(r"^(\d{2}:\d{2}:\d{2})$")
RE_FMT2_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")


def parse_format2(html: str) -> list[Transaction]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n", strip=True)

    stmt_date = datetime.now().strftime("%Y-%m-%d")
    dm = RE_FMT2_DATE.search(text)
    if dm:
        y, m, d = dm.groups()
        stmt_date = f"{y}-{int(m):02d}-{int(d):02d}"

    txs = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    for i, line in enumerate(lines):
        am = RE_FMT2_AMOUNT.search(line)
        if not am:
            continue
        amount_str = am.group(1)

        merchant = ""
        card_tail = ""
        if i + 1 < len(lines):
            mm = RE_FMT2_MERCHANT.search(lines[i + 1])
            if mm:
                card_tail = mm.group(1)
                merchant = mm.group(2).strip()
        if not merchant:
            continue

        time_str = ""
        if i > 0:
            tm = RE_FMT2_TIME.search(lines[i - 1])
            if tm:
                time_str = tm.group(1)

        if txs and txs[-1].merchant == merchant and txs[-1].amount == float(amount_str):
            continue

        txs.append(Transaction(
            account_suffix=card_tail,
            date=stmt_date,
            time=time_str,
            merchant=merchant,
            amount=float(amount_str),
            balance=None,
            is_credit_card=True,
            raw_text=f"{lines[i-1] if i > 0 else ''} {lines[i]} {lines[i+1] if i+1 < len(lines) else ''}",
        ))
    return txs


def parse_email(html_body: str, year: int | None = None) -> list[Transaction]:
    soup = BeautifulSoup(html_body, "html.parser")
    text = soup.get_text(separator="\n", strip=True)

    if "消费明细" in text and "尾号" in text:
        return parse_format2(html_body)
    if "您账户" in text and "余额" in text:
        return parse_format1(text, year)
    logger.warning("Unknown email format: %.200s", text)
    return []
