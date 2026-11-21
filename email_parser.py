"""Parse CMB bank emails into structured transaction records.

Supported formats:
  1. Plain-text consumption notification
  2. Plain-text transfer notification (资金归集)
  3. Plain-text income notification (工资入账)
  4. Plain-text deduction notification (扣款/保险)
  5. Plain-text auto-transfer to external account (自动转账)
  6. Credit card statement HTML with multiple transactions

All dates are resolved against the email's own Date header (ref_date) rather
than "now", so messages processed late or across a year boundary keep the
correct date.
"""

import re
import logging
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


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
    tx_type: int = 1  # 1=expense, 2=income, 3=transfer, 4=refund
    target_account_suffix: str = ""

    def to_api_payload(self, account_id: int, category_id: int | None,
                       target_account_id: int | None = None,
                       note_extra: str = "", confirmed: bool = True,
                       external_id: str | None = None,
                       tags: list[str] | None = None,
                       ext_fields: dict | None = None,
                       review_status: str | None = None) -> dict:
        note = (self.raw_text + (note_extra or "")).strip()
        payload = {
            "type": self.tx_type,
            "amount": round(self.amount, 2),
            "accountId": account_id,
            "transactionDate": self.date,
            "note": note[:200],
            "isConfirmed": confirmed,
        }
        if TIME_RE.match(self.time or ""):
            payload["transactionTime"] = self.time
        if external_id:
            payload["externalId"] = external_id
        if review_status:
            payload["reviewStatus"] = review_status
        if tags:
            payload["tags"] = tags
        if ext_fields:
            payload["extFields"] = ext_fields
        if self.tx_type in (1, 4):
            payload["merchantName"] = self.merchant.strip()[:50]
        if self.tx_type == 3 and target_account_id:
            payload["targetAccountId"] = target_account_id
        if self.tx_type in (1, 2, 4) and category_id:
            payload["categoryId"] = category_id
        return payload


def _resolve_date(month: int | str, day: int | str, ref: datetime | None) -> str:
    """Resolve a month/day to a full date using the email date as reference.

    Bank notifications never describe a future transaction, so if the naive
    date lands after the email date we assume it belongs to the previous year.
    """
    ref_date = (ref or datetime.now()).date()
    m, d = int(month), int(day)
    try:
        candidate = date(ref_date.year, m, d)
    except ValueError:
        return f"{ref_date.year}-{m:02d}-{d:02d}"
    if candidate > ref_date:
        try:
            candidate = date(ref_date.year - 1, m, d)
        except ValueError:
            pass
    return candidate.strftime("%Y-%m-%d")


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
# Outgoing transfer to a person (转账汇款)
RE_FMT1_OUT_TRANSFER = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})转账汇款人民币(\d+\.\d{2})元?，余额(\d+\.\d{2})元?，收款人：(.+?)(?:[，,]|$)"
)
# Outgoing real-time interbank transfer
RE_FMT1_OUT_INTERBANK = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})实时转至他行人民币(\d+\.\d{2})元?，余额(\d+\.\d{2})元?，收款人(.+?)(?:[，,]|$)"
)
# Incoming transfer from another own account
RE_FMT1_IN_OWN = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日收到本行转入人民币(\d+\.\d{2})元?，余额(\d+\.\d{2})元?，付方(.+?)(?:[，,]|$)"
)
# Credit card repayment with the target card suffix
RE_FMT1_CC_REPAY = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})向尾号为(\d+)的信用卡还款人民币(\d+\.\d{2})元?，余额(\d+\.\d{2})元?"
)
# Credit card repayment without target suffix
RE_FMT1_CC_REPAY_NOTARGET = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})信用卡还款交易人民币(\d+\.\d{2})元?，余额(\d+\.\d{2})元?"
)
# UnionPay incoming
RE_FMT1_UP_IN = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})银联入账人民币(\d+\.\d{2})元?，余额(\d+\.\d{2})元?(?:（(.+?)）)?"
)
# UnionPay outgoing
RE_FMT1_UP_OUT = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})银联扣款人民币(\d+\.\d{2})元?，余额(\d+\.\d{2})元?(?:（(.+?)）)?"
)
# Incoming real-time interbank transfer
RE_FMT1_IN_INTERBANK = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日他行实时转入人民币(\d+\.\d{2})元?，余额(\d+\.\d{2})元?，付方(.+?)(?:[，,]|$)"
)
# Deduction with a purpose but no 商户 field
RE_FMT1_DEDUCTION_PURPOSE = re.compile(
    r"您账户(\d+)\s*于(?:(\d{4})年)?(\d{2})月(\d{2})日(\d{2}):(\d{2})(?::(\d{2}))?扣款人民币(\d+\.\d{2})元?，用于(.+?)[，,]\s*余额(?:人民币)?(\d+\.\d{2})元?"
)
# Generic credit "入账<amount>元"
RE_FMT1_CREDIT_GENERIC = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})入账(\d+\.\d{2})元?，余额(\d+\.\d{2})元?(?:（(.+?)）)?"
)
# Apple Pay / online card payment (device card suffix in parentheses)
RE_FMT1_DEVICE_PAY = re.compile(
    r"您账户(\d+)(?:（设备卡号(\d+)）)?\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})"
    r"(线上免密支付|线上支付|线下支付|扫码支付|银联在线支付)人民币(\d+\.\d{2})，"
    r"余额(\d+\.\d{2})(?:，(.+))?"
)

