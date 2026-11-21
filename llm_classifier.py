"""Local Ollama fallback classifier with disk cache and a review queue.

Only called after the learned merchant map, alias rules and keyword matching all
failed. Results are cached by (model, tx_type, normalized merchant) so the same
merchant is never sent to the model twice. Low-confidence answers are written to
llm_review.csv instead of being applied.
"""

import csv
import json
import logging
import re
import time
from pathlib import Path

import httpx

from category_mapper import normalize_merchant

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
CACHE_FILE = ROOT / "llm_cache.json"
REVIEW_FILE = ROOT / "llm_review.csv"

EXPENSE_EXAMPLES = """\
示例：
支付宝-北京三快在线科技有限公司 -> 餐饮
支付宝-上海拉扎斯信息科技有限公司 -> 餐饮
支付宝-中国铁路网络有限公司 -> 交通
支付宝-贝壳租房 -> 住房
支付宝-上海盒马网络科技有限公司 -> 购物
支付宝-阿里云计算有限公司 -> 订阅服务
支付宝-中国移动 -> 通讯
财付通-微信支付-微信红包 -> 人情往来
"""

# 这些商户/通知没有足够的分类信息，不交给 LLM 猜
SKIP_PATTERNS = (
    "特约商户", "扫二维码付款", "二维码付款", "AlipayPaymentServices",
    "收到本行转入", "他行实时转入", "银联入账", "入账款项",
    "云闪付转账", "手机转账",
)

INCOME_EXAMPLES = """\
示例：
您账户0611入账工资 -> 工资
支付宝-某某报销 -> 报销
大额存单利息 -> 投资
公积金入账 -> 公积金
"""


class OllamaClassifier:
    def __init__(self, config: dict, mapping: dict[str, int], category_types: dict[int, int]):
        self.enabled = bool(config.get("llm_enabled", False))
        self.url = str(config.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/")
        self.model = str(config.get("ollama_model", "qwen-3b-edge:latest"))
        self.min_confidence = float(config.get("llm_min_confidence", 0.8))
        self.timeout = float(config.get("llm_timeout", 60))
        self.disable_thinking = bool(config.get("ollama_disable_thinking", False))
        self.mapping = mapping or {}
        self.category_types = category_types or {}
        self.cache = self._load_json(CACHE_FILE, {})
        self.reviewed = self._load_review_keys()

    # ---------- persistence ----------

    @staticmethod
    def _load_json(path: Path, default):
        try:
            if path.exists():
                with open(path) as f:
                    return json.load(f)
        except Exception as e:
            logger.warning("Failed to load %s: %s", path, e)
        return default

    def _load_review_keys(self) -> set[str]:
        keys: set[str] = set()
        try:
            if REVIEW_FILE.exists():
                with open(REVIEW_FILE, newline="") as f:
                    for row in csv.DictReader(f):
                        if row.get("key"):
                            keys.add(row["key"])
        except Exception:
            pass
        return keys

    def _save_cache(self):
        tmp = CACHE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)
        tmp.replace(CACHE_FILE)

    def _append_review(self, key: str, text: str, note: str, suggestion: str, confidence: float):
        if key in self.reviewed:
            return
        self.reviewed.add(key)
        new_file = not REVIEW_FILE.exists()
        with open(REVIEW_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["key", "merchant", "note", "suggestion", "confidence", "ts"])
            writer.writerow([key, text[:100], note[:100], suggestion, confidence, int(time.time())])

    # ---------- model call ----------

    def _allowed_categories(self, tx_type: int) -> list[str]:
        return [
            name for name, cid in self.mapping.items()
            if self.category_types.get(cid) in (tx_type, None)
        ]

    def _build_prompt(self, text: str, note: str, tx_type: int) -> str:
        allowed = self._allowed_categories(tx_type)
        examples = EXPENSE_EXAMPLES if tx_type == 1 else INCOME_EXAMPLES
        return f"""你是记账分类助手。只能从下面的分类中选择一个：
{", ".join(allowed)}

{examples}
待分类：
商户：{text[:120]}
备注：{note[:120]}

只输出 JSON，不要解释：{{"category":"分类名","confidence":0到1之间的小数}}"""

    def _call(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_predict": 128},
        }
        if self.disable_thinking:
            payload["think"] = False
        started = time.time()
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.url}/api/chat", json=payload)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
        logger.info("LLM classified in %.1fs: %s", time.time() - started, content[:80])
        return content

    def _parse(self, content: str) -> tuple[str, float]:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                return "", 0.0
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return "", 0.0
        category = str(data.get("category", "")).strip()
        try:
            confidence = float(data.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        return category, max(0.0, min(1.0, confidence))

    # ---------- public ----------

    def classify(self, text: str, note: str, tx_type: int) -> int | None:
        if not self.enabled or tx_type not in (1, 2):
            return None
        normalized = normalize_merchant(text)
        combined = normalize_merchant(f"{text} {note}")
        if any(pattern in combined for pattern in SKIP_PATTERNS):
            return None
        if not normalized and not note:
            return None
        key = f"{self.model}|{tx_type}|{normalized or note[:80]}"

        cached = self.cache.get(key)
        if cached:
            cid = self.mapping.get(cached.get("category", ""))
            if cid is not None and self.category_types.get(cid) in (tx_type, None):
                return cid

        allowed = set(self._allowed_categories(tx_type))
        if not allowed:
            return None
        try:
            content = self._call(self._build_prompt(text, note, tx_type))
        except Exception as e:
            logger.warning("Ollama fallback failed for '%s': %s", normalized, e)
            return None

        category, confidence = self._parse(content)
        cid = self.mapping.get(category)
        if cid is None or category not in allowed or confidence < self.min_confidence:
            logger.warning("LLM suggestion rejected for '%s': %s (%.2f)",
                           normalized, category or "?", confidence)
            self._append_review(key, text, note, category or "?", confidence)
            return None

        self.cache[key] = {
            "category": category,
            "confidence": confidence,
            "model": self.model,
            "ts": int(time.time()),
        }
        self._save_cache()
        return cid