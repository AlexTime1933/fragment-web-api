import base64

import pytest

from fragment_api.ton import Address, AddressError, Builder, Cell, CellError, comment_body, read_comment
from fragment_api.ton.boc import deserialize_boc, serialize_boc
from fragment_api.ton.crc import crc16, crc32c
from fragment_api.ton.wallets import V4R2_CODE, V5R1_CODE


def test_crc_known_values():
    assert crc32c(b"123456789") == 0xE3069283
    assert crc16(b"123456789") == 0x31C3


def test_wallet_code_hashes_match_official():
    assert V5R1_CODE.hash().hex() == "20834b7b72b112147e1b2fb457b84e74d1a30f04f737d4f62a668e9552d2b72f"
    assert V4R2_CODE.hash().hex() == "feb5ff6820e2ff0d9483e7e0d62c817d846789fb4ae580c878866d959dabd5c0"


def test_empty_cell_hash():
    # well-known hash of an empty ordinary cell
    assert Cell().hash().hex() == "96a296d224f285c67bee93c30f8a309157f0daa35dc5b87e410b78630a09cfc7"


def test_boc_roundtrip_complex_dag():
    leaf = Builder().store_uint(0xABC, 12).end_cell()
    mid = Builder().store_bit(1).store_ref(leaf).store_ref(leaf).end_cell()
    root = Builder().store_bytes(b"hello").store_ref(mid).store_ref(leaf).end_cell()
    for crc in (True, False):
        data = serialize_boc(root, with_crc=crc)
        back = deserialize_boc(data)[0]
        assert back.hash() == root.hash()
        assert back.to_boc(with_crc=crc) == data
    # shared leaf is stored once
    assert len(set(c.hash() for c in (leaf, mid, root))) == 3


def test_boc_accepts_hex_and_base64():
    cell = Builder().store_uint(7, 3).end_cell()
    raw = cell.to_boc()
    assert Cell.from_boc(raw.hex()) == cell
    assert Cell.from_boc(base64.b64encode(raw).decode()) == cell
    assert Cell.from_boc(base64.urlsafe_b64encode(raw).decode().rstrip("=")) == cell


def test_boc_crc_mismatch_rejected():
    raw = bytearray(Builder().store_uint(1, 8).end_cell().to_boc())
    raw[-1] ^= 0xFF
    with pytest.raises(CellError):
        Cell.from_boc(bytes(raw))


def test_boc_garbage_rejected():
    with pytest.raises(CellError):
        Cell.from_boc(b"\x00\x01\x02\x03\x04")
    with pytest.raises(CellError):
        Cell.from_boc("not a boc !!")


def test_builder_slice_roundtrip():
    addr = Address(0, bytes(range(32)))
    c = (
        Builder()
        .store_uint(5, 8)
        .store_int(-3, 8)
        .store_coins(1_500_000_000)
        .store_address(addr)
        .store_address(None)
        .store_maybe_ref(None)
        .store_maybe_ref(Cell())
        .end_cell()
    )
    s = c.begin_parse()
    assert s.load_uint(8) == 5
    assert s.load_int(8) == -3
    assert s.load_coins() == 1_500_000_000
    assert s.load_address() == addr
    assert s.load_address() is None
    assert s.load_maybe_ref() is None
    assert s.load_maybe_ref() == Cell()
    s.end_parse()


def test_builder_overflow():
    b = Builder().store_uint(0, 1000)
    with pytest.raises(CellError):
        b.store_uint(0, 24)
    b2 = Builder()
    for _ in range(4):
        b2.store_ref(Cell())
    with pytest.raises(CellError):
        b2.store_ref(Cell())
    with pytest.raises(CellError):
        Builder().store_uint(256, 8)
    with pytest.raises(CellError):
        Builder().store_int(128, 8)


def test_long_comment_snake_roundtrip():
    text = "Ref#" + "x" * 500 + " ✓ юникод"
    body = comment_body(text)
    assert read_comment(body) == text
    assert body.refs  # spilled into the ref chain
    assert read_comment(Builder().store_uint(1, 32).end_cell()) is None
    assert read_comment(None) is None


def test_address_forms():
    raw = "0:" + "ab" * 32
    a = Address.parse(raw)
    assert a.to_raw() == raw
    friendly_b = a.to_str(bounceable=True)
    friendly_nb = a.to_str(bounceable=False)
    assert friendly_b.startswith("EQ") and friendly_nb.startswith("UQ")
    assert Address.parse(friendly_b) == a == Address.parse(friendly_nb)
    assert Address.parse(friendly_b).bounceable and not Address.parse(friendly_nb).bounceable
    assert Address.parse(a.to_str(url_safe=False)) == a
    assert Address.parse(a.to_str(test_only=True)).test_only
    m = Address.parse("-1:" + "00" * 32)
    assert m.workchain == -1 and m.to_str().startswith("Ef")


def test_fragment_address_parses():
    a = Address.parse("EQBAjaOyi2wGWlk-EDkSabqqnF-MrrwMadnwqrurKpkla9nE")
    assert a.workchain == 0 and a.bounceable


def test_address_checksum_rejected():
    good = "EQBAjaOyi2wGWlk-EDkSabqqnF-MrrwMadnwqrurKpkla9nE"
    bad = good[:-1] + ("F" if good[-1] != "F" else "G")
    with pytest.raises(AddressError):
        Address.parse(bad)
    assert not Address.is_valid("hello")
    assert not Address.is_valid("0:zz")
