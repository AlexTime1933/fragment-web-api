"""Jetton (TEP-74) transfers, used to pay Fragment in USDT on TON.

Fragment's USDT-on-TON payment is an ordinary jetton transfer the wallet sends to
its OWN jetton wallet::

    our wallet --(~0.05 TON for gas, payload = transfer)--> our USDT jetton wallet
        --(USDT)--> Fragment's address, with a text comment in forward_payload

The payload is parsed here so the library can check the real destination and
amount before signing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .address import Address
from .cell import Cell

#: ``transfer`` op of a jetton wallet (TEP-74)
JETTON_TRANSFER_OP = 0x0F8A7EA5

#: Tether USD on TON mainnet (jetton master; tonapi: "Tether USD", verified)
USDT_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
USDT_DECIMALS = 6


def to_units(amount: "Decimal | str | int", decimals: int = USDT_DECIMALS) -> int:
    return int((Decimal(str(amount)) * (Decimal(10) ** decimals)).to_integral_value())


def from_units(amount: int, decimals: int = USDT_DECIMALS) -> Decimal:
    return Decimal(amount) / (Decimal(10) ** decimals)


@dataclass(frozen=True)
class JettonTransfer:
    query_id: int
    amount: int  # in the jetton's smallest units
    destination: Address
    response_destination: Address | None
    has_custom_payload: bool
    forward_ton_amount: int  # nanoton
    forward_payload: Cell | None

    @property
    def comment(self) -> str | None:
        """Text comment in the forward payload (op 0), if any."""
        if self.forward_payload is None:
            return None
        s = self.forward_payload.begin_parse()
        try:
            if s.load_uint(32) != 0:
                return None
            return s.load_snake_string()
        except Exception:  # not a text comment
            return None


def parse_transfer(body: Cell) -> JettonTransfer:
    """Decode a jetton ``transfer`` message body; ``ValueError`` if it is not one."""
    s = body.begin_parse()
    try:
        op = s.load_uint(32)
        if op != JETTON_TRANSFER_OP:
            raise ValueError(f"not a jetton transfer (op {op:#x})")
        query_id = s.load_uint(64)
        amount = s.load_coins()
        destination = s.load_address()
        response = s.load_address()
        has_custom = s.load_bit()
        if has_custom:
            s.load_ref()
        forward_ton = s.load_coins()
        forward_payload = s.load_ref() if s.load_bit() else s.to_cell()
    except ValueError:
        raise
    except Exception as exc:  # truncated / malformed cell
        raise ValueError(f"malformed jetton transfer: {exc}") from None
    if destination is None:
        raise ValueError("jetton transfer has no destination")
    return JettonTransfer(query_id, amount, destination, response, has_custom, forward_ton, forward_payload)
