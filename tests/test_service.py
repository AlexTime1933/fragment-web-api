"""End-to-end purchase flow against in-memory fakes (no network, no real funds)."""

import asyncio
from decimal import Decimal

import pytest

from fragment_web_api import Fragment, SafetyPolicy
from fragment_web_api.exceptions import (
    InsufficientBalanceError,
    TransactionFailedError,
    TransactionSafetyError,
)
from fragment_web_api.fragment import FragmentClient
from fragment_web_api.ton import Address, WalletV5R1, mnemonic, read_comment
from fragment_web_api.ton.keys import verify
from fragment_web_api.ton.cell import Cell
from fragment_web_api.ton.tonapi import TonApiClient

from .fakes import FRAGMENT_ADDR, FakeFragment, FakeTonApi, decode_external, mock_client, parse_v5_body

COOKIES = {"stel_ssid": "s", "stel_token": "t"}


@pytest.fixture(scope="module")
def wallet():
    return WalletV5R1.from_mnemonic(mnemonic.generate())


def build(wallet, fake_frag=None, fake_ton=None, safety=None, link=True):
    fake_frag = fake_frag or FakeFragment(wallet.address.to_raw() if link else None)
    fake_ton = fake_ton or FakeTonApi()
    client = FragmentClient(COOKIES, http_client=mock_client(fake_frag.handle))
    tonapi = TonApiClient("test-key", http_client=mock_client(fake_ton.handle), max_retries=0)
    f = Fragment(client, wallet=wallet, tonapi=tonapi, safety=safety, confirm_timeout=2)
    return f, fake_frag, fake_ton


def run(coro):
    return asyncio.run(coro)


def test_buy_stars_happy_path(wallet):
    f, frag, ton = build(wallet)
    result = run(f.buy_stars("alice", 100))
    assert result.sent and result.confirmed
    assert len(ton.sent) == 1 and ton.sent[0] == result.boc
    init, body = decode_external(result.boc)
    assert init is None  # wallet is active (seqno 4)
    parsed = parse_v5_body(body)
    assert parsed["op"] == 0x7369676E and parsed["seqno"] == 4 and parsed["wallet_id"] == wallet.wallet_id
    (msg,) = parsed["messages"]
    assert msg["dest"] == Address.parse(FRAGMENT_ADDR)
    assert msg["value"] == frag.amount_nano
    assert msg["bounce"] is True
    assert msg["mode"] == 3
    assert read_comment(msg["payload"]).endswith("Ref#REQ123")
    # signature check
    s = body.begin_parse()
    n = body.bit_length - 512
    signed = Cell(s.load_uint(n), n, body.refs)
    assert verify(wallet.public_key, s.load_bytes(64), signed.hash())
    methods = [c["method"] for c in frag.calls]
    assert methods == ["searchStarsRecipient", "initBuyStarsRequest", "getBuyStarsLink", "confirmReq"]
    assert frag.calls[-1]["boc"] == result.boc and frag.calls[-1]["id"] == "REQ123"


def test_gift_premium(wallet):
    f, frag, ton = build(wallet)
    frag.amount_nano = 8_810_000_000
    result = run(f.gift_premium("@bobby", 3))
    assert result.request.kind == "premium" and result.request.quantity == 3
    assert [c["method"] for c in frag.calls][:3] == [
        "searchPremiumGiftRecipient", "initGiftPremiumRequest", "getGiftPremiumLink"]
    assert frag.calls[1]["months"] == "3"


def test_buy_for_myself(wallet):
    f, frag, ton = build(wallet)
    run(f.buy_stars(None, 50))
    assert frag.calls[0]["method"] == "initBuyStarsRequest"
    assert frag.calls[0]["recipient"] == "TEST_MYSELF_RECIPIENT"


def test_first_purchase_deploys_uninit_wallet(wallet):
    f, frag, ton = build(wallet, fake_ton=FakeTonApi(status="uninit", seqno=0))
    result = run(f.buy_stars("alice", 100))
    init, body = decode_external(result.boc)
    assert init == wallet.state_init and parse_v5_body(body)["seqno"] == 0


