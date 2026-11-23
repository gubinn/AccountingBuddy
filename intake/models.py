"""Data structures shared by the chat intake pipeline."""

from dataclasses import dataclass


@dataclass
class DraftTransaction:
    """A transaction extracted from a chat message before it is posted."""

    tx_type: int = 1  # 1=expense, 2=income, 4=refund
    amount: float = 0.0
    currency: str = "CNY"
    date: str = ""
    time: str = ""
    merchant: str = ""
    note: str = ""
    account_hint: str = ""
    confidence: float = 0.0


@dataclass
class InboundMessage:
    """Normalised inbound chat message, independent of the transport."""

    channel: str
    chat_id: str
    message_id: str
    text: str = ""
    image_path: str | None = None
    caption: str = ""
    sent_at: int = 0