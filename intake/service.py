"""Turn an inbound chat message into BudgetPilot transactions.

This module owns the business rules: image OCR, extraction, account/category
resolution, duplicate detection, idempotent creation and the reply text. It has
no transport code, so Telegram (and later WhatsApp) can share it.
"""

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from category_mapper import (CategoryMapper, build_initial_mapping,
                             build_merchant_map)
from email_parser import Transaction
from llm_classifier import OllamaClassifier

from .accounts import AccountResolver
from .extractor import extract
from .models import DraftTransaction
from .ocr import ocr_image

logger = logging.getLogger(__name__)

TYPE_LABELS = {1: "支出", 2: "收入", 4: "退款"}


class IntakeError(Exception):
    pass


@dataclass
class CreatedTransaction:
    draft: DraftTransaction
    tx_id: int | None = None
    account_id: int | None = None
    account_name: str = ""
    category_id: int | None = None
    category_name: str = ""
    confirmed: bool = True
    duplicate_of: int | None = None
    error: str = ""
    external_id: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class ProcessResult:
    text: str = ""
    drafts: list[DraftTransaction] = field(default_factory=list)
    created: list[CreatedTransaction] = field(default_factory=list)
    from_image: bool = False


def serialize_result(result: ProcessResult) -> dict:
    """State-file representation of a preview (no Telegram objects)."""
    return {
        "text": result.text,
        "from_image": result.from_image,
        "items": [{
            "draft": asdict(item.draft),
            "tx_id": item.tx_id,
            "account_id": item.account_id,
            "account_name": item.account_name,
            "category_id": item.category_id,
            "category_name": item.category_name,
            "confirmed": item.confirmed,
            "duplicate_of": item.duplicate_of,
            "error": item.error,
            "external_id": item.external_id,
            "payload": item.payload,
        } for item in result.created],
    }


def deserialize_result(data: dict) -> ProcessResult:
    items: list[CreatedTransaction] = []
    for entry in (data or {}).get("items") or []:
        items.append(CreatedTransaction(
            draft=DraftTransaction(**(entry.get("draft") or {})),
            tx_id=entry.get("tx_id"),
            account_id=entry.get("account_id"),
            account_name=entry.get("account_name") or "",
            category_id=entry.get("category_id"),
            category_name=entry.get("category_name") or "",
            confirmed=bool(entry.get("confirmed", True)),
            duplicate_of=entry.get("duplicate_of"),
            error=entry.get("error") or "",
            external_id=entry.get("external_id") or "",
            payload=entry.get("payload") or {},
        ))
    return ProcessResult(
        text=(data or {}).get("text") or "",
        drafts=[item.draft for item in items],
        created=items,
        from_image=bool((data or {}).get("from_image")),
    )