# ===== Asset movements (booked as transfers with the investment account) =====
INVEST_SUFFIX = "INVEST"

# 大额存单到期转入活期：本金回银行，利息记收入
RE_ASSET_CD_MATURITY = re.compile(
    r"您账户(\d+)大额存单于(\d{2})月(\d{2})日到期转入活期，"
    r"人民币本金(\d+\.\d{2})元、利息(\d+\.\d{2})元，活期余额(\d+\.\d{2})元"
)
# 银行活期 -> 证券保证金
RE_ASSET_TO_SECURITIES = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(?:(\d{2}):(\d{2}))?发生银行活期转证券保证金"
    r"人民币(\d+(?:\.\d{2})?)，余额(\d+(?:\.\d{2})?)"
)
# 证券保证金 -> 银行活期
RE_ASSET_FROM_SECURITIES = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})发生证券保证金转银行活期"
    r"人民币(\d+\.\d{2})，余额(\d+\.\d{2})"
)
# 自助购汇：人民币减少，港币现汇进入投资账户
RE_ASSET_FX_BUY = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})发生自助购汇，"
    r"购汇港币现汇(\d+\.\d{2})，本次购汇人民币减少(\d+\.\d{2})，人民币余额(\d+\.\d{2})"
)
# 港币柜台取现（邮件只给港币金额，无法可靠折算成人民币，保留告警）
RE_ASSET_HKD_WITHDRAW = re.compile(
    r"您账户(\d+)\s*于(\d{2})月(\d{2})日(\d{2}):(\d{2})柜台取款港币(\d+)，余额(\d+(?:\.\d{2})?)"
)


def _strip_parenthetical(text: str) -> str:
    return re.sub(r"（.*?）|\(.*?\)", "", text or "").strip()


