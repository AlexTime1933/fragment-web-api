"""Small async client for tonapi.io (keys are issued at tonconsole.com)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..exceptions import TonApiError, TransactionFailedError, TransactionTimeoutError
from .address import Address

log = logging.getLogger(__name__)

MAINNET_URL = "https://tonapi.io"
TESTNET_URL = "https://testnet.tonapi.io"


class TonApiClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = MAINNET_URL,
        timeout: float = 20.0,
        max_retries: int = 4,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._own_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._headers = headers
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries

    async def aclose(self) -> None:
        if self._own_client:
            await self._http.aclose()

    async def __aenter__(self) -> "TonApiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ http
    async def _request(self, method: str, path: str, *, allow_404: bool = False, **kwargs: Any) -> Any:
        url = f"{self._base}{path}"
        delay = 1.0
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.request(method, url, headers=self._headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise TonApiError(f"tonapi network error: {exc}") from exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code >= 400:
                break
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise TonApiError("tonapi returned non-JSON response", status=resp.status_code) from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        message = payload.get("error") if isinstance(payload, dict) else str(payload)
        raise TonApiError(f"tonapi {method} {path} failed ({resp.status_code}): {message}",
                          status=resp.status_code, payload=payload)

    # ----------------------------------------------------------- accounts
    async def get_account(self, address: "Address | str") -> dict:
        addr = Address.parse(address).to_raw()
        data = await self._request("GET", f"/v2/accounts/{addr}", allow_404=True)
        return data or {"address": addr, "balance": 0, "status": "nonexist"}

    async def get_balance(self, address: "Address | str") -> int:
        return int((await self.get_account(address)).get("balance", 0))

    async def get_seqno(self, address: "Address | str") -> int:
        account = await self.get_account(address)
        if account.get("status") != "active":
            return 0
        data = await self._request("GET", f"/v2/wallet/{Address.parse(address).to_raw()}/seqno")
        return int(data["seqno"])

    async def get_wallet_info(self, address: "Address | str") -> dict | None:
        return await self._request("GET", f"/v2/wallet/{Address.parse(address).to_raw()}", allow_404=True)

    async def run_get_method(self, address: "Address | str", method: str, *args: str) -> dict:
        params = [("args", a) for a in args]
        return await self._request(
            "GET", f"/v2/blockchain/accounts/{Address.parse(address).to_raw()}/methods/{method}", params=params
        )

    async def get_public_key(self, address: "Address | str") -> bytes | None:
        """Public key stored in a deployed wallet (``get_public_key``)."""
        try:
            res = await self.run_get_method(address, "get_public_key")
        except TonApiError:
            return None
        if not res or not res.get("success") or not res.get("stack"):
            return None
        num = res["stack"][0].get("num")
        if num is None:
            return None
        return int(num, 16 if num.startswith(("0x", "-0x")) else 10).to_bytes(32, "big")

    async def get_jetton_wallet(self, owner: "Address | str", master: "Address | str") -> tuple[Address, int]:
        """The owner's jetton wallet for ``master`` and its balance (smallest units).

        The address comes from the master contract itself (``get_wallet_address``),
        so it does not depend on tonapi's indexer; the balance is 0 if the wallet
        does not exist yet.
        """
        owner_raw = Address.parse(owner).to_raw()
        res = await self.run_get_method(master, "get_wallet_address", owner_raw)
        try:
            wallet = Address.parse(res["decoded"]["jetton_wallet_address"])
        except (KeyError, TypeError, ValueError):
            raise TonApiError("tonapi: cannot resolve jetton wallet address", payload=res) from None
        data = await self._request("GET", f"/v2/accounts/{owner_raw}/jettons/{Address.parse(master).to_raw()}",
                                   allow_404=True)
        balance = int((data or {}).get("balance") or 0)
        if data and data.get("wallet_address", {}).get("address"):
            if Address.parse(data["wallet_address"]["address"]) != wallet:
                raise TonApiError("tonapi: jetton wallet address mismatch", payload=data)
        return wallet, balance

    async def get_ton_rate(self, currency: str = "usd") -> float | None:
        data = await self._request("GET", "/v2/rates", params={"tokens": "ton", "currencies": currency})
        try:
            return float(data["rates"]["TON"]["prices"][currency.upper()])
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------- messages
    async def send_boc(self, boc_b64: str) -> None:
        await self._request("POST", "/v2/blockchain/message", json={"boc": boc_b64})

    async def emulate_wallet_message(self, boc_b64: str) -> dict:
        """Dry-run a signed external message without sending it."""
        return await self._request("POST", "/v2/wallet/emulate", json={"boc": boc_b64})

    async def get_transaction_by_message(self, message_hash_hex: str) -> dict | None:
        return await self._request(
            "GET", f"/v2/blockchain/messages/{message_hash_hex}/transaction", allow_404=True
        )

    async def get_trace(self, trace_id: str) -> dict | None:
        return await self._request("GET", f"/v2/traces/{trace_id}", allow_404=True)

    async def wait_for_transaction(
        self,
        message_hash_hex: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> dict:
        """Wait until the wallet processed our external message.

        Raises :class:`TransactionFailedError` when the wallet transaction was
        aborted or produced no outgoing messages, and
        :class:`TransactionTimeoutError` when nothing appeared in time.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                tx = await self.get_transaction_by_message(message_hash_hex)
            except TonApiError as exc:
                log.debug("waiting for tx %s: %s", message_hash_hex, exc)
                tx = None
            if tx:
                if not tx.get("success", False) or tx.get("aborted", False):
                    raise TransactionFailedError(
                        "wallet transaction failed", message_hash=message_hash_hex, transaction=tx
                    )
                action = tx.get("action_phase") or {}
                if not tx.get("out_msgs") or action.get("skipped_actions") or action.get("success") is False:
                    raise TransactionFailedError(
                        "wallet accepted the message but the transfer was not sent "
                        "(insufficient balance?)",
                        message_hash=message_hash_hex,
                        transaction=tx,
                    )
                return tx
            if time.monotonic() >= deadline:
                raise TransactionTimeoutError(
                    f"transaction for message {message_hash_hex} not found after {timeout:.0f}s",
                    message_hash=message_hash_hex,
                )
            await asyncio.sleep(poll_interval)
