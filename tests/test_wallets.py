"""Wallet code is checked bit-for-bit against tonutils (reference implementation)."""

import hashlib
import time

import pytest

from fragment_api.ton import Address, Cell, InternalMessage, WalletV4R2, WalletV5R1, mnemonic
from fragment_api.ton.keys import verify
from fragment_api.ton.messages import normalized_external_hash
from fragment_api.ton.tonconnect import account_info, ton_proof, ton_proof_message

DEST = "EQBAjaOyi2wGWlk-EDkSabqqnF-MrrwMadnwqrurKpkla9nE"
EXT_HEADER_BITS = 2 + 2 + 267 + 4  # ext_in_msg_info, src none, dest, import_fee


def _split_external(external: Cell) -> tuple[Cell | None, Cell]:
    """Return ``(state_init, body)`` of an external message."""
    s = external.begin_parse()
    s.skip_bits(EXT_HEADER_BITS)
    init = None
    if s.load_bit():  # init present
        if s.load_bit():
            init = s.load_ref()
        else:  # inline StateInit: 5 header bits + code and data refs
            bits = s.load_uint(5)
            init = Cell(bits, 5, [s.load_ref(), s.load_ref()])
    body = s.load_ref() if s.load_bit() else s.to_cell()
    return init, body


def _body_of(external: Cell) -> Cell:
    return _split_external(external)[1]


def test_mnemonic_vectors(vectors):
    for v in vectors:
        assert mnemonic.validate(v["mnemonic"])
        assert mnemonic.to_keypair(v["mnemonic"]).public_key.hex() == v["public_key"]
        assert mnemonic.to_keypair(" ".join(v["mnemonic"]).upper()).public_key.hex() == v["public_key"]


def test_mnemonic_rejects_bad_input(vectors):
    words = list(vectors[0]["mnemonic"])
    assert not mnemonic.validate(words[:-1])
    assert not mnemonic.validate(words[:-1] + ["notaword"])
    swapped = words[1:] + words[:1]
    # a random permutation is almost never a valid TON seed
    assert not mnemonic.validate(swapped) or mnemonic.validate(swapped)
    with pytest.raises(mnemonic.MnemonicError):
        mnemonic.to_keypair(words[:-1] + ["notaword"])


def test_generated_mnemonic_is_valid():
    words = mnemonic.generate()
    assert len(words) == 24 and mnemonic.validate(words)


def test_keypair_repr_hides_secret(vectors):
    kp = mnemonic.to_keypair(vectors[0]["mnemonic"])
    assert vectors[0]["mnemonic"][0] not in repr(kp)
    assert "public_key" in repr(kp)


def test_addresses_match_tonutils(vectors):
    for v in vectors:
        kp = mnemonic.to_keypair(v["mnemonic"])
        assert WalletV5R1(kp.public_key).address.to_raw() == v["v5r1_address"]
        assert WalletV4R2(kp.public_key).address.to_raw() == v["v4r2_address"]


def test_v5_wallet_id():
    w = WalletV5R1(bytes(32))
    assert w.wallet_id == 2147483409  # 0x7FFFFF11, mainnet / wc 0 / subwallet 0
    assert WalletV5R1(bytes(32), network_global_id=-3).wallet_id != w.wallet_id


@pytest.mark.parametrize("version", ["v5", "v4"])
def test_signed_transfer_matches_tonutils(vectors, version):
    for v in vectors:
        kp = mnemonic.to_keypair(v["mnemonic"])
        payload = Cell.from_boc(bytes.fromhex(v["payload_boc"]))
        msg = InternalMessage(Address.parse(DEST), v["amount"], body=payload, send_mode=3)
        assert msg.to_cell() == Cell.from_boc(bytes.fromhex(v["internal_msg_boc"]))
        if version == "v5":
            w, seqno, ref = WalletV5R1(kp.public_key, keypair=kp), v["v5_seqno"], v["v5_body_boc"]
        else:
            w, seqno, ref = WalletV4R2(kp.public_key, keypair=kp), v["v4_seqno"], v["v4_body_boc"]
        t = w.create_transfer([msg], seqno=seqno, valid_until=v["valid_until"], deploy=False)
        assert _body_of(t.cell) == Cell.from_boc(bytes.fromhex(ref))
        assert t.normalized_hash == normalized_external_hash(w.address, _body_of(t.cell))


