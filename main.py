"""Main orchestrator: fetch emails -> parse -> classify -> create transactions."""

import os
import sys
import logging
import yaml
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from api_client import APIClient, APIError
from email_fetcher import fetch_emails
from email_parser import parse_email
from category_mapper import CategoryMapper, build_initial_mapping

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
        return yaml.safe_load(f)


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


def run(dry_run: bool = False, verbose: bool = False, full_sync: bool = False):
    setup_logging(verbose)
    logger.info("=== accounting-buddy started ===")

    env = load_env()
    config = load_config()
    api = APIClient(env["api_base_url"], env["api_key"])

    build_initial_mapping(api, config)
    if config.get("category_mapping"):
        save_config(config)
    mapper = CategoryMapper(config.get("category_mapping", {}))
    account_map = config.get("account_mapping", {})
    logger.info("Account mapping: %s", account_map)

    # All transactions are unconfirmed during full_sync to avoid balance conflicts
    force_unconfirmed = full_sync

    emails = fetch_emails(
        host=env["email_host"], port=env["email_port"],
        user=env["email_user"], password=env["email_password"],
        folder=config.get("email_folder", "INBOX"),
        senders=config.get("cmb_senders", []),
        full_sync=dry_run or full_sync,
    )

    if not emails:
        logger.info("No new emails to process")
        return

    stats = {"total": 0, "created": 0, "failed": 0, "skipped": 0}

    for email in emails:
        logger.info("Processing email from %s: %s", email["sender"], email["subject"])
        txs = parse_email(email["html_body"])
        stats["total"] += len(txs)

        if not txs:
            logger.warning("No transactions parsed from email UID %d", email["uid"])
            stats["skipped"] += 1
            continue

        for tx in txs:
            account_id = account_map.get(tx.account_suffix, 637)

            target_account_id = None
            if tx.tx_type == 3 and tx.target_account_suffix:
                target_account_id = account_map.get(tx.target_account_suffix, 637)

            category_id = mapper.classify(tx.merchant)
            if category_id is None:
                if tx.tx_type == 1:
                    category_id = 261  # "其他支出"
                elif tx.tx_type == 2:
                    category_id = 255  # "其他收入"
            note_extra = ""

            if dry_run:
                logger.info("[DRY RUN] Would create: %s",
                            tx.to_api_payload(account_id, category_id, target_account_id, note_extra))
                stats["created"] += 1
                continue

            confirmed = not force_unconfirmed
            payload = tx.to_api_payload(account_id, category_id, target_account_id, note_extra, confirmed)
            try:
                result = api.create_transaction_with_retry(payload)
                tx_id = result.get("id", "?")
                status_str = "confirmed" if confirmed else "pending"
                logger.info("Created tx %s: %.2f %s @ %s [%s]",
                            tx_id, tx.amount, tx.merchant, tx.date, status_str)
                stats["created"] += 1
            except APIError as e:
                logger.error("API error for tx %.2f %s: %s", tx.amount, tx.merchant, e.message)
                stats["failed"] += 1
            except Exception as e:
                logger.error("Unexpected error: %s", e)
                stats["failed"] += 1

    logger.info("=== Summary ===")
    logger.info("Total emails processed: %d", len(emails))
    logger.info("Transactions: total=%d created=%d failed=%d skipped=%d",
                stats["total"], stats["created"], stats["failed"], stats["skipped"])


def main():
    dry_run = "--dry-run" in sys.argv
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    full_sync = "--full-sync" in sys.argv
    run(dry_run=dry_run, verbose=verbose, full_sync=full_sync)


if __name__ == "__main__":
    main()
