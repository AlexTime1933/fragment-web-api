"""Data classes returned by the Fragment client."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..ton.address import Address
from ..ton.cell import Cell
from ..ton.messages import read_comment

NANO = Decimal(10) ** 9


def to_nano(amount: "Decimal | str | int | float") -> int:
    return int((Decimal(str(amount)) * NANO).to_integral_value())


def from_nano(amount: int) -> Decimal:
    return Decimal(amount) / NANO


@dataclass(frozen=True)
class StarsPrice:
    quantity: int
    price_ton: Decimal
    price_usd: Decimal | None = None


@dataclass(frozen=True)
class PremiumPrice:
    months: int
    price_ton: Decimal
    price_usd: Decimal | None = None
    discount: str | None = None  # e.g. "-52%"


@dataclass(frozen=True)
class Recipient:
    """A Telegram user resolved by Fragment.

    ``recipient`` is Fragment's opaque id that must be passed to the purchase
    calls; it is bound to the current session.
    """

    recipient: str
    name: str
    username: str | None = None
    myself: bool = False
    photo_html: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PurchaseRequest:
    """Result of ``initBuyStarsRequest`` / ``initGiftPremiumRequest``."""

    req_id: str
    amount_ton: Decimal
    kind: str  # "stars" | "premium"
    quantity: int  # stars count or months
    recipient: str
    myself: bool = False
    to_bot: bool = False
    item_title: str | None = None
    raw: dict = field(default_factory=dict, repr=False)
    #: "ton" or "usdt_ton" (USDT jetton on TON)
    payment_method: str = "ton"
    #: price in USD from Fragment's price list; the USDT amount is checked against it
    amount_usd: Decimal | None = None


@dataclass(frozen=True)
class TonConnectMessage:
    address: Address
    amount: int  # nanotons
    payload: Cell | None = None
    state_init: Cell | None = None
    raw_address: str = ""

    @property
    def comment(self) -> str | None:
        return read_comment(self.payload)


@dataclass(frozen=True)
class TonConnectTransaction:
    """The ``transaction`` object Fragment hands to a TON Connect wallet."""

    valid_until: int | None
    messages: tuple[TonConnectMessage, ...]
    confirm_method: str | None = None
    confirm_params: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def total_amount(self) -> int:
        return sum(m.amount for m in self.messages)

    @classmethod
    def from_response(cls, result: dict) -> "TonConnectTransaction":
        tx = result.get("transaction")
        if not isinstance(tx, dict) or not isinstance(tx.get("messages"), list) or not tx["messages"]:
            raise ValueError("response has no TON Connect transaction")
        messages = []
        for m in tx["messages"]:
            payload = m.get("payload")
            state_init = m.get("stateInit") or m.get("state_init")
            messages.append(
                TonConnectMessage(
                    address=Address.parse(m["address"]),
                    amount=int(m["amount"]),
                    payload=Cell.from_boc(payload) if payload else None,
                    state_init=Cell.from_boc(state_init) if state_init else None,
                    raw_address=m["address"],
                )
            )
        valid_until = tx.get("validUntil") or tx.get("valid_until")
        if valid_until is not None:
            valid_until = int(valid_until)
            if valid_until > 10**11:  # milliseconds
                valid_until //= 1000
        return cls(
            valid_until=valid_until,
            messages=tuple(messages),
            confirm_method=result.get("confirm_method"),
            confirm_params=dict(result.get("confirm_params") or {}),
            raw=result,
        )


@dataclass(frozen=True)
class WalletSession:
    """``Wallet.init`` options embedded in Fragment pages."""

    address: str | None
    logged_in: bool
    ton_proof: str | None
    version: int


@dataclass(frozen=True)
class PurchaseResult:
    request: PurchaseRequest
    transaction: TonConnectTransaction
    message_hash: str  # normalized external message hash (hex)
    boc: str  # signed external message (base64)
    wallet_address: str
    sent: bool
    confirmed: bool = False
    chain_transaction: dict | None = field(default=None, repr=False)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)
