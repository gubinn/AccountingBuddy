"""Reclassify existing "其他支出/其他收入" transactions with the new classifier.

Usage:
  ./venv/bin/python reclassify.py --dry-run          # preview only
  ./venv/bin/python reclassify.py                    # apply
  ./venv/bin/python reclassify.py --no-llm           # rules/learned/keywords only
  ./venv/bin/python reclassify.py --limit 20         # apply at most 20 changes

Only transactions whose current category is 其他支出/其他收入 are touched.
The bulk endpoint updates category_id only (no balance/budget side effects).
"""

import argparse
import logging
import os
import sys
from collections import Counter

import yaml
from dotenv import load_dotenv

from api_client import APIClient
from category_mapper import (CategoryMapper, build_initial_mapping,
                             build_merchant_map)
from llm_classifier import OllamaClassifier

ROOT = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("reclassify")


def load_config() -> dict:
    with open(os.path.join(ROOT, "config.yaml")) as f:
        return yaml.safe_load(f) or {}


def save_config(config: dict) -> None:
    with open(os.path.join(ROOT, "config.yaml"), "w") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False)


def fetch_all_transactions(api: APIClient) -> list[dict]:
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
    parser.add_argument("--no-llm", action="store_true", help="不使用 Ollama 兜底")
    parser.add_argument("--refine-subcategories", action="store_true",
                        help="把一级分类交易细分到同一级下的二级分类")
    parser.add_argument("--limit", type=int, default=0, help="最多应用多少条（0=全部）")
    args = parser.parse_args()

    load_dotenv(os.path.join(ROOT, ".env"))
    config = load_config()
    api = APIClient(os.environ["API_BASE_URL"], os.environ["API_KEY"])

    try:
        changed, category_types, categories = build_initial_mapping(api, config)
        if changed:
            save_config(config)
        mapping = config.get("category_mapping") or {}
        id_to_name = {cid: info["path"] for cid, info in categories.items()}
        for name, cid in mapping.items():
            id_to_name.setdefault(cid, name)
        other_ids = {config.get("fallback_expense_category_id"),
                     config.get("fallback_income_category_id")}
        other_ids |= {cid for cid, info in categories.items()
                      if info["name"] in ("其他支出", "其他收入")}
        other_ids.discard(None)
        if not other_ids:
            logger.error("找不到「其他支出/其他收入」分类，无法确定目标范围")
            return

        merchants = api.get_merchants()
        merchant_map = build_merchant_map(merchants, category_types, excluded=other_ids)
        llm = None if args.no_llm else OllamaClassifier(config, mapping, category_types)
        mapper = CategoryMapper(mapping, category_types,
                                merchant_map=merchant_map, llm=llm,
                                categories=categories)

        def is_child(cid):
            return bool(categories.get(cid, {}).get("parent_id"))

        def top_of(cid):
            info = categories.get(cid)
            seen = set()
            while info and info.get("parent_id") and cid not in seen:
                seen.add(cid)
                cid = info["parent_id"]
                info = categories.get(cid)
            return cid

        parents_with_children = {info["parent_id"] for info in categories.values()
                                 if info.get("parent_id")}
        top_with_children = {cid for cid, info in categories.items()
                             if info.get("parent_id") == 0 and cid in parents_with_children}
        if args.refine_subcategories:
            logger.info("Subcategory refinement enabled: %d top-level categories",
                        len(top_with_children))

        logger.info("Fetching transactions...")
        txs = fetch_all_transactions(api)
        logger.info("Loaded %d transactions", len(txs))

        changes: list[dict] = []
        before = Counter()
        proposed = Counter()
        cross_top = 0
        cross_details = Counter()
        for tx in txs:
            if tx.get("type") not in (1, 2):
                continue
            current = tx.get("categoryId")
            is_other = current in other_ids
            is_refine_candidate = (args.refine_subcategories and not is_other
                                   and current in top_with_children)
            if not is_other and not is_refine_candidate:
                continue
            before[id_to_name.get(current, "?")] += 1
            text = (tx.get("merchantName") or "").strip()
            note = tx.get("note") or ""
            new_id = mapper.classify(text or note, tx["type"], note=note,
                                     allow_llm=not args.no_llm)
            if not new_id or new_id == current:
                continue
            if is_refine_candidate:
                # 只允许落到子分类；规则明确要求的跨大类（如 盒马/山姆 购物→餐饮/食材）放行并计数
                if not is_child(new_id):
                    continue
                if top_of(new_id) != current:
                    cross_top += 1
                    cross_details[(id_to_name.get(current, "?"),
                                   id_to_name.get(new_id, "?"),
                                   text[:40])] += 1
            changes.append({"id": tx["id"], "categoryId": new_id})
            proposed[id_to_name.get(new_id, str(new_id))] += 1

        logger.info("Candidate transactions: %d", sum(before.values()))
        logger.info("Proposed changes: %d (cross-top: %d)", len(changes), cross_top)
        for name, count in proposed.most_common():
            logger.info("  -> %s: %d", name, count)
        if cross_details:
            logger.info("Cross-top details:")
            for (old, new, merchant), count in cross_details.most_common(15):
                logger.info("  %s -> %s: %d  %s", old, new, count, merchant)
        if not changes:
            return

        if args.dry_run:
            for item in changes[:30]:
                logger.info("  [DRY RUN] tx %s -> %s",
                            item["id"], id_to_name.get(item["categoryId"]))
            logger.info("[DRY RUN] 共 %d 条未写入", len(changes))
            return

        if args.limit > 0:
            changes = changes[:args.limit]
        applied = 0
        for i in range(0, len(changes), 200):
            batch = changes[i:i + 200]
            applied += api.reclassify_transactions(batch)
            logger.info("Applied %d/%d", applied, len(changes))
        logger.info("Done. Reclassified %d transactions.", applied)
    finally:
        api.close()


if __name__ == "__main__":
    main()