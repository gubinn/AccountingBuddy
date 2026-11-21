"""Main orchestrator: fetch emails -> parse -> classify -> create transactions."""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from annotations import annotations_for
from api_client import APIClient, APIError
from category_mapper import (CategoryMapper, build_initial_mapping,
                             build_merchant_map)
from email_fetcher import fetch_emails, mark_processed
from email_parser import INVEST_SUFFIX, Transaction, parse_email
from llm_classifier import OllamaClassifier

logger = logging.getLogger("accounting-buddy")
ROOT = Path(__file__).parent.resolve()


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()],
    )


def load_config() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f) or {}


def load_env() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "email_host": os.environ["EMAIL_HOST"],
        "email_port": int(os.environ["EMAIL_PORT"]),
        "email_user": os.environ["EMAIL_USER"],
        "email_password": os.environ["EMAIL_PASSWORD"],
        "api_base_url": os.environ["API_BASE_URL"],
        "api_key": os.environ["API_KEY"],
    }


def save_config(config: dict):
    with open(ROOT / "config.yaml", "w") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)


def _parse_email_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _resolve_account(suffix: str, account_map: dict, default_account,
                     investment_account):
    if not suffix:
        return None
    if suffix in account_map:
        return account_map[suffix]
    if suffix == INVEST_SUFFIX:
        return investment_account
    return default_account


def build_entries(txs: list[Transaction], uid: int, provident_fund_card: str | None):
    """Return [(external_id, transaction)] in posting order.

    Provident-fund deposits are not notified by email; the monthly 资金归集
    transfer out of the provident-fund card is the only signal. For each such
    transfer, synthesize a matching income on the same card *before* the
    transfer, so the sweep has sufficient balance and the amount is counted as
    income. externalId stays stable (`cmb:<uid>:pf:<idx>`) for idempotent replay.
    """
    entries: list[tuple[str, Transaction]] = []
    pf_card = str(provident_fund_card) if provident_fund_card else None
    for idx, tx in enumerate(txs):
        if pf_card and tx.tx_type == 3 and tx.account_suffix == pf_card:
            pf_tx = Transaction(
                account_suffix=tx.account_suffix,
                date=tx.date,
                time=tx.time,
                merchant="公积金入账",
                amount=tx.amount,
                balance=None,
                raw_text="公积金入账（按资金归集金额推算）",
                tx_type=2,
            )
            entries.append((f"cmb:{uid}:pf:{idx}", pf_tx))
        entries.append((f"cmb:{uid}:{idx}", tx))
    return entries