def test_dry_run_does_not_send(wallet):
    f, frag, ton = build(wallet)
    result = run(f.buy_stars("alice", 100, dry_run=True))
    assert not result.sent and ton.sent == [] and ton.emulated == [result.boc]
    assert "confirmReq" not in [c["method"] for c in frag.calls]


def test_rejects_unknown_destination(wallet):
    frag = FakeFragment(wallet.address.to_raw())
    frag.pay_to = WalletV5R1(bytes(32)).address.to_str()
    f, frag, ton = build(wallet, fake_frag=frag)
    with pytest.raises(TransactionSafetyError, match="destination"):
        run(f.buy_stars("alice", 100))
    assert ton.sent == []


def test_rejects_amount_above_quote(wallet):
    frag = FakeFragment(wallet.address.to_raw())
    frag.amount_nano = 5 * 10**9  # quote says 1.1021 TON
    f, frag, ton = build(wallet, fake_frag=frag)
    with pytest.raises(TransactionSafetyError, match="exceeds quoted"):
        run(f.buy_stars("alice", 100))
    assert ton.sent == []


def test_rejects_amount_above_cap(wallet):
    f, frag, ton = build(wallet, safety=SafetyPolicy(max_amount_ton=Decimal("1")))
    with pytest.raises(TransactionSafetyError, match="max_amount_ton"):
        run(f.buy_stars("alice", 100))
    assert ton.sent == []


def test_rejects_foreign_session_wallet(wallet):
    other = WalletV5R1(bytes(range(32)))
    f, frag, ton = build(wallet, fake_frag=FakeFragment(other.address.to_raw()))
    with pytest.raises(TransactionSafetyError, match="wrong seed"):
        run(f.buy_stars("alice", 100))
    assert ton.sent == []
    assert "initBuyStarsRequest" not in [c["method"] for c in frag.calls]
    assert "getBuyStarsLink" not in [c["method"] for c in frag.calls]


def test_insufficient_balance(wallet):
    f, frag, ton = build(wallet, fake_ton=FakeTonApi(balance=10**9))
    with pytest.raises(InsufficientBalanceError):
        run(f.buy_stars("alice", 100))
    assert ton.sent == []


def test_failed_chain_transaction(wallet):
    ton = FakeTonApi()
    ton.tx_success = False
    f, frag, ton = build(wallet, fake_ton=ton)
    with pytest.raises(TransactionFailedError):
        run(f.buy_stars("alice", 100))
    assert "confirmReq" not in [c["method"] for c in frag.calls]


def test_concurrent_purchases_are_serialized(wallet):
    f, frag, ton = build(wallet)

    async def go():
        return await asyncio.gather(f.buy_stars("alice", 100), f.buy_stars("bobby", 50))

    results = run(go())
    assert len(ton.sent) == 2 and all(r.sent for r in results)


def test_link_wallet_with_ton_proof(wallet):
    f, frag, ton = build(wallet, link=False)
    # fixture session has a different (dummy) wallet linked -> must refuse without force
    with pytest.raises(Exception, match="another wallet"):
        run(f.link_wallet())
    assert run(f.link_wallet(force=True))
    call = next(c for c in frag.calls if c["method"] == "checkTonProofAuth")
    import json

    proof = json.loads(call["proof"])
    account = json.loads(call["account"])
    assert proof["payload"] == "TEST_PROOF_PAYLOAD" and proof["domain"]["value"] == "fragment.com"
    assert account["address"] == wallet.address.to_raw() and account["publicKey"] == wallet.public_key.hex()


def test_stars_payment_address_is_allowed(wallet):
    frag = FakeFragment(wallet.address.to_raw())
    frag.pay_to = "UQCFJEP4WZ_mpdo0_kMEmsTgvrMHG7K_tWY16pQhKHwoOtFz"  # Fragment's Stars wallet (seen live)
    f, frag, ton = build(wallet, fake_frag=frag)
    assert run(f.buy_stars("alice", 100)).sent


def test_rejects_foreign_from_field(wallet):
    other = WalletV5R1(bytes(range(32))).address.to_raw()
    frag = FakeFragment(wallet.address.to_raw())
    frag.extra_response["getBuyStarsLink"] = {}
    orig = frag._link

    def link_with_from(f):
        res = orig(f)
        res["transaction"]["from"] = other
        return res

    frag.m_getBuyStarsLink = link_with_from
    f, frag, ton = build(wallet, fake_frag=frag)
    with pytest.raises(TransactionSafetyError, match="expects payment from"):
        run(f.buy_stars("alice", 100))
    assert ton.sent == []


