"""Ordinary TON cells: :class:`Cell`, :class:`Builder` and :class:`Slice`.

Only ordinary (non-exotic, level 0) cells are supported. That covers every
cell a wallet needs to build or to read from a TON Connect payload.
Bits are kept as a Python ``int`` plus a bit length, most significant bit first.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

from .address import Address

MAX_BITS = 1023
MAX_REFS = 4


class CellError(ValueError):
    pass


class Cell:
    __slots__ = ("_bits", "_len", "refs", "_hash", "_depth")

    def __init__(self, bits: int = 0, bit_len: int = 0, refs: Iterable["Cell"] = ()) -> None:
        refs = tuple(refs)
        if bit_len < 0 or bit_len > MAX_BITS:
            raise CellError(f"cell bit length {bit_len} out of range")
        if len(refs) > MAX_REFS:
            raise CellError("cell can have at most 4 references")
        if bits < 0 or bits >> bit_len:
            raise CellError("bits do not fit into bit length")
        for r in refs:
            if not isinstance(r, Cell):
                raise CellError("references must be cells")
        self._bits = bits
        self._len = bit_len
        self.refs: tuple[Cell, ...] = refs
        self._hash: bytes | None = None
        self._depth: int | None = None

    # ---------------------------------------------------------------- basics
    @property
    def bit_length(self) -> int:
        return self._len

    @property
    def bits(self) -> int:
        return self._bits

    def data_bytes(self) -> bytes:
        """Cell data padded with the completion tag (``1`` then zeros)."""
        n = self._len
        if n % 8 == 0:
            return self._bits.to_bytes(n // 8, "big")
        pad = 8 - n % 8
        value = (self._bits << pad) | (1 << (pad - 1))
        return value.to_bytes((n + 7) // 8, "big")

    def descriptors(self) -> bytes:
        d1 = len(self.refs)  # ordinary cell, level 0
        d2 = (self._len // 8) + ((self._len + 7) // 8)
        return bytes([d1, d2])

    @property
    def depth(self) -> int:
        if self._depth is None:
            self._depth = 0 if not self.refs else 1 + max(r.depth for r in self.refs)
        return self._depth

    def hash(self) -> bytes:
        if self._hash is None:
            parts = [self.descriptors(), self.data_bytes()]
            parts.extend(r.depth.to_bytes(2, "big") for r in self.refs)
            parts.extend(r.hash() for r in self.refs)
            self._hash = hashlib.sha256(b"".join(parts)).digest()
        return self._hash

    def begin_parse(self) -> "Slice":
        return Slice(self)

    # ----------------------------------------------------------- conversions
    def to_boc(self, *, with_crc: bool = True) -> bytes:
        from .boc import serialize_boc

        return serialize_boc(self, with_crc=with_crc)

    def to_boc_b64(self, *, with_crc: bool = True) -> str:
        import base64

        return base64.b64encode(self.to_boc(with_crc=with_crc)).decode()

    @staticmethod
    def from_boc(data: "bytes | str") -> "Cell":
        from .boc import deserialize_boc

        roots = deserialize_boc(data)
        if len(roots) != 1:
            raise CellError(f"expected exactly one root cell, got {len(roots)}")
        return roots[0]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Cell):
            return NotImplemented
        return self.hash() == other.hash()

    def __hash__(self) -> int:
        return hash(self.hash())

    def __repr__(self) -> str:
        bits = format(self._bits, f"0{self._len}b") if self._len else ""
        return f"Cell(bits={self._len}, refs={len(self.refs)}, data={bits[:64]}{'...' if self._len > 64 else ''})"


def begin_cell() -> "Builder":
    return Builder()


class Builder:
    __slots__ = ("_bits", "_len", "_refs")

    def __init__(self) -> None:
        self._bits = 0
        self._len = 0
        self._refs: list[Cell] = []

    @property
    def bit_length(self) -> int:
        return self._len

    @property
    def bits_left(self) -> int:
        return MAX_BITS - self._len

    @property
    def refs_left(self) -> int:
        return MAX_REFS - len(self._refs)

    def _append(self, value: int, n: int) -> "Builder":
        if n < 0:
            raise CellError("negative bit count")
        if self._len + n > MAX_BITS:
            raise CellError("cell overflow: more than 1023 bits")
        self._bits = (self._bits << n) | value
        self._len += n
        return self

    def store_bit(self, bit: "bool | int") -> "Builder":
        return self._append(1 if bit else 0, 1)

    def store_uint(self, value: int, n: int) -> "Builder":
        if value < 0 or (n == 0 and value != 0) or value >> n:
            raise CellError(f"value {value} does not fit into uint{n}")
        return self._append(value, n)

    def store_int(self, value: int, n: int) -> "Builder":
        if n == 0:
            if value != 0:
                raise CellError("value does not fit into int0")
            return self
        if not -(1 << (n - 1)) <= value < (1 << (n - 1)):
            raise CellError(f"value {value} does not fit into int{n}")
        return self._append(value & ((1 << n) - 1), n)

    def store_bytes(self, data: bytes) -> "Builder":
        if not data:
            return self
        return self._append(int.from_bytes(data, "big"), len(data) * 8)

    def store_bits(self, value: int, n: int) -> "Builder":
        """Store raw ``n`` bits given as an unsigned integer."""
        return self.store_uint(value, n)

    def store_coins(self, amount: int) -> "Builder":
        """Store a ``Grams`` / ``VarUInteger 16`` value (nanotons)."""
        if amount < 0:
            raise CellError("coins amount must be non-negative")
        size = (amount.bit_length() + 7) // 8
        if size > 15:
            raise CellError("coins amount too large")
        self.store_uint(size, 4)
        if size:
            self.store_uint(amount, size * 8)
        return self

    def store_address(self, address: "Address | str | None") -> "Builder":
        if address is None:
            return self.store_uint(0, 2)  # addr_none$00
        address = Address.parse(address)
        self.store_uint(0b10, 2)  # addr_std$10
        self.store_bit(0)  # anycast: nothing
        self.store_int(address.workchain, 8)
        return self.store_bytes(address.hash_part)

    def store_ref(self, cell: Cell) -> "Builder":
        if not isinstance(cell, Cell):
            raise CellError("store_ref expects a Cell")
        if len(self._refs) >= MAX_REFS:
            raise CellError("cell overflow: more than 4 references")
        self._refs.append(cell)
        return self

    def store_maybe_ref(self, cell: Cell | None) -> "Builder":
        if cell is None:
            return self.store_bit(0)
        self.store_bit(1)
        return self.store_ref(cell)

    def store_slice(self, s: "Slice") -> "Builder":
        n = s.remaining_bits
        if n:
            self._append(s.preload_uint(n), n)
        for r in s.remaining_ref_cells():
            self.store_ref(r)
        return self

    def store_cell(self, cell: Cell) -> "Builder":
        """Append the full contents (bits and refs) of ``cell``."""
        return self.store_slice(cell.begin_parse())

    def store_builder(self, other: "Builder") -> "Builder":
        self._append(other._bits, other._len)
        for r in other._refs:
            self.store_ref(r)
        return self

    def store_snake_string(self, text: str) -> "Builder":
        """Store UTF-8 text in this builder and continue in a ref chain."""
        return self.store_snake_bytes(text.encode("utf-8"))

    def store_snake_bytes(self, data: bytes) -> "Builder":
        room = self.bits_left // 8
        head, rest = data[:room], data[room:]
        self.store_bytes(head)
        if rest:
            self.store_ref(Builder().store_snake_bytes(rest).end_cell())
        return self

    def end_cell(self) -> Cell:
        return Cell(self._bits, self._len, self._refs)


class Slice:
    __slots__ = ("_cell", "_pos", "_ref_pos")

    def __init__(self, cell: Cell) -> None:
        self._cell = cell
        self._pos = 0
        self._ref_pos = 0

    @property
    def remaining_bits(self) -> int:
        return self._cell.bit_length - self._pos

    @property
    def remaining_refs(self) -> int:
        return len(self._cell.refs) - self._ref_pos

    def remaining_ref_cells(self) -> tuple[Cell, ...]:
        return self._cell.refs[self._ref_pos:]

    def preload_uint(self, n: int) -> int:
        if n < 0 or n > self.remaining_bits:
            raise CellError(f"cell underflow: need {n} bits, have {self.remaining_bits}")
        if n == 0:
            return 0
        shift = self._cell.bit_length - self._pos - n
        return (self._cell.bits >> shift) & ((1 << n) - 1)

    def load_uint(self, n: int) -> int:
        value = self.preload_uint(n)
        self._pos += n
        return value

    def load_int(self, n: int) -> int:
        value = self.load_uint(n)
        if n and value >> (n - 1):
            value -= 1 << n
        return value

    def load_bit(self) -> bool:
        return bool(self.load_uint(1))

    def load_bytes(self, length: int) -> bytes:
        if length == 0:
            return b""
        return self.load_uint(length * 8).to_bytes(length, "big")

    def load_coins(self) -> int:
        size = self.load_uint(4)
        return self.load_uint(size * 8) if size else 0

    def load_address(self) -> Address | None:
        tag = self.load_uint(2)
        if tag == 0b00:
            return None
        if tag != 0b10:
            raise CellError(f"unsupported address tag {tag:02b}")
        if self.load_bit():
            raise CellError("anycast addresses are not supported")
        wc = self.load_int(8)
        return Address(wc, self.load_bytes(32))

    def load_ref(self) -> Cell:
        if self._ref_pos >= len(self._cell.refs):
            raise CellError("cell underflow: no more references")
        ref = self._cell.refs[self._ref_pos]
        self._ref_pos += 1
        return ref

    def load_maybe_ref(self) -> Cell | None:
        return self.load_ref() if self.load_bit() else None

    def skip_bits(self, n: int) -> "Slice":
        self.load_uint(n)
        return self

    def load_snake_bytes(self) -> bytes:
        if self.remaining_bits % 8:
            raise CellError("snake data is not byte aligned")
        out = bytearray(self.load_bytes(self.remaining_bits // 8))
        if self.remaining_refs:
            out += self.load_ref().begin_parse().load_snake_bytes()
        return bytes(out)

    def load_snake_string(self) -> str:
        return self.load_snake_bytes().decode("utf-8")

    def to_cell(self) -> Cell:
        """Remaining bits and refs as a new cell."""
        return Builder().store_slice(self).end_cell()

    def end_parse(self) -> None:
        if self.remaining_bits or self.remaining_refs:
            raise CellError("slice is not fully consumed")