def run(dry_run: bool = False, verbose: bool = False, full_sync: bool = False):
    setup_logging(verbose)
    logger.info("=== accounting-buddy started (dry_run=%s, full_sync=%s) ===",
                dry_run, full_sync)

    env = load_env()
    config = load_config()
    api = APIClient(env["api_base_url"], env["api_key"])

    try:
        mapping_changed, category_types, _categories = build_initial_mapping(api, config)
        if mapping_changed and not dry_run:
            save_config(config)
            logger.info("Updated category_mapping in config.yaml")

        # Layer 2: learned merchant -> category mapping from BudgetPilot.
        # Only non-"其他" categories are trusted (user corrections / aliases).
        merchants: list[dict] = []
        try:
            merchants = api.get_merchants()
        except Exception as e:
            logger.warning("Failed to load merchants for learned mapping: %s", e)
        other_ids = {config.get("fallback_expense_category_id"),
                     config.get("fallback_income_category_id")}
        other_ids |= {cid for cid, info in _categories.items()
                      if info["name"] in ("其他支出", "其他收入")}
        other_ids.discard(None)
        merchant_map = build_merchant_map(merchants, category_types, excluded=other_ids)
        logger.info("Learned merchant mappings: %d (from %d merchants)",
                    len(merchant_map), len(merchants))

        llm = OllamaClassifier(config, config.get("category_mapping") or {}, category_types)
        mapper = CategoryMapper(
            config.get("category_mapping") or {}, category_types,
            merchant_map=merchant_map, llm=llm, categories=_categories,
        )
        account_map = config.get("account_mapping") or {}
        default_account = config.get("default_account_id")
        investment_account = config.get("investment_account_id")
        fallback_expense = config.get("fallback_expense_category_id")
        fallback_income = config.get("fallback_income_category_id")
        logger.info("Account mapping: %s (default=%s, investment=%s)",
                    account_map, default_account, investment_account)

        # All transactions are unconfirmed during full_sync to avoid balance conflicts
        force_unconfirmed = full_sync

        try:
            emails, max_scanned_uid = fetch_emails(
                host=env["email_host"], port=env["email_port"],
                user=env["email_user"], password=env["email_password"],
                folder=config.get("email_folder", "INBOX"),
                senders=config.get("cmb_senders", []),
                full_sync=full_sync, dry_run=dry_run,
            )
        except Exception as e:
            logger.error("Failed to fetch emails: %s", e)
            return

        if not emails:
            logger.info("No new CMB emails to process")
            # Advance past scanned non-CMB mail so it is not downloaded again
            if not dry_run and not full_sync and max_scanned_uid > 0:
                try:
                    mark_processed(
                        host=env["email_host"], port=env["email_port"],
                        user=env["email_user"], password=env["email_password"],
                        folder=config.get("email_folder", "INBOX"),
                        uids=[], state_uid=max_scanned_uid,
                    )
                except Exception as e:
                    logger.error("Failed to advance email cursor: %s", e)
            return

        stats = {"total": 0, "created": 0, "failed": 0, "skipped": 0, "emails_failed": 0}
        successful_uids: list[int] = []
        contiguous_uid = 0
        first_failure_seen = False

        for email in sorted(emails, key=lambda e: e["uid"]):
            uid = email["uid"]
            logger.info("Processing email UID %d from %s: %s",
                        uid, email["sender"], email["subject"])
            email_dt = _parse_email_datetime(email.get("date"))
            txs = parse_email(email["html_body"], ref_date=email_dt)
            entries = build_entries(txs, uid, config.get("provident_fund_card"))
            stats["total"] += len(entries)

            if not entries:
                logger.warning("No transactions parsed from email UID %d, skipping "
                               "(use --dry-run -v and check the format)", uid)
                stats["skipped"] += 1
                if not dry_run and not full_sync:
                    successful_uids.append(uid)
                    if not first_failure_seen:
                        contiguous_uid = uid
                continue

            email_ok = True
            for external_id, tx in entries:
                account_id = _resolve_account(tx.account_suffix, account_map,
                                              default_account, investment_account)
                if account_id is None:
                    logger.error("No account mapping for suffix %s (tx %.2f %s); "
                                 "add it to account_mapping, set default_account_id, "
                                 "or set investment_account_id for INVEST",
                                 tx.account_suffix, tx.amount, tx.merchant)
                    stats["failed"] += 1
                    email_ok = False
                    continue

                target_account_id = None
                if tx.tx_type == 3:
                    if not tx.target_account_suffix:
                        logger.error("Transfer without target card suffix: %s", tx.merchant)
                        stats["failed"] += 1
                        email_ok = False
                        continue
                    target_account_id = _resolve_account(tx.target_account_suffix, account_map,
                                                     default_account, investment_account)
                    if target_account_id is None:
                        logger.error("No account mapping for transfer target %s; "
                                     "set investment_account_id if it is INVEST",
                                     tx.target_account_suffix)
                        stats["failed"] += 1
                        email_ok = False
                        continue

                category_id = mapper.classify(tx.merchant, tx.tx_type, note=tx.raw_text)
                if category_id is None:
                    if tx.tx_type in (1, 4):
                        category_id = fallback_expense
                    elif tx.tx_type == 2:
                        category_id = fallback_income
                if tx.tx_type in (1, 2, 4) and category_id is None:
                    logger.error("No category for tx %.2f %s (type=%d); configure "
                                 "fallback_expense_category_id / fallback_income_category_id",
                                 tx.amount, tx.merchant, tx.tx_type)
                    stats["failed"] += 1
                    email_ok = False
                    continue

                tags, ext_fields = annotations_for(tx)

                # 对外转账先标记待复核：用户可在网页确认成“往来款/借出”，
                # 或改为“人情往来/亲属转账”。待复核期间不计入收支/预算。
                review_status = None
                if tx.tx_type in (1, 2) and (tx.merchant.startswith("转账-")
                                             or tx.merchant.startswith("转入-")):
                    review_status = "PENDING"
                    if tx.tx_type == 1:
                        loan_category = mapper.resolve_category("往来款/借出")
                        if loan_category:
                            category_id = loan_category

                payload = tx.to_api_payload(
                    account_id, category_id, target_account_id,
                    confirmed=not force_unconfirmed, external_id=external_id,
                    tags=tags, ext_fields=ext_fields,
                    review_status=review_status,
                )

                if dry_run:
                    logger.info("[DRY RUN] Would create: %s", payload)
                    stats["created"] += 1
                    continue

                try:
                    result = api.create_transaction_with_retry(payload)
                    tx_id = result.get("id", "?")
                    status_str = "confirmed" if not force_unconfirmed else "pending"
                    logger.info("Created tx %s (externalId=%s): %.2f %s @ %s [%s]",
                                tx_id, external_id, tx.amount, tx.merchant, tx.date, status_str)
                    stats["created"] += 1
                except APIError as e:
                    logger.error("API error for tx %.2f %s: %s", tx.amount, tx.merchant, e.message)
                    stats["failed"] += 1
                    email_ok = False
                except Exception as e:
                    logger.error("Unexpected error for tx %.2f %s: %s", tx.amount, tx.merchant, e)
                    stats["failed"] += 1
                    email_ok = False

            if not dry_run and not full_sync:
                if email_ok:
                    successful_uids.append(uid)
                    if not first_failure_seen:
                        contiguous_uid = uid
                else:
                    stats["emails_failed"] += 1
                    first_failure_seen = True

        # Mark emails as read and advance the cursor only after successful
        # recording; failed emails stay in the range and are retried next run
        # (externalId makes the retry idempotent).
        if not dry_run and not full_sync and (successful_uids or max_scanned_uid > 0):
            # With no failures the cursor can safely move past all scanned mail
            # (including trailing non-CMB messages); otherwise it stops at the
            # last contiguous success so the failed email is retried.
            state_uid = max_scanned_uid if not first_failure_seen else contiguous_uid
            try:
                mark_processed(
                    host=env["email_host"], port=env["email_port"],
                    user=env["email_user"], password=env["email_password"],
                    folder=config.get("email_folder", "INBOX"),
                    uids=successful_uids, state_uid=state_uid,
                )
            except Exception as e:
                logger.error("Failed to mark emails as processed: %s", e)

        logger.info("=== Summary ===")
        logger.info("Emails processed: %d (failed: %d)", len(emails), stats["emails_failed"])
        logger.info("Transactions: total=%d created=%d failed=%d skipped=%d",
                    stats["total"], stats["created"], stats["failed"], stats["skipped"])
    finally:
        api.close()


def main():
    dry_run = "--dry-run" in sys.argv
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    full_sync = "--full-sync" in sys.argv
    run(dry_run=dry_run, verbose=verbose, full_sync=full_sync)


if __name__ == "__main__":
    main()