# --------------------------------------------------------------- USDT on TON
def _usdt(wallet, **frag_attrs):
    f, frag, ton = build(wallet)
    for k, v in frag_attrs.items():
        setattr(frag, k, v)
    return f, frag, ton


def _usdt_rejected(wallet, match, safety=None, **frag_attrs):
    f, frag, ton = build(wallet, safety=safety)
    for k, v in frag_attrs.items():
        setattr(frag, k, v)
    with pytest.raises(TransactionSafetyError, match=match):
        run(f.buy_stars("alice", 100, payment_method="usdt_ton"))
    assert ton.sent == []


def test_usdt_payment(wallet):
    from fragment_web_api.ton.jetton import parse_transfer

    from .fakes import JETTON_WALLET

    f, frag, ton = _usdt(wallet)
    result = run(f.buy_stars("alice", 100, payment_method="usdt_ton"))
    assert result.sent and result.extra["currency"] == "USDT" and result.extra["amount_usdt"] == Decimal("1.5")
    assert frag.calls[1]["payment_method"] == "usdt_ton"
    assert result.request.amount_usd == Decimal("15000")  # Fragment's USD price, the USDT limit
    (msg,) = parse_v5_body(decode_external(result.boc)[1])["messages"]
    assert msg["dest"] == Address.parse(JETTON_WALLET) and msg["value"] == frag.usdt_gas_nano
    t = parse_transfer(msg["payload"])
    assert t.amount == 1_500_000 and t.destination == Address.parse(FRAGMENT_ADDR)
    assert t.response_destination == wallet.address and t.comment.endswith("Ref#REQ123")


def test_usdt_premium_and_balance(wallet):
    f, frag, ton = _usdt(wallet)
    result = run(f.gift_premium("bobby", 3, payment_method="usdt_ton"))
    assert result.sent and result.request.amount_usd
    assert run(f.get_usdt_balance()) == Decimal("1000")


def test_usdt_rejects_foreign_jetton_wallet(wallet):
    _usdt_rejected(wallet, "not to our USDT wallet", usdt_via="0:" + "cd" * 32)


def test_usdt_rejects_foreign_destination(wallet):
    _usdt_rejected(wallet, "unexpected USDT destination", usdt_to="0:" + "ee" * 32)


def test_usdt_rejects_foreign_response(wallet):
    _usdt_rejected(wallet, "excess to a foreign address", usdt_response="0:" + "ee" * 32)


def test_usdt_rejects_amount_above_price(wallet):
    _usdt_rejected(wallet, "does not match the price", usdt_units=16_000 * 10**6)


def test_usdt_rejects_amount_above_cap(wallet):
    _usdt_rejected(wallet, "max_amount_usdt", safety=SafetyPolicy(max_amount_usdt=Decimal("1")))


def test_usdt_rejects_too_much_gas(wallet):
    _usdt_rejected(wallet, "for gas", usdt_gas_nano=2 * 10**9)


def test_usdt_rejects_plain_ton_transfer(wallet):
    # Fragment answers a USDT request with a TON transfer to the jetton wallet: no payload -> refused
    f, frag, ton = build(wallet)
    frag.extra_response["getBuyStarsLink"] = {"transaction": {
        "messages": [{"address": frag.usdt_via, "amount": "50000000"}], "validUntil": 9999999999}}
    with pytest.raises(TransactionSafetyError, match="no jetton transfer payload"):
        run(f.buy_stars("alice", 100, payment_method="usdt_ton"))


def test_usdt_insufficient_balance(wallet):
    f, frag, ton = build(wallet)
    ton.usdt_balance = 10**6
    with pytest.raises(InsufficientBalanceError, match="USDT"):
        run(f.buy_stars("alice", 100, payment_method="usdt_ton"))
    assert ton.sent == []


def test_unknown_payment_method(wallet):
    from fragment_web_api.exceptions import ConfigError

    f, frag, ton = build(wallet)
    with pytest.raises(ConfigError):
        run(f.buy_stars("alice", 100, payment_method="usdt_eth"))
