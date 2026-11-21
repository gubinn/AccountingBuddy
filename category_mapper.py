"""Category mapping: merchant normalization + alias rules + learned + LLM.

Classification order (first hit wins):
  1. Learned merchant mapping from BudgetPilot's merchant table
     (the user correcting a transaction updates merchant.category_id)
  2. Curated alias rules in merchant_rules.yaml (legal entity -> brand -> category)
  3. Category-name / keyword substring matching (legacy)
  4. Local Ollama fallback with cached, confidence-gated results

Missing category mappings are built from the BudgetPilot category tree on every
run, so newly added categories (通讯、订阅服务、人情往来...) map automatically.
"""

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
DEFAULT_RULES_FILE = ROOT / "merchant_rules.yaml"

DEFAULT_KEYWORDS = {
    "餐饮": ["餐饮", "餐厅", "外卖", "美团", "饿了么", "肯德基", "麦当劳", "星巴克",
             "瑞幸", "咖啡", "奶茶", "火锅", "烧烤", "必胜客", "汉堡", "披萨", "小吃"],
    "交通": ["地铁", "公交", "滴滴", "打车", "高德", "出租车", "停车", "加油", "高铁",
             "火车", "飞机", "携程", "飞猪", "共享单车", "高速"],
    "住房": ["房租", "房贷", "物业", "水费", "电费", "燃气", "供暖", "维修"],
    "购物": ["超市", "淘宝", "京东", "拼多多", "天猫", "唯品会", "购物", "百货", "商城", "便利店"],
    "医疗": ["医院", "药店", "诊所", "体检", "药房", "挂号", "医疗"],
    "教育": ["教育", "课程", "培训", "学费", "图书", "书店", "考试"],
    "娱乐": ["电影", "游戏", "爱奇艺", "优酷", "哔哩哔哩", "B站", "网易云", "QQ音乐",
             "腾讯视频", "演出", "门票"],
    "通讯": ["通讯", "话费", "联通", "移动", "电信", "流量", "宽带", "通信"],
    "订阅服务": ["订阅", "云服务", "阿里云", "火山引擎", "OpenAI", "OpenRouter",
                 "DeepSeek", "会员"],
    "人情往来": ["红包", "礼金", "随礼", "群收款", "人情"],
    "投资": ["利息", "股息", "理财", "基金", "证券", "投资"],
    "公积金": ["公积金", "住房补贴"],
    "退款": ["退货", "退款", "退票", "冲正"],
    "报销": ["报销"],
    "水电": ["水电", "物业", "燃气", "供暖"],
    "工资": ["工资", "薪资", "薪酬", "奖金", "补贴"],
    "转账": ["转账", "转帐", "汇款", "还款"],
}

DEFAULT_PREFIXES = [
    "支付宝-支付宝-消费-", "支付宝-消费-", "支付宝-",
    "财付通-微信支付-", "财付通-", "微信支付-", "银联-",
    "Apple Pay, 线上支付消费，", "Apple Pay",
]

# 公司/法律主体后缀，反复剥离；括号里的分支机构也去掉
LEGAL_SUFFIX_RE = re.compile(
    r"(?:(?:（[^）]*）|\([^)]*\))?"
    r"(?:股份有限公司|有限责任公司|有限公司|公司|个体工商户|分公司))$"
)
TRAILING_NOISE_RE = re.compile(r"(?:快捷支付|扫二维码付款)$")


def normalize_merchant(text: str, prefixes: list[str] | None = None) -> str:
    """Strip payment-channel prefixes, legal suffixes and branch names."""
    s = (text or "").strip()
    prefixes = sorted(prefixes or DEFAULT_PREFIXES, key=len, reverse=True)
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if s.startswith(prefix):
                s = s[len(prefix):]
                changed = True
    s = re.sub(r"\s+", "", s)
    for _ in range(3):
        trimmed = LEGAL_SUFFIX_RE.sub("", s)
        trimmed = TRAILING_NOISE_RE.sub("", trimmed)
        if trimmed == s:
            break
        s = trimmed
    s = re.sub(r"(?:（[^）]*）|\([^)]*\))$", "", s)
    return s.strip(" -，,、")


class MerchantRuleSet:
    """Curated alias rules: normalized merchant substring -> category name."""

    def __init__(self, path: str | Path | None = None):
        data: dict = {}
        path = Path(path or DEFAULT_RULES_FILE)
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        self.prefixes = data.get("normalize", {}).get("prefixes") or DEFAULT_PREFIXES
        self.rules = data.get("rules") or []
        pairs: list[tuple[str, str]] = []
        for rule in self.rules:
            category = rule.get("category")
            if not category:
                continue
            for pattern in rule.get("patterns", []):
                pattern = str(pattern).strip() if pattern is not None else ""
                if pattern:
                    pairs.append((pattern, category))
        self._index = sorted(pairs, key=lambda item: len(item[0]), reverse=True)

    def normalize(self, text: str) -> str:
        return normalize_merchant(text, self.prefixes)

    def match_all(self, text: str) -> list[str]:
        normalized = self.normalize(text)
        result: list[str] = []
        for pattern, category in self._index:
            if pattern in normalized and category not in result:
                result.append(category)
        return result

    def match(self, text: str) -> str | None:
        matches = self.match_all(text)
        return matches[0] if matches else None
        return None