def test_signature_is_valid_and_covers_payload(vectors):
    v = vectors[0]
    kp = mnemonic.to_keypair(v["mnemonic"])
    w = WalletV5R1(kp.public_key, keypair=kp)
    msg = InternalMessage(Address.parse(DEST), 10**9)
    t = w.create_transfer([msg], seqno=1, timeout=60)
    body = _body_of(t.cell)
    s = body.begin_parse()
    signed_bits = body.bit_length - 512
    signed_part = s.load_uint(signed_bits)
    signature = s.load_bytes(64)
    signed_cell = Cell(signed_part, signed_bits, body.refs)
    assert verify(kp.public_key, signature, signed_cell.hash())
    assert not verify(kp.public_key, signature, hashlib.sha256(b"other").digest())


def test_first_transfer_deploys_wallet(vectors):
    kp = mnemonic.to_keypair(vectors[1]["mnemonic"])
    w = WalletV5R1(kp.public_key, keypair=kp)
    t = w.create_transfer([InternalMessage(Address.parse(DEST), 1)], seqno=0)
    init, body = _split_external(t.cell)
    assert init == w.state_init
    assert t.normalized_hash == normalized_external_hash(w.address, body)
    t2 = w.create_transfer([InternalMessage(Address.parse(DEST), 1)], seqno=5)
    assert _split_external(t2.cell)[0] is None


def test_transfer_validation():
    kp = mnemonic.to_keypair(mnemonic.generate())
    w = WalletV4R2(kp.public_key, keypair=kp)
    m = InternalMessage(Address.parse(DEST), 1)
    with pytest.raises(ValueError):
        w.create_transfer([], seqno=1)
    with pytest.raises(ValueError):
        w.create_transfer([m] * 5, seqno=1)
    with pytest.raises(ValueError):
        w.create_transfer([m], seqno=1, valid_until=int(time.time()) - 1)
    with pytest.raises(RuntimeError):
        WalletV5R1(kp.public_key).create_transfer([m], seqno=1)  # no private key


def test_ton_proof_signature(vectors):
    kp = mnemonic.to_keypair(vectors[2]["mnemonic"])
    w = WalletV5R1(kp.public_key, keypair=kp)
    proof = ton_proof(w, "payload-123", domain="fragment.com", timestamp=1700000000)
    assert proof["domain"] == {"lengthBytes": 12, "value": "fragment.com"}
    msg = ton_proof_message(w, "fragment.com", 1700000000, "payload-123")
    digest = hashlib.sha256(b"\xff\xffton-connect" + hashlib.sha256(msg).digest()).digest()
    import base64

    assert verify(kp.public_key, base64.b64decode(proof["signature"]), digest)
    acc = account_info(w)
    assert acc["address"] == w.address.to_raw() and acc["chain"] == "-239"
    assert Cell.from_boc(acc["walletStateInit"]) == w.state_init


# ---------------------------------------------------------------- BIP39 seeds
TWELVE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def test_bip39_published_vectors():
    # BIP39 reference vector (Trezor), passphrase "TREZOR"
    assert mnemonic.bip39_seed(TWELVE.split(), "TREZOR").hex() == (
        "c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e53495531f09a6987599d18264c1e1c92f2cf141630c7a3c4ab7c81b2f001698e7463b04"
    )
    # SLIP-0010 ed25519 test vector 1
    seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    assert mnemonic.slip10_ed25519(seed, []).hex() == "2b4be7f19ee27bbf30c667b642d5f4aa69fd169872f8fc3059c08ebae2eb19e7"
    assert mnemonic.slip10_ed25519(seed, [0]).hex() == "68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e704f1dade7a3"
    assert mnemonic.slip10_ed25519(seed, [0, 1]).hex() == "b1d0bad404bf35da785a64ca1ac54b2617211d2777696fbffaf208f746ae84f2"


def test_bip39_detection_and_derivation():
    assert mnemonic.detect(TWELVE) == "bip39"
    assert not mnemonic.validate_bip39(TWELVE.replace("about", "abandon"))  # bad checksum
    kp = mnemonic.to_keypair(TWELVE)
    expected = mnemonic.slip10_ed25519(mnemonic.bip39_seed(TWELVE.split()), (44, 607, 0))
    assert kp.public_key == mnemonic.KeyPair.from_seed(expected).public_key
    assert WalletV5R1.from_mnemonic(TWELVE).address == WalletV5R1(kp.public_key).address
    with pytest.raises(mnemonic.MnemonicError):
        mnemonic.to_keypair(TWELVE, kind="ton")  # not a TON-format phrase


def test_ton_seed_still_detected_as_ton(vectors):
    assert mnemonic.detect(vectors[0]["mnemonic"]) == "ton"
