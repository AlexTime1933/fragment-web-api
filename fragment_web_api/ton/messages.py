"""TL-B serialization of the message types a wallet needs."""

from __future__ import annotations

from dataclasses import dataclass, field

from .address import Address
from .cell import Builder, Cell

# Send modes (https://docs.ton.org/develop/smart-contracts/messages#message-modes)
SEND_MODE_PAY_FEES_SEPARATELY = 1
SEND_MODE_IGNORE_ERRORS = 2
SEND_MODE_DEFAULT = SEND_MODE_PAY_FEES_SEPARATELY | SEND_MODE_IGNORE_ERRORS


def comment_body(text: str) -> Cell:
    """Plain text comment: ``op = 0`` followed by snake-encoded UTF-8."""
    return Builder().store_uint(0, 32).store_snake_string(text).end_cell()


def read_comment(body: Cell | None) -> str | None:
    """Return the text of a plain comment body, or ``None`` if it is not one."""
    if body is None:
        return None
    s = body.begin_parse()
    if s.remaining_bits < 32 or s.load_uint(32) != 0:
        return None
    try:
        return s.load_snake_string()
    except (ValueError, UnicodeDecodeError):
        return None


def state_init_cell(code: Cell, data: Cell) -> Cell:
    return (
        Builder()
        .store_bit(0)  # split_depth: nothing
        .store_bit(0)  # special: nothing
        .store_maybe_ref(code)
        .store_maybe_ref(data)
        .store_bit(0)  # library: empty
        .end_cell()
    )


def contract_address(code: Cell, data: Cell, workchain: int = 0) -> Address:
    return Address(workchain, state_init_cell(code, data).hash())


@dataclass(frozen=True)
class InternalMessage:
    """An outgoing internal message (``MessageRelaxed``) sent by a wallet."""

    destination: Address
    amount: int  # nanotons
    body: Cell | None = None
    state_init: Cell | None = None
    bounce: bool = True
    send_mode: int = SEND_MODE_DEFAULT
    extra: dict = field(default_factory=dict, compare=False)

    def to_cell(self) -> Cell:
        b = Builder()
        b.store_bit(0)  # int_msg_info$0
        b.store_bit(1)  # ihr_disabled
        b.store_bit(self.bounce)
        b.store_bit(0)  # bounced
        b.store_address(None)  # src: filled in by the wallet contract
        b.store_address(self.destination)
        b.store_coins(self.amount)
        b.store_bit(0)  # extra currencies: empty
        b.store_coins(0)  # ihr_fee
        b.store_coins(0)  # fwd_fee
        b.store_uint(0, 64)  # created_lt
        b.store_uint(0, 32)  # created_at
        _store_init_and_body(b, self.state_init, self.body or Cell())
        return b.end_cell()


def _store_init_and_body(b: Builder, state_init: Cell | None, body: Cell, *, force_ref: bool = False) -> None:
    """Store ``init`` and ``body`` inline when they fit, else as refs.

    Mirrors ``storeMessage`` of the official ``@ton/core`` so that our cells
    are bit-for-bit identical to what Tonkeeper produces.
    """
    if state_init is not None:
        need_ref = force_ref or b.bits_left - 2 < state_init.bit_length + body.bit_length
        if need_ref:
            b.store_bit(1).store_bit(1).store_ref(state_init)
        else:
            b.store_bit(1).store_bit(0).store_cell(state_init)
    else:
        b.store_bit(0)
    need_ref = (
        force_ref
        or b.bits_left - 1 < body.bit_length
        or (4 - b.refs_left) + len(body.refs) > 4
    )
    if need_ref:
        b.store_bit(1).store_ref(body)
    else:
        b.store_bit(0).store_cell(body)


def external_message(
    destination: Address,
    body: Cell,
    state_init: Cell | None = None,
    *,
    force_ref: bool = False,
) -> Cell:
    b = Builder()
    b.store_uint(0b10, 2)  # ext_in_msg_info$10
    b.store_address(None)  # src: addr_none
    b.store_address(destination)
    b.store_coins(0)  # import_fee
    _store_init_and_body(b, state_init, body, force_ref=force_ref)
    return b.end_cell()


def normalized_external_hash(destination: Address, body: Cell) -> bytes:
    """TEP-467 normalized hash: src=addr_none, import_fee=0, no init, body as ref.

    This is the id under which indexers (tonapi, toncenter) find the message.
    """
    return external_message(destination, body, None, force_ref=True).hash()