class IntakeService:
    def __init__(self, api: Any, config: dict):
        self.api = api
        self.config = config or {}
        self.intake_config = self.config.get("intake") or {}

        self.accounts = self.api.get_accounts()
        self.resolver = AccountResolver(self.accounts, self.config)

        changed, category_types, categories = build_initial_mapping(self.api, self.config)
        if changed:
            logger.info("Intake rebuilt category mapping in memory")
        self.categories = categories
        self.category_names = {
            int(cid): str(info.get("name") or f"#{cid}")
            for cid, info in categories.items()
        }
        self.category_types = category_types

        merchants: list[dict] = []
        try:
            merchants = self.api.get_merchants() or []
        except Exception as exc:  # noqa: BLE001 - learned map is optional
            logger.warning("Intake could not load merchants: %s", exc)

        other_ids = {
            self.config.get("fallback_expense_category_id"),
            self.config.get("fallback_income_category_id"),
        }
        other_ids |= {
            cid for cid, info in categories.items()
            if info.get("name") in ("其他支出", "其他收入")
        }
        other_ids.discard(None)
        merchant_map = build_merchant_map(merchants, category_types, excluded=other_ids)
        llm = OllamaClassifier(
            self.config,
            self.config.get("category_mapping") or {},
            category_types,
        )
        self.mapper = CategoryMapper(
            self.config.get("category_mapping") or {},
            category_types,
            merchant_map=merchant_map,
            llm=llm,
            categories=categories,
        )

    # ---------- public ----------

    def prepare(self, text: str, image_path: str | None = None,
                channel: str = "telegram", chat_id: str = "",
                message_id: str = "") -> ProcessResult:
        """Parse a message into a preview. Does not write to BudgetPilot."""
        parts = [part.strip() for part in (text or "").splitlines() if part.strip()]
        if image_path:
            try:
                ocr_text = ocr_image(image_path)
            except Exception as exc:  # noqa: BLE001
                raise IntakeError(f"图片识别失败：{exc}") from exc
            if ocr_text:
                parts.append(ocr_text)
        combined = "\n".join(parts).strip()
        if not combined:
            return ProcessResult(text="", from_image=bool(image_path))

        drafts = extract(combined, self.config)
        result = ProcessResult(text=combined, drafts=drafts, from_image=bool(image_path))
        if not drafts:
            return result

        prefix = f"chat:{channel}:{chat_id}:{message_id}"
        caption = (text or "").strip()
        for index, draft in enumerate(drafts):
            external_id = prefix if len(drafts) == 1 else f"{prefix}:{index}"
            try:
                created = self._prepare_one(
                    draft, combined, external_id, channel, message_id, caption)
            except Exception as exc:  # noqa: BLE001 - report per transaction
                logger.error("Intake prepare failed for %s: %s", external_id, exc)
                created = CreatedTransaction(
                    draft=draft, external_id=external_id, error=str(exc))
            result.created.append(created)
        return result

    def commit(self, result: ProcessResult, confirmed: bool = True) -> ProcessResult:
        """Write a prepared preview to BudgetPilot."""
        for item in result.created:
            if item.tx_id is not None or item.error or not item.payload:
                continue
            item.payload["isConfirmed"] = bool(confirmed)
            try:
                response = self.api.create_transaction_with_retry(item.payload)
                item.tx_id = response.get("id") if isinstance(response, dict) else None
                item.confirmed = bool(confirmed)
                item.error = ""
                logger.info("Intake created tx %s (%s %.2f %s)", item.tx_id,
                            item.draft.tx_type, item.draft.amount, item.draft.merchant)
            except Exception as exc:  # noqa: BLE001 - keep the preview for retry
                item.error = str(exc)
                logger.error("Intake commit failed for %s: %s", item.external_id, exc)
        return result

    def process(self, text: str, image_path: str | None = None,
                channel: str = "telegram", chat_id: str = "",
                message_id: str = "") -> ProcessResult:
        """Prepare and confirm in one step (used by tests and scripts)."""
        return self.commit(
            self.prepare(text, image_path, channel, chat_id, message_id),
            confirmed=True,
        )

    def undo(self, tx_ids: list[int]) -> tuple[list[int], list[int]]:
        deleted: list[int] = []
        failed: list[int] = []
        for tx_id in tx_ids:
            try:
                self.api.delete_transaction(tx_id)
                deleted.append(tx_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Intake undo failed for transaction %s: %s", tx_id, exc)
                failed.append(tx_id)
        return deleted, failed

    # ---------- creation ----------

    def _prepare_one(self, draft: DraftTransaction, raw_text: str,
                     external_id: str, channel: str, message_id: str,
                     caption: str = "") -> CreatedTransaction:
        draft.merchant = self._caption_merchant(draft.merchant, caption)
        account_id = self.resolver.resolve(draft.account_hint, raw_text)
        if account_id is None:
            raise IntakeError("没有可用账户，请在 config.yaml 配置 intake.default_account_id")

        generic = draft.merchant in ("聊天记账", "测试", "其他", "未知")
        category_id = None if generic else self.mapper.classify(
            draft.merchant or "", draft.tx_type,
            note=draft.note or raw_text[:200],
        )
        if category_id is None:
            fallback_key = ("fallback_income_category_id" if draft.tx_type == 2
                            else "fallback_expense_category_id")
            category_id = self.config.get(fallback_key)
        if category_id is None:
            raise IntakeError("没有匹配到分类，请配置 fallback_expense_category_id")

        duplicate_of = self._find_duplicate(draft, account_id)

        note = draft.note or raw_text
        if draft.tx_type == 2 and draft.merchant:
            note = f"{draft.merchant} {note}".strip()
        tx = Transaction(
            account_suffix="",
            date=draft.date,
            time=draft.time,
            merchant=draft.merchant or "聊天记账",
            amount=draft.amount,
            balance=None,
            raw_text=note,
            tx_type=draft.tx_type,
        )
        payload = tx.to_api_payload(
            account_id,
            category_id,
            confirmed=True,
            external_id=external_id,
            tags=["聊天记账"],
            ext_fields={
                "source": f"chat:{channel}",
                "chat_confidence": draft.confidence,
            },
        )
        payload["currency"] = draft.currency
        payload["metadata"] = {
            "source": "chat",
            "channel": channel,
            "message_id": str(message_id),
        }

        return CreatedTransaction(
            draft=draft,
            account_id=account_id,
            account_name=self.resolver.name_of(account_id),
            category_id=category_id,
            category_name=self.category_names.get(int(category_id), f"#{category_id}"),
            confirmed=True,
            duplicate_of=duplicate_of,
            external_id=external_id,
            payload=payload,
        )

    _GENERIC_MERCHANTS = {"", "聊天记账", "测试", "其他", "未知", "转账", "入账"}
    _CAPTION_VERBS = (
        "花了", "买了", "支付", "消费", "付款", "今天", "昨天", "前天",
        "中午", "上午", "下午", "晚上", "转给", "收到",
    )

    @staticmethod
    def _caption_merchant(merchant: str, caption: str) -> str:
        """Use a short caption such as "房租收入" when the model returned nothing."""
        if merchant and merchant not in IntakeService._GENERIC_MERCHANTS:
            return merchant
        candidate = (caption or "").strip()
        if not candidate:
            return merchant or "聊天记账"
        candidate = re.sub(r"[¥￥]?\d+(?:\.\d{1,2})?\s*(?:元|块)?", "", candidate)
        candidate = re.sub(r"\s+", "", candidate).strip("，,。.;；:：-—")
        if (not candidate or len(candidate) > 12
                or any(word in candidate for word in IntakeService._CAPTION_VERBS)):
            return merchant or "聊天记账"
        # "房租收入" reads better as merchant "房租"; the category stays 房租收入.
        return re.sub(r"(收入|支出)$", "", candidate) or candidate

    def _find_duplicate(self, draft: DraftTransaction, account_id: int) -> int | None:
        if self.intake_config.get("duplicate_check", True) is False:
            return None
        try:
            lookback = int(self.intake_config.get("duplicate_lookback_days", 1))
        except (TypeError, ValueError):
            lookback = 1
        try:
            day = date.fromisoformat(draft.date)
        except ValueError:
            return None
        try:
            data = self.api.get_transactions(
                startDate=(day - timedelta(days=lookback)).isoformat(),
                endDate=(day + timedelta(days=lookback)).isoformat(),
                size=50,
            )
        except Exception as exc:  # noqa: BLE001 - duplicate check is best effort
            logger.warning("Duplicate lookup failed: %s", exc)
            return None

        for item in (data or {}).get("list") or []:
            if item.get("type") != draft.tx_type:
                continue
            if str(item.get("accountId")) != str(account_id):
                continue
            try:
                amount = float(item.get("amount"))
            except (TypeError, ValueError):
                continue
            if abs(amount - draft.amount) <= 0.01 and item.get("id"):
                return int(item["id"])
        return None

    # ---------- replies ----------

    @staticmethod
    def _no_transaction_text() -> str:
        return ("❌ 没识别出交易。\n\n"
                "可以这样发：\n"
                "• 午饭 35 微信\n"
                "• 支出 35 打车\n"
                "• 或直接发支付成功截图")

    def _item_lines(self, item: CreatedTransaction) -> list[str]:
        draft = item.draft
        lines = [f"{TYPE_LABELS.get(draft.tx_type, '交易')}  "
                 f"{draft.currency} {draft.amount:.2f}"]
        if draft.tx_type in (1, 4) and draft.merchant:
            lines.append(f"商户：{draft.merchant}")
        elif draft.merchant:
            lines.append(f"来源：{draft.merchant}")
        lines.append(f"分类：{item.category_name}")
        lines.append(f"账户：{item.account_name}")
        when = draft.date + (f" {draft.time[:5]}" if draft.time else "")
        lines.append(f"日期：{when}")
        return lines

    def format_preview(self, result: ProcessResult) -> str:
        if not result.drafts:
            return self._no_transaction_text()

        ok = [c for c in result.created if not c.error]
        failed = [c for c in result.created if c.error]
        lines = ["待确认 · 聊天记账", ""]
        if len(ok) > 1:
            lines.append(f"共 {len(ok)} 笔，将一次性处理")
            lines.append("")

        for index, item in enumerate(ok, 1):
            prefix = f"{index}) " if len(ok) > 1 else ""
            item_lines = self._item_lines(item)
            lines.append(prefix + item_lines[0])
            lines.extend(item_lines[1:])
            if item.duplicate_of:
                lines.append(f"⚠️ 已有相似交易 #{item.duplicate_of}（同账户、同金额）")
            if item.draft.confidence < 0.8:
                lines.append(f"识别把握不高（{item.draft.confidence:.0%}）")
            lines.append("")

        if result.from_image and ok:
            excerpt = " ".join(result.text.split())[:200]
            if excerpt:
                lines.append("识别到的文字：")
                lines.append(excerpt)
                lines.append("")

        for item in failed:
            lines.append(f"❌ {TYPE_LABELS.get(item.draft.tx_type, '交易')} "
                         f"{item.draft.currency} {item.draft.amount:.2f}：{item.error}")
            lines.append("")

        if ok:
            ttl = self.intake_config.get("confirm_ttl_minutes", 30)
            lines.append("确认后才会写入账本。")
            lines.append(f"预览 {ttl} 分钟内有效。")
        else:
            lines.append("以上内容无法生成可入账的预览，请修改后重新发送。")
        return "\n".join(lines).strip()

    def format_result(self, result: ProcessResult) -> str:
        if not result.drafts:
            return self._no_transaction_text()

        lines: list[str] = []
        ok = [c for c in result.created if not c.error]
        failed = [c for c in result.created if c.error]
        pending = [c for c in ok if not c.confirmed]

        if not ok:
            lines.append("❌ 入账失败")
        elif not pending:
            lines.append("✅ 已入账")
        elif len(pending) == len(ok):
            lines.append("🕓 已记为待确认")
        else:
            lines.append("✅ 部分已入账，其余待确认")
        if failed and ok:
            lines.append("❌ 部分交易失败")

        for item in ok:
            lines.append("")
            lines.extend(self._item_lines(item))
            lines.append(f"交易：#{item.tx_id}")
            if not item.confirmed:
                lines.append("状态：待确认（不影响余额）")
            if item.duplicate_of:
                lines.append(f"⚠️ 与已有交易 #{item.duplicate_of} 金额相同")

        for item in failed:
            lines.append("")
            lines.append(f"❌ {TYPE_LABELS.get(item.draft.tx_type, '交易')} "
                         f"{item.draft.currency} {item.draft.amount:.2f}：{item.error}")

        if any(item.tx_id for item in ok):
            lines.append("")
            lines.append("回复 /undo 可撤销本次记账")
        return "\n".join(lines).strip()

    @staticmethod
    def help_text() -> str:
        return (
            "记账助手\n\n"
            "支持三种用法：\n"
            "1. 简写：午饭 35 微信\n"
            "2. 自然语言：今天在星巴克花了 32 块\n"
            "3. 支付成功截图 / 账单截图（可带说明文字）\n\n"
            "我会先发一条预览，确认后才写入账本。\n"
            "预览按钮：确认入账 / 标为待确认 / 取消 / 修改内容。\n"
            "文字命令：/confirm 确认，/cancel 取消，/edit 35 星巴克 修改。\n"
            "/undo 撤销上一条已入账的消息。\n"
            "所有识别都在本机完成。"
        )