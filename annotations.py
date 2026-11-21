"""Derive payment channel (extFields) and transaction-method tags.

Channel goes to extFields.channel; method tags go to tags. Both are derived
from the merchant name and the original email text, not guessed from category.
"""

CHANNEL_PATTERNS = [
    ("Apple Pay", ["Apple Pay", "apple pay"]),
    ("京东支付", ["京东支付"]),
    ("抖音支付", ["抖音支付"]),
    ("程支付", ["程支付"]),
    ("微信支付", ["财付通", "微信支付"]),
    ("银联", ["银联"]),
    ("支付宝", ["支付宝"]),
]


def detect_channel(merchant: str, note: str = "") -> str | None:
    """Return the payment channel for a transaction, or None if unknown."""
    for text in (merchant or "", note or ""):
        for label, patterns in CHANNEL_PATTERNS:
            if any(pattern in text for pattern in patterns):
                return label
    return None


def derive_tags(tx_type: int, merchant: str, note: str) -> list[str]:
    """Return method tags such as 内部转账 / 信用卡还款 / 自动扣款."""
    text = f"{merchant or ''} {note or ''}"
    tags: list[str] = []

    if tx_type == 3:
        tags.append("内部转账")
        if "信用卡还款" in text:
            tags.append("信用卡还款")
        if "资金归集" in text:
            tags.append("资金归集")

    if "自动转账" in text:
        tags.append("自动转账")

    # 银行代扣类通知；银联扣款是主动支付，不算自动扣款
    if "扣款人民币" in text and "银联扣款" not in text:
        tags.append("自动扣款")

    # 去重并保持顺序
    return list(dict.fromkeys(tags))


def annotations_for(tx) -> tuple[list[str], dict]:
    """Return (tags, extFields) for an email_parser.Transaction."""
    channel = detect_channel(tx.merchant, tx.raw_text)
    ext_fields = {"channel": channel} if channel else {}
    tags = derive_tags(tx.tx_type, tx.merchant, tx.raw_text)
    return tags, ext_fields