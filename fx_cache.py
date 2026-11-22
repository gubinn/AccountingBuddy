"""HKD cash-withdrawal → FX buy cost pairing.

The CMB withdrawal email only contains the HKD amount. The matching RMB cost
is in the earlier 自助购汇 email. We remember FX buys (HKD amount → CNY cost)
in a small local cache and consume the most recent unused matching buy when a
withdrawal email arrives.
"""

import json
import os

CACHE_FILE = os.path.join(os.path.dirname(__file__), "fx_cost_cache.json")


def _load() -> list[dict]:
    if not os.path.exists(CACHE_FILE):
        return []
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("entries", [])
    return data if isinstance(data, list) else []


def _save(entries: list[dict]) -> None:
    tmp = f"{CACHE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CACHE_FILE)


def record_buy(hkd: float, cny: float, tx_date: str, external_id: str) -> None:
    """Remember an FX buy; deduplicate by externalId."""
    if hkd is None or cny is None or not external_id:
        return
    entries = _load()
    if any(e.get("external_id") == external_id for e in entries):
        return
    entries.append({
        "hkd": round(float(hkd), 2),
        "cny": round(float(cny), 2),
        "date": tx_date,
        "external_id": external_id,
        "used": False,
    })
    _save(entries)


def find_cost(hkd: float) -> dict | None:
    """Return the most recent unused FX buy matching the HKD amount."""
    if hkd is None:
        return None
    target = round(float(hkd), 2)
    candidates = [
        (idx, entry) for idx, entry in enumerate(_load())
        if not entry.get("used") and round(float(entry.get("hkd", 0)), 2) == target
    ]
    if not candidates:
        return None
    _, entry = max(candidates, key=lambda pair: (pair[1].get("date", ""), pair[0]))
    return entry


def mark_used(entry: dict | None) -> None:
    """Consume an FX buy after the withdrawal was recorded successfully."""
    if not entry:
        return
    external_id = entry.get("external_id")
    entries = _load()
    for item in entries:
        if item.get("external_id") == external_id and not item.get("used"):
            item["used"] = True
            _save(entries)
            return


def take_cost(hkd: float, consume: bool = True) -> float | None:
    """Return the CNY cost of the most recent unused same-HKD FX buy.

    With consume=False (dry-run) the entry is left untouched.
    """
    entry = find_cost(hkd)
    if entry is None:
        return None
    if consume:
        mark_used(entry)
    return float(entry["cny"])