"""BudgetPilot API client wrapper."""

import time
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class APIError(Exception):
    """API returned a non-zero code."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"API error {code}: {message}")


class APIClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        self.client = httpx.Client(base_url=self.base_url, timeout=15)

    def close(self):
        self.client.close()

    def __del__(self):
        if hasattr(self, "client"):
            self.client.close()

    def _request(self, method: str, path: str, **kwargs) -> Any:
        # Strip /api/v1 prefix if base_url already contains it
        if path.startswith("/api/v1/"):
            path = path[len("/api/v1"):]
        resp = self.client.request(method, path, headers=self.headers, **kwargs)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise APIError(body["code"], body.get("message", "unknown error"))
        return body.get("data")

    # --- Accounts ---

    def get_accounts(self, active: bool = True) -> list[dict]:
        return self._request("GET", "/api/v1/accounts", params={"active": active})

    def get_account(self, account_id: int) -> dict:
        return self._request("GET", f"/api/v1/accounts/{account_id}")

    # --- Categories ---

    def get_categories(self, type_: int | None = None) -> list[dict]:
        params = {"type": type_} if type_ is not None else {}
        return self._request("GET", "/api/v1/categories/tree", params=params)

    # --- Transactions ---

    def create_transaction(self, data: dict) -> dict:
        return self._request("POST", "/api/v1/transactions", json=data)

    def get_transactions(self, **params) -> dict:
        data = self._request("GET", "/api/v1/transactions", params=params)
        return {
            "list": data.get("items", []),
            "total": data.get("total", 0),
            "page": data.get("page", 1),
            "size": data.get("size", 20),
        }

    def delete_transaction(self, tx_id: int) -> None:
        self._request("DELETE", f"/api/v1/transactions/{tx_id}")

    def confirm_transaction(self, tx_id: int) -> None:
        self._request("POST", f"/api/v1/transactions/{tx_id}/confirm")

    def update_transaction(self, tx_id: int, data: dict) -> dict:
        return self._request("PUT", f"/api/v1/transactions/{tx_id}", json=data)

    # --- Transactions with retry for idempotency ---

    def create_transaction_with_retry(self, data: dict, retries: int = 2, delay: int = 3) -> dict:
        """Create transaction with retry on timeout/duplicate (code 10004)."""
        last_exc = None
        for attempt in range(retries + 1):
            try:
                return self.create_transaction(data)
            except httpx.TimeoutException:
                last_exc = RuntimeError("Request timeout")
                if attempt < retries:
                    logger.warning("API timeout, retry %d/%d after %ds", attempt + 1, retries, delay)
                    time.sleep(delay)
            except APIError as e:
                if e.code == 10004:
                    logger.info("Transaction already exists (idempotency hit)")
                    return self._find_existing_transaction(data)
                raise
        raise last_exc

    def _find_existing_transaction(self, data: dict) -> dict:
        params = {
            "accountId": data["accountId"],
            "startDate": data.get("transactionDate"),
            "endDate": data.get("transactionDate"),
            "minAmount": data["amount"],
            "maxAmount": data["amount"],
            "size": 50,
        }
        result = self.get_transactions(**params)
        for tx in result.get("list", []):
            if tx["amount"] == data["amount"] and tx["accountId"] == data["accountId"]:
                return tx
        return {"status": "duplicate_but_not_found"}
