"""按信用卡对账单补录 HKD 漏记交易。

数据来源合并：
  - 预览 CSV：邮件里的精确消费时间、商户、分类和 externalId；
  - 招行信用卡电子账单 PDF：实际人民币入账金额、交易日和卡号。

账单没有时分秒，所以时间取邮件明细；金额取对账单的实际人民币入账，
不用预览里的 ECB 估算汇率。externalId 沿用预览里的内容哈希，可安全重复执行。
默认 dry-run，加 --apply 才调用后台 API 写库。

示例：
  python backfill_hkd_statement.py \
    --preview reconciliation_backfill_preview.csv \
    --statement 2026-10-credit-card.pdf \
    --account-id 5 --card-suffix 1234 --bill-label "2026年10月信用卡账单" \
    --apply

依赖：pdftotext（poppler-utils）、预览 CSV、.env 里的 API_BASE_URL / API_KEY。
预览 CSV 需要列：currency, date, time, amount, cid, merchant, ext, source。
"""
import argparse
import csv
import os
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv  # noqa: E402
from api_client import APIClient, APIError  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))

# 交易日 记账日 摘要 人民币金额 卡号末四位 交易地金额(HK)
ROW = re.compile(
    r"^\s*(?:\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+?)\s+([\d,]+\.\d{2})\s+(\d{4})\s+"
    r"([\d,]+\.\d{2})\(HK\)\s*$"
)


def statement_hkd_rows(path: str, card_suffix: str) -> dict[Decimal, dict]:
    text = subprocess.run(["pdftotext", "-layout", path, "-"],
                          check=True, capture_output=True, text=True).stdout
    rows = {}
    for line in text.splitlines():
        m = ROW.match(line)
        if not m or m.group(4) != card_suffix or "Apple Pay" not in m.group(2):
            continue
        hkd = Decimal(m.group(5).replace(",", ""))
        rows[hkd] = {
            "post_date": m.group(1),
            "desc": m.group(2).strip(),
            "cny": Decimal(m.group(3).replace(",", "")),
            "card": m.group(4),
            "hkd": hkd,
        }
    return rows


def preview_hkd_rows(path: str) -> list[dict]:
    return [r for r in csv.DictReader(open(path, encoding="utf-8-sig"))
            if r["currency"] == "HKD"]


def payload(row: dict, bill: dict, account_id: int, card_suffix: str,
            bill_label: str) -> dict:
    time = row["time"]
    hkd = Decimal(row["amount"])
    note = f"{time} HKD {hkd:.2f} 尾号{card_suffix} 消费 {bill['desc']}"
    return {
        "type": 1,
        "amount": float(bill["cny"]),
        "currency": "CNY",
        "transactionDate": row["date"],
        "transactionTime": time,
        "accountId": account_id,
        "categoryId": int(row["cid"]),
        "merchantName": row["merchant"][:50],
        "autoCreateMerchant": True,
        "note": note[:200],
        "externalId": row["ext"],
        "extFields": {
            "channel": "Apple Pay",
            "原币金额": f"{hkd:.2f} HKD",
            "信用卡卡号": card_suffix,
            "账单入账": bill_label,
            "补录来源": row["source"],
            "backfill": date.today().isoformat(),
        },
        "tags": ["历史补录", "信用卡账单补录"],
        "isConfirmed": True,
        "reviewStatus": "REVIEWED",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", required=True, help="邮件预览 CSV")
    parser.add_argument("--statement", required=True, help="招行信用卡电子账单 PDF")
    parser.add_argument("--account-id", type=int, required=True, help="入账账户 ID")
    parser.add_argument("--card-suffix", required=True, help="信用卡卡号末四位")
    parser.add_argument("--bill-label", default="信用卡账单", help="账单标识")
    parser.add_argument("--apply", action="store_true", help="真正写库")
    args = parser.parse_args()

    bills = statement_hkd_rows(args.statement, args.card_suffix)
    rows = preview_hkd_rows(args.preview)
    print(f"对账单 Apple Pay/HKD 行 {len(bills)} 笔，邮件待补录 {len(rows)} 笔")

    missing = [r for r in rows if Decimal(r["amount"]) not in bills]
    if missing:
        print("以下邮件记录在对账单中找不到同原币金额，终止：")
        for r in missing:
            print("  ", r["date"], r["time"], r["amount"], r["merchant"], r["ext"])
        sys.exit(1)

    payloads = []
    for r in rows:
        bill = bills[Decimal(r["amount"])]
        p = payload(r, bill, args.account_id, args.card_suffix, args.bill_label)
        payloads.append(p)
        print(f"  {p['transactionDate']} {p['transactionTime']} "
              f"HKD {r['amount']:>8} -> CNY {p['amount']:>9.2f}  "
              f"{p['merchantName']}  {bill['desc']}")

    if len({p["externalId"] for p in payloads}) != len(payloads):
        print("externalId 有重复，终止")
        sys.exit(1)

    if not args.apply:
        print("\nDRY RUN，未写库。加 --apply 执行。")
        return

    load_dotenv(os.path.join(ROOT, ".env"))
    api = APIClient(os.environ["API_BASE_URL"], os.environ["API_KEY"])
    try:
        for p in payloads:
            try:
                result = api.create_transaction_with_retry(p)
                print(f"OK  {p['transactionDate']} {p['transactionTime']} "
                      f"CNY {p['amount']:.2f} -> id={result.get('id')}")
            except APIError as e:
                print(f"FAIL {p['transactionDate']} {p['transactionTime']} "
                      f"CNY {p['amount']:.2f}: {e.message}")
            except Exception as e:
                print(f"FAIL {p['transactionDate']} {p['transactionTime']} "
                      f"CNY {p['amount']:.2f}: {e}")
    finally:
        api.close()


if __name__ == "__main__":
    main()