def flatten_category_tree(tree: list[dict]) -> dict[int, dict]:
    """Flatten the category tree into {id: {name, type, parent_id}}."""
    result: dict[int, dict] = {}

    def walk(nodes: list[dict]):
        for node in nodes or []:
            result[node["id"]] = {
                "name": node.get("name", ""),
                "type": node.get("type"),
                "parent_id": node.get("parentId", 0),
            }
            walk(node.get("children") or [])

    walk(tree)
    return result


def _match_group(group: str, keywords: list[str], categories: dict[int, dict]) -> int | None:
    # 1) Exact category name match; prefer top-level, then expense categories
    exact = [cid for cid, info in categories.items() if info["name"] == group]
    if exact:
        exact.sort(key=lambda cid: (categories[cid]["parent_id"] != 0,
                                    categories[cid]["type"] != 1))
        return exact[0]

    # 2) Keyword contained in a category name, longest keyword wins
    best: tuple[tuple, int] | None = None
    for cid, info in categories.items():
        for kw in keywords:
            if kw and kw in info["name"]:
                score = (len(kw), info["parent_id"] == 0, info["type"] == 1)
                if best is None or score > best[0]:
                    best = (score, cid)
    return best[1] if best else None


def build_initial_mapping(api_client: Any, config: dict) -> tuple[bool, dict[int, int], dict[int, dict]]:
    """Fill in missing category mappings.

    Returns (changed, category_types, categories) where category_types maps id
    to 1/2 and categories is the flattened tree.
    """
    categories = flatten_category_tree(api_client.get_categories())
    category_types = {cid: info["type"] for cid, info in categories.items()
                      if info["type"] is not None}

    existing: dict[str, int] = config.get("category_mapping") or {}
    changed = False
    # Drop stale entries for known groups whose category no longer exists
    for group in list(existing):
        if group in DEFAULT_KEYWORDS and existing[group] not in categories:
            logger.warning("Category mapping %s -> %s no longer exists; dropping",
                           group, existing[group])
            del existing[group]
            changed = True

    for group, keywords in DEFAULT_KEYWORDS.items():
        if group in existing and existing[group] in categories:
            continue
        match = _match_group(group, keywords, categories)
        if match is not None and existing.get(group) != match:
            logger.info("Mapped category %s -> %s (id=%s)",
                        group, categories[match]["name"], match)
            existing[group] = match
            changed = True

    config["category_mapping"] = existing
    return changed, category_types, categories


def build_merchant_map(merchants: list[dict], category_types: dict[int, int],
                       excluded: set[int] | None = None) -> dict[str, int]:
    """Build normalized merchant/alias -> category map from BudgetPilot merchants.

    Only non-"其他" categories are trusted as learned knowledge.
    """
    excluded = excluded or set()
    result: dict[str, int] = {}
    for merchant in merchants or []:
        category_id = merchant.get("categoryId")
        if not category_id or category_id in excluded:
            continue
        if category_types.get(category_id) not in (1, 2):
            continue
        name = (merchant.get("name") or "").strip()
        if name:
            result[name] = category_id
            result[normalize_merchant(name)] = category_id
        alias = merchant.get("alias") or ""
        for part in re.split(r"[,，;；/]", alias):
            part = part.strip()
            if part:
                result[part] = category_id
                result[normalize_merchant(part)] = category_id
    return result


class CategoryMapper:
    def __init__(self, mapping: dict[str, int], category_types: dict[int, int] | None = None,
                 merchant_map: dict[str, int] | None = None,
                 rules_path: str | Path | None = None, llm: Any = None):
        self.mapping = mapping or {}
        self.category_types = category_types or {}
        self.merchant_map = merchant_map or {}
        self.rules = MerchantRuleSet(rules_path)
        self.llm = llm

        self.keyword_to_id: dict[str, int] = {}
        for category_name, cid in self.mapping.items():
            for kw in DEFAULT_KEYWORDS.get(category_name, []):
                self.keyword_to_id[kw] = cid

        # Longest names/keywords first, so specific matches win
        self._names = sorted(self.mapping.items(), key=lambda kv: len(kv[0]), reverse=True)
        self._keywords = sorted(self.keyword_to_id.items(), key=lambda kv: len(kv[0]), reverse=True)

    def _type_ok(self, category_id: int, tx_type: int | None) -> bool:
        if tx_type not in (1, 2):
            return True
        category_type = self.category_types.get(category_id)
        return category_type is None or category_type == tx_type

    def classify(self, text: str, tx_type: int | None = None,
                 note: str = "", allow_llm: bool = True) -> int | None:
        raw = (text or "").strip()
        normalized = self.rules.normalize(raw)

        # 1) Learned from BudgetPilot merchant table (user corrections)
        for key in (raw, normalized):
            if not key:
                continue
            cid = self.merchant_map.get(key)
            if cid is not None and self._type_ok(cid, tx_type):
                return cid

        # 2) Curated alias rules (merchant first, then note; first type-compatible wins)
        for candidate in (raw, note):
            if not candidate:
                continue
            for category_name in self.rules.match_all(candidate):
                cid = self.mapping.get(category_name)
                if cid is not None and self._type_ok(cid, tx_type):
                    return cid

        # 3) Legacy category-name / keyword matching
        for variant in {raw, normalized}:
            if not variant:
                continue
            for name, cid in self._names:
                if name and name in variant and self._type_ok(cid, tx_type):
                    return cid
            for kw, cid in self._keywords:
                if kw and kw in variant and self._type_ok(cid, tx_type):
                    return cid

        # 4) Local LLM fallback
        if allow_llm and self.llm is not None and tx_type in (1, 2):
            cid = self.llm.classify(raw, note, tx_type)
            if cid is not None and self._type_ok(cid, tx_type):
                return cid
        return None