"""Backfill payment channel (extFields.channel) and method tags.

Usage:
  ./venv/bin/python annotate.py --dry-run   # preview counts only
  ./venv/bin/python annotate.py             # apply

Only adds tags and merges extFields; existing user tags/channel values are
preserved. No balance or category changes.
"""

import argparse
import logging
import os
from collections import Counter

from dotenv import load_dotenv

from annotations import detect_channel, derive_tags
from api_client import APIClient

ROOT = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("annotate")


def fetch_all(api: APIClient) -> list[dict]:
    page, size, items = 1, 200, []
    while True:
        result = api.get_transactions(page=page, size=size)
        items.extend(result["list"])
        if page * size >= result["total"]:
            break
        page += 1
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = parser.parse_args()

    load_dotenv(os.path.join(ROOT, ".env"))
    api = APIClient(os.environ["API_BASE_URL"], os.environ["API_KEY"])
    try:
        logger.info("Fetching transactions...")
        txs = fetch_all(api)
        logger.info("Loaded %d transactions", len(txs))

        items = []
        channels = Counter()
        new_tags = Counter()
        skipped = 0

        for tx in txs:
            merchant = (tx.get("merchantName") or "").strip()
            note = tx.get("note") or ""
            tx_type = tx.get("type")

            channel = detect_channel(merchant, note)
            tags = derive_tags(tx_type, merchant, note)

            existing_tags = set(tx.get("tags") or [])
            add_tags = [tag for tag in tags if tag not in existing_tags]

            existing_ext = tx.get("extFields") or {}
            ext_fields = {}
            if channel and not existing_ext.get("channel"):
                ext_fields["channel"] = channel
                channels[channel] += 1

            for tag in add_tags:
                new_tags[tag] += 1

            if add_tags or ext_fields:
                items.append({
                    "id": tx["id"],
                    "addTags": add_tags,
                    "extFields": ext_fields,
                })
            else:
                skipped += 1

        logger.info("Transactions needing annotation: %d (skipped: %d)", len(items), skipped)
        logger.info("Channel additions: %s", dict(channels))
        logger.info("Tag additions: %s", dict(new_tags))

        if args.dry_run:
            logger.info("[DRY RUN] Nothing written")
            return

        applied = 0
        for i in range(0, len(items), 200):
            batch = items[i:i + 200]
            applied += api.annotate_transactions(batch)
            logger.info("Applied %d/%d", applied, len(items))
        logger.info("Done. Annotated %d transactions.", applied)
    finally:
        api.close()


if __name__ == "__main__":
    main()