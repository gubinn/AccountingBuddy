"""历史漏记交易补录（一次性脚本）。

数据来源：reconciliation_backfill_preview_20261122.csv
只补 CNY，HKD 等用户下载信用卡对账单后按人民币入账。
默认 dry-run；加 --apply 才写库。externalId 用内容哈希，可安全重复执行。
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
from api_client import APIClient, APIError
from email_fetcher import _decode_header_value  # noqa: F401  (保持依赖显式)

PREVIEW = "/home/gubin/reconciliation_backfill_preview_20261122.csv"
ACCOUNT_ID = 5  # 招商银行信用卡(0886)


def channel_for(raw: str) -> str | None:
    for keyword, channel in (("支付宝", "支付宝"), ("京东支付", "京东支付"),
                             ("财付通", "微信支付"), ("滴滴支付", "滴滴支付")):
        if keyword in (raw or ""):
            return channel
    return None


def selected(row: dict) -> bool:
    if row["currency"] != "CNY":
        return False
    if row["action"].startswith("补录"):
        return True
    # 用户确认 13:22:18 的 48.50 是第二笔真实退款，覆盖预览里的“跳过”
    return (row["date"] == "2026-05-30" and row["time"] == "13:22:18"
            and row["amount"] == "48.50")


def payload(row: dict) -> dict:
    tx_type = 4 if row["kind"] == "退货" else 1
    ext = {"backfill": "2026-11-22", "来源邮件": row["source"]}
    channel = channel_for(row["raw"])
    if channel:
        ext["channel"] = channel
    return {
        "type": tx_type,
        "amount": float(row["amount"]),
        "currency": "CNY",
        "transactionDate": row["date"],
        "transactionTime": row["time"],
        "accountId": ACCOUNT_ID,
        "categoryId": int(row["cid"]),
        "merchantName": row["merchant"][:50],
        "autoCreateMerchant": True,
        "note": row["raw"][:200],
        "externalId": row["ext"],
        "extFields": ext,
        "tags": ["历史补录"],
        "isConfirmed": True,
        "reviewStatus": "REVIEWED",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写库")
    args = parser.parse_args()

    rows = [r for r in csv.DictReader(open(PREVIEW, encoding="utf-8-sig")) if selected(r)]
    print(f"待补录 {len(rows)} 笔（CNY）")
    for r in rows:
        print(f"  {r['date']} {r['time']} {r['kind']} {r['amount']:>9} "
              f"{r['merchant']} -> {r['category']}({r['cid']}) {r['ext']}")
    if not args.apply:
        print("\nDRY RUN，未写库。加 --apply 执行。")
        return

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    api = APIClient(os.environ["API_BASE_URL"], os.environ["API_KEY"])
    try:
        for r in rows:
            try:
                result = api.create_transaction_with_retry(payload(r))
                print(f"OK  {r['date']} {r['time']} {r['amount']} {r['merchant']} -> id={result.get('id')}")
            except APIError as e:
                print(f"FAIL {r['date']} {r['time']} {r['amount']} {r['merchant']}: {e.message}")
            except Exception as e:
                print(f"FAIL {r['date']} {r['time']} {r['amount']} {r['merchant']}: {e}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
