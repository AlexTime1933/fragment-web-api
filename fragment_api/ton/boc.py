"""Bag of Cells (BoC) serialization."""

from __future__ import annotations

import base64
import binascii
import re

from .cell import Cell, CellError
from .crc import crc32c

_MAGIC_GENERIC = b"\xb5\xee\x9c\x72"
_MAGIC_INDEXED = b"\x68\xff\x65\xf3"
_MAGIC_INDEXED_CRC = b"\xac\xc3\xa7\x28"

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _to_bytes(data: "bytes | str") -> bytes:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    text = data.strip()
    if _HEX_RE.match(text) and len(text) % 2 == 0 and text.lower().startswith(
        ("b5ee9c72", "68ff65f3", "acc3a728")
    ):
        return bytes.fromhex(text)
    try:
        padded = text + "=" * (-len(text) % 4)
        return base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CellError("BoC is neither valid hex nor base64") from exc


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise CellError("unexpected end of BoC")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def uint(self, n: int) -> int:
        return int.from_bytes(self.take(n), "big")


def deserialize_boc(data: "bytes | str") -> list[Cell]:
    raw = _to_bytes(data)
    r = _Reader(raw)
    magic = r.take(4)
    if magic == _MAGIC_GENERIC:
        flags = r.uint(1)
        has_idx = bool(flags & 0x80)
        has_crc = bool(flags & 0x40)
        if (flags >> 3) & 0b11:
            raise CellError("unsupported BoC flags")
        size = flags & 0b111
    elif magic in (_MAGIC_INDEXED, _MAGIC_INDEXED_CRC):
        has_idx = True
        has_crc = magic == _MAGIC_INDEXED_CRC
        size = r.uint(1)
    else:
        raise CellError("unknown BoC magic")
    if not 1 <= size <= 4:
        raise CellError("invalid BoC ref size")
    off_bytes = r.uint(1)
    if not 1 <= off_bytes <= 8:
        raise CellError("invalid BoC offset size")
    cells_num = r.uint(size)
    roots_num = r.uint(size)
    absent_num = r.uint(size)
    tot_size = r.uint(off_bytes)
    if absent_num:
        raise CellError("BoC with absent cells is not supported")
    if roots_num < 1 or roots_num > cells_num:
        raise CellError("invalid BoC root count")
    if magic == _MAGIC_GENERIC:
        root_idx = [r.uint(size) for _ in range(roots_num)]
    else:
        root_idx = list(range(roots_num))
    if has_idx:
        r.take(cells_num * off_bytes)
    cells_data = r.take(tot_size)
    if has_crc:
        expected = int.from_bytes(r.take(4), "little")
        if crc32c(raw[:r.pos - 4]) != expected:
            raise CellError("BoC CRC32C mismatch")
    if r.pos != len(raw):
        raise CellError("trailing data after BoC")

    cr = _Reader(cells_data)
    raw_cells: list[tuple[int, int, list[int]]] = []
    for i in range(cells_num):
        d1 = cr.uint(1)
        d2 = cr.uint(1)
        ref_num = d1 & 7
        is_exotic = bool(d1 & 8)
        with_hashes = bool(d1 & 16)
        level_mask = d1 >> 5
        if ref_num > 4:
            raise CellError("invalid reference count in BoC")
        if is_exotic or level_mask:
            raise CellError("exotic cells are not supported")
        if with_hashes:
            cr.take((bin(level_mask).count("1") + 1) * (32 + 2))
        data_len = (d2 + 1) // 2
        data = cr.take(data_len)
        if d2 % 2:
            if not data or data[-1] == 0:
                raise CellError("invalid cell completion tag")
            value = int.from_bytes(data, "big")
            trailing = (value & -value).bit_length()  # position of the tag bit + 1
            bit_len = data_len * 8 - trailing
            bits = value >> trailing
        else:
            bit_len = data_len * 8
            bits = int.from_bytes(data, "big")
        refs = [cr.uint(size) for _ in range(ref_num)]
        for ref in refs:
            if ref <= i or ref >= cells_num:
                raise CellError("BoC is not topologically ordered")
        raw_cells.append((bits, bit_len, refs))
    if cr.pos != len(cells_data):
        raise CellError("BoC cell data size mismatch")

    built: list[Cell | None] = [None] * cells_num
    for i in range(cells_num - 1, -1, -1):
        bits, bit_len, refs = raw_cells[i]
        built[i] = Cell(bits, bit_len, [built[j] for j in refs])  # type: ignore[misc]
    return [built[i] for i in root_idx]  # type: ignore[misc]


def _topological(root: Cell) -> list[Cell]:
    """Order unique cells so that every parent precedes its children."""
    post: list[Cell] = []
    seen: set[bytes] = set()
    stack: list[tuple[Cell, int]] = [(root, 0)]
    seen.add(root.hash())
    while stack:
        cell, i = stack[-1]
        if i < len(cell.refs):
            stack[-1] = (cell, i + 1)
            child = cell.refs[i]
            h = child.hash()
            if h not in seen:
                seen.add(h)
                stack.append((child, 0))
        else:
            stack.pop()
            post.append(cell)
    post.reverse()
    return post


def serialize_boc(root: Cell, *, with_crc: bool = True) -> bytes:
    cells = _topological(root)
    index = {c.hash(): i for i, c in enumerate(cells)}
    n = len(cells)
    size = max(1, (n.bit_length() + 7) // 8)
    payload = bytearray()
    for c in cells:
        payload += c.descriptors()
        payload += c.data_bytes()
        for ref in c.refs:
            payload += index[ref.hash()].to_bytes(size, "big")
    off_bytes = max(1, (len(payload).bit_length() + 7) // 8)
    out = bytearray(_MAGIC_GENERIC)
    out.append((0x40 if with_crc else 0) | size)
    out.append(off_bytes)
    out += n.to_bytes(size, "big")  # cells
    out += (1).to_bytes(size, "big")  # roots
    out += (0).to_bytes(size, "big")  # absent
    out += len(payload).to_bytes(off_bytes, "big")
    out += (0).to_bytes(size, "big")  # root index
    out += payload
    if with_crc:
        out += crc32c(bytes(out)).to_bytes(4, "little")
    return bytes(out)