def parse_format1(text: str, ref_date: datetime | None = None) -> list[Transaction]:
    txs = []

    # Standard consumption (one per line; an email may contain several)
    for line in text.strip().splitlines():
        m = RE_FMT1.search(line)
        if m:
            card_tail, month, day, hour, minute, merchant, amount, balance = m.groups()
            txs.append(Transaction(
                account_suffix=card_tail,
                date=_resolve_date(month, day, ref_date),
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
            date=_resolve_date(month, day, ref_date),
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
            date=_resolve_date(month, day, ref_date),
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

    # Auto-transfer to external account (自动转账): the email body has no
    # timestamp, so use the email's own date/time instead of "now".
    m = RE_FMT1_AUTO_TRANSFER.search(text)
    if m:
        card_tail, payee_name, payee_account, amount, balance = m.groups()
        ref_dt = ref_date or datetime.now()
        txs.append(Transaction(
            account_suffix=card_tail,
            date=ref_dt.strftime("%Y-%m-%d"),
            time=ref_dt.strftime("%H:%M:%S"),
            merchant=f"转账-{payee_name.strip()}",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(),
            tx_type=1,
        ))

    # 转账汇款（对外）
    for m in RE_FMT1_OUT_TRANSFER.finditer(text):
        card, month, day, hour, minute, amount, balance, payee = m.groups()
        txs.append(Transaction(
            account_suffix=card, date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant=f"转账-{payee.strip()}",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(),
        ))

    # 实时转至他行
    for m in RE_FMT1_OUT_INTERBANK.finditer(text):
        card, month, day, hour, minute, amount, balance, payee = m.groups()
        txs.append(Transaction(
            account_suffix=card, date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant=f"转账-{payee.strip()}",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(),
        ))

    # 收到本行转入
    for m in RE_FMT1_IN_OWN.finditer(text):
        card, month, day, amount, balance, payer = m.groups()
        txs.append(Transaction(
            account_suffix=card, date=_resolve_date(month, day, ref_date),
            time="", merchant=f"转入-{_strip_parenthetical(payer)}",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=2,
        ))

    # 信用卡还款（邮件指明了目标卡尾号，记一笔转账）
    for m in RE_FMT1_CC_REPAY.finditer(text):
        card, month, day, hour, minute, target, amount, balance = m.groups()
        txs.append(Transaction(
            account_suffix=card, target_account_suffix=target,
            date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant=f"信用卡还款(尾号{target})",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=3,
        ))

    # 信用卡还款交易（无目标卡信息）
    for m in RE_FMT1_CC_REPAY_NOTARGET.finditer(text):
        card, month, day, hour, minute, amount, balance = m.groups()
        txs.append(Transaction(
            account_suffix=card, date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant="信用卡还款",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(),
        ))

    # 银联入账
    for m in RE_FMT1_UP_IN.finditer(text):
        card, month, day, hour, minute, amount, balance, note = m.groups()
        merchant = f"银联入账-{_strip_parenthetical(note)}" if note else "银联入账"
        txs.append(Transaction(
            account_suffix=card, date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant=merchant,
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=2,
        ))

    # 银联扣款
    for m in RE_FMT1_UP_OUT.finditer(text):
        card, month, day, hour, minute, amount, balance, note = m.groups()
        merchant = f"银联扣款-{_strip_parenthetical(note)}" if note else "银联扣款"
        txs.append(Transaction(
            account_suffix=card, date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant=merchant,
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(),
        ))

    # 他行实时转入
    for m in RE_FMT1_IN_INTERBANK.finditer(text):
        card, month, day, amount, balance, payer = m.groups()
        txs.append(Transaction(
            account_suffix=card, date=_resolve_date(month, day, ref_date),
            time="", merchant=f"转入-{_strip_parenthetical(payer)}",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=2,
        ))

    # 扣款（仅说明用途，没有商户字段）
    for m in RE_FMT1_DEDUCTION_PURPOSE.finditer(text):
        card, year_f, month, day, hour, minute, second, amount, purpose, balance = m.groups()
        tx_date = (f"{year_f}-{int(month):02d}-{int(day):02d}"
                   if year_f else _resolve_date(month, day, ref_date))
        txs.append(Transaction(
            account_suffix=card, date=tx_date,
            time=f"{hour}:{minute}:{second or '00'}", merchant=purpose.strip(),
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(),
        ))

    # 通用入账（金额写在“入账”之后）
    for m in RE_FMT1_CREDIT_GENERIC.finditer(text):
        card, month, day, hour, minute, amount, balance, note = m.groups()
        merchant = f"入账-{_strip_parenthetical(note)}" if note else "入账"
        txs.append(Transaction(
            account_suffix=card, date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant=merchant,
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=2,
        ))

    # Apple Pay / 线上支付
    for m in RE_FMT1_DEVICE_PAY.finditer(text):
        card, _device, month, day, hour, minute, kind, amount, balance, desc = m.groups()
        merchant = (desc or kind).strip()
        txs.append(Transaction(
            account_suffix=card, date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant=merchant,
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(),
        ))

    # ===== Asset movements =====
    # 大额存单到期：本金从投资账户转回银行卡，利息记收入
    for m in RE_ASSET_CD_MATURITY.finditer(text):
        card, month, day, principal, interest, balance = m.groups()
        tx_date = _resolve_date(month, day, ref_date)
        txs.append(Transaction(
            account_suffix=INVEST_SUFFIX, target_account_suffix=card,
            date=tx_date, time="00:00:00", merchant="大额存单到期(本金)",
            amount=float(principal), balance=None,
            is_credit_card=False, raw_text=text.strip(), tx_type=3,
        ))
        txs.append(Transaction(
            account_suffix=card, date=tx_date, time="00:00:00",
            merchant="大额存单利息", amount=float(interest), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=2,
        ))

    # 银行活期 -> 证券保证金
    for m in RE_ASSET_TO_SECURITIES.finditer(text):
        card, month, day, hour, minute, amount, balance = m.groups()
        tx_time = f"{hour}:{minute}:00" if hour else ""
        txs.append(Transaction(
            account_suffix=card, target_account_suffix=INVEST_SUFFIX,
            date=_resolve_date(month, day, ref_date),
            time=tx_time, merchant="转证券保证金",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=3,
        ))

    # 证券保证金 -> 银行活期
    for m in RE_ASSET_FROM_SECURITIES.finditer(text):
        card, month, day, hour, minute, amount, balance = m.groups()
        txs.append(Transaction(
            account_suffix=INVEST_SUFFIX, target_account_suffix=card,
            date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant="证券保证金转活期",
            amount=float(amount), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=3,
        ))

    # 自助购汇：人民币转入投资账户（港币现汇）
    for m in RE_ASSET_FX_BUY.finditer(text):
        card, month, day, hour, minute, hkd, cny, balance = m.groups()
        txs.append(Transaction(
            account_suffix=card, target_account_suffix=INVEST_SUFFIX,
            date=_resolve_date(month, day, ref_date),
            time=f"{hour}:{minute}:00", merchant=f"购汇(港币{hkd})",
            amount=float(cny), balance=float(balance),
            is_credit_card=False, raw_text=text.strip(), tx_type=3,
        ))

    if RE_ASSET_HKD_WITHDRAW.search(text):
        logger.warning("HKD cash withdrawal not auto-booked (amount is in HKD): %.120s", text)

    # Fallback: whole-body match when no line-based match succeeded
    if not txs:
        m = RE_FMT1.search(text)
        if m:
            card_tail, month, day, hour, minute, merchant, amount, balance = m.groups()
            txs.append(Transaction(
                account_suffix=card_tail,
                date=_resolve_date(month, day, ref_date),
                time=f"{hour}:{minute}:00",
                merchant=merchant.rstrip(),
                amount=float(amount), balance=float(balance),
                is_credit_card=False, raw_text=text.strip(),
            ))
    return txs


# Format 2: Credit card statement HTML
RE_FMT2_AMOUNT = re.compile(r"CNY\s+(-?\d+\.\d{2})")
RE_FMT2_MERCHANT = re.compile(r"尾号(\d+)\s+(消费|退货|退款)\s+(.+)")
RE_FMT2_TIME = re.compile(r"^(\d{2}:\d{2}:\d{2})$")
RE_FMT2_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")


def parse_format2(html: str, ref_date: datetime | None = None) -> list[Transaction]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n", strip=True)

    stmt_date = (ref_date or datetime.now()).strftime("%Y-%m-%d")
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
        kind = "消费"
        # The merchant may be on the same line or the next/previous one
        for j in (i, i + 1, i - 1, i + 2):
            if 0 <= j < len(lines):
                mm = RE_FMT2_MERCHANT.search(lines[j])
                if mm:
                    card_tail, kind, merchant = mm.group(1), mm.group(2), mm.group(3).strip()
                    break
        if not merchant:
            continue

        time_str = ""
        for j in (i - 1, i):
            if 0 <= j < len(lines):
                tm = RE_FMT2_TIME.search(lines[j])
                if tm:
                    time_str = tm.group(1)
                    break

        # Prefer a date on the same row, fall back to the statement date
        row_date = stmt_date
        row_dm = RE_FMT2_DATE.search(line)
        if row_dm:
            y, m, d = row_dm.groups()
            row_date = f"{y}-{int(m):02d}-{int(d):02d}"

        amount = abs(float(amount_str))
        is_refund = kind in ("退货", "退款") or amount_str.startswith("-")
        tx_type = 4 if is_refund else 1

        if txs and txs[-1].merchant == merchant and txs[-1].amount == amount:
            continue

        txs.append(Transaction(
            account_suffix=card_tail,
            date=row_date,
            time=time_str,
            merchant=merchant,
            amount=amount,
            balance=None,
            is_credit_card=True,
            raw_text=f"{lines[i-1] if i > 0 else ''} {lines[i]} {lines[i+1] if i+1 < len(lines) else ''}",
            tx_type=tx_type,
        ))
    return txs


def parse_email(html_body: str, ref_date: datetime | None = None) -> list[Transaction]:
    soup = BeautifulSoup(html_body, "html.parser")
    text = soup.get_text(separator="\n", strip=True)

    if "消费明细" in text and "尾号" in text:
        return parse_format2(html_body, ref_date)
    if "您账户" in text and "余额" in text:
        return parse_format1(text, ref_date)
    logger.warning("Unknown email format: %.200s", text)
    return []