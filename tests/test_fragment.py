import asyncio
import json
from decimal import Decimal

import pytest

from fragment_web_api.exceptions import (
    AuthenticationError,
    ConfigError,
    InvalidAmountError,
    RecipientNotFoundError,
    VerificationRequiredError,
    WalletNotConnectedError,
)
from fragment_web_api.fragment import FragmentClient, normalize_username, parse_cookies, parser
from fragment_web_api.fragment.models import TonConnectTransaction

from .conftest import load_fixture
from .fakes import FakeFragment, mock_client

COOKIES = {"stel_ssid": "s", "stel_token": "t", "stel_ton_token": "x", "stel_dt": "-180"}


def run(coro):
    return asyncio.run(coro)


def make(fake: FakeFragment) -> FragmentClient:
    return FragmentClient(COOKIES, http_client=mock_client(fake.handle))


# ------------------------------------------------------------------ parser
def test_parse_stars_options_from_api():
    opts = parser.parse_stars_options(load_fixture("updateStarsBuyState.json")["options_html"])
    assert len(opts) == 22
    assert opts[0].quantity == 50 and opts[0].price_usd == Decimal("0.75")
    assert all(o.price_ton > 0 for o in opts)
    assert [o.quantity for o in opts] == sorted(o.quantity for o in opts)


def test_parse_cur_price_with_thousands_separator():
    p = parser.parse_stars_cur_price(load_fixture("updateStarsPrices.json")["cur_price"], 1_000_000)
    assert p.price_ton == Decimal("11007.11") and p.price_usd == Decimal("15000")


def test_parse_premium_options():
    opts = parser.parse_premium_options(load_fixture("updatePremiumState.json")["options_html"])
    assert [o.months for o in opts] == [12, 6, 3]
    assert opts[0].price_usd == Decimal("28.99") and opts[0].discount == "-52%"


def test_parse_page():
    page = load_fixture("stars_page.html")
    assert parser.api_hash(page) == "0123456789abcdef01"
    ws = parser.wallet_session(page)
    assert ws.logged_in and ws.version == 2 and ws.ton_proof == "TEST_PROOF_PAYLOAD"
    assert parser.is_telegram_logged_in(page)
    assert parser.aj_state(page)["myselfResult"]["found"]["myself"] is True
    assert len(parser.parse_stars_options(parser.extract_options_html(page, "stars"))) == 22
    premium = load_fixture("premium_page.html")
    assert len(parser.parse_premium_options(parser.extract_options_html(premium, "premium"))) == 3


def test_number_parsing():
    assert parser.number('1,234<span class="mini-frac">.56</span>') == Decimal("1234.56")
    assert parser.number("&#036;28.99") == Decimal("28.99")
    assert parser.number("") is None


# ----------------------------------------------------------------- cookies
def test_parse_cookies_formats():
    assert parse_cookies({"a": 1}) == {"a": "1"}
    assert parse_cookies("a=1; b=2") == {"a": "1", "b": "2"}
    assert parse_cookies('{"a": "1"}') == {"a": "1"}
    assert parse_cookies('[{"name": "a", "value": "1", "domain": "fragment.com"}]') == {"a": "1"}
    assert parse_cookies(json.dumps(COOKIES)) == COOKIES
    with pytest.raises(ConfigError):
        FragmentClient({"stel_ssid": "x"})


def test_library_does_not_read_cookie_files(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(COOKIES))
    with pytest.raises(ConfigError):  # a path is just an (invalid) string to the library
        FragmentClient(str(f))


def test_network_errors_are_retried_then_wrapped():
    from fragment_web_api.exceptions import FragmentError
    import httpx

    attempts = []
    fake = FakeFragment()

    def flaky(request):
        attempts.append(request.method)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("boom")
        return fake.handle(request)

    async def go():
        async with FragmentClient(COOKIES, http_client=mock_client(flaky)) as fc:
            return await fc.get_stars_price(1_000_000)

    assert run(go()).quantity == 1_000_000 and attempts[0] == "GET"

    def down(request):
        raise httpx.ConnectError("down")

    async def go_down():
        async with FragmentClient(COOKIES, http_client=mock_client(down), retries=1) as fc:
            await fc.get_stars_price(100)

    with pytest.raises(FragmentError, match="network error"):
        run(go_down())


def test_normalize_username():
    assert normalize_username("@Durov") == "Durov"
    assert normalize_username("https://t.me/some_user") == "some_user"
    for bad in ("", "ab", "1abc", "has space", "a" * 40):
        with pytest.raises(RecipientNotFoundError):
            normalize_username(bad)


# ------------------------------------------------------------------ client
def test_prices_and_search():
    fake = FakeFragment()

    async def go():
        async with make(fake) as fc:
            price = await fc.get_stars_price(1_000_000)
            packages = await fc.get_stars_packages()
            premium = await fc.get_premium_prices()
            rcpt = await fc.search_stars_recipient("@alice", 100)
            me = await fc.myself()
            rate = await fc.ton_rate_usd()
            return price, packages, premium, rcpt, me, rate

    price, packages, premium, rcpt, me, rate = run(go())
    assert price.quantity == 1_000_000 and price.price_ton == Decimal("11007.11")
    assert len(packages) == 22 and len(premium) == 3
    assert rcpt.recipient == "RCPT_ALICE" and rcpt.username == "alice"
    assert me.myself and me.recipient == "TEST_MYSELF_RECIPIENT"
    assert rate == Decimal("1.3629712568159")
    assert fake.calls[0]["method"] == "updateStarsPrices" and fake.calls[0]["quantity"] == "1000000"


def test_errors_are_mapped():
    fake = FakeFragment()

    async def go():
        async with make(fake) as fc:
            with pytest.raises(RecipientNotFoundError):
                await fc.search_stars_recipient("nobody_here")
            with pytest.raises(InvalidAmountError):
                await fc.get_stars_price(10)
            with pytest.raises(InvalidAmountError):
                await fc.init_premium_purchase("R", 5)
            fake.extra_response["initBuyStarsRequest"] = {"need_ton": True}
            with pytest.raises(WalletNotConnectedError):
                await fc.init_stars_purchase("R", 50)
            fake.extra_response["initBuyStarsRequest"] = {"need_verify": True}
            with pytest.raises(VerificationRequiredError):
                await fc.init_stars_purchase("R", 50)
            fake.status = 401
            with pytest.raises(AuthenticationError):
                await fc.call("updateStarsPrices", quantity=50)

    run(go())


def test_purchase_request_and_transaction():
    fake = FakeFragment()

    async def go():
        async with make(fake) as fc:
            req = await fc.init_stars_purchase("RCPT_ALICE", 100)
            tx = await fc.get_transaction(req, account={"a": 1}, device={"d": 2})
            return req, tx

    req, tx = run(go())
    assert req.req_id == "REQ123" and req.amount_ton == Decimal("1.1021") and req.kind == "stars"
    assert isinstance(tx, TonConnectTransaction)
    assert tx.total_amount == fake.amount_nano
    assert tx.messages[0].comment.endswith("Ref#REQ123")
    assert tx.confirm_method == "confirmReq"
    link_call = fake.calls[-1]
    assert link_call["method"] == "getBuyStarsLink" and link_call["transaction"] == "1"
    assert json.loads(link_call["account"]) == {"a": 1}


def test_transaction_valid_until_ms_normalized():
    tx = TonConnectTransaction.from_response({"transaction": {
        "validUntil": 1_900_000_000_000,
        "messages": [{"address": "EQBAjaOyi2wGWlk-EDkSabqqnF-MrrwMadnwqrurKpkla9nE", "amount": "5"}],
    }})
    assert tx.valid_until == 1_900_000_000 and tx.messages[0].payload is None
    with pytest.raises(ValueError):
        TonConnectTransaction.from_response({"ok": True})


def test_wallet_link_popup_is_not_kyc():
    from fragment_web_api import WalletLinkRequiredError

    fake = FakeFragment()
    fake.extra_response["getBuyStarsLink"] = dict(FakeFragment.LINK_POPUP)

    async def go():
        async with make(fake) as fc:
            req = await fc.init_stars_purchase("RCPT_ALICE", 50)
            with pytest.raises(WalletLinkRequiredError) as ei:
                await fc.get_transaction(req)
            assert ei.value.action == "link" and "cannot be removed" in str(ei.value)
            assert isinstance(ei.value, VerificationRequiredError)
            # explicit, separate call answers the popup
            assert "linked" in await fc.link_wallet_to_account()
            return await fc.get_transaction(req)

    tx = run(go())
    assert tx.total_amount == fake.amount_nano
    assert [c["method"] for c in fake.calls].count("linkWallet") == 1


def test_item_title_html_stripped():
    fake = FakeFragment()
    fake.extra_response["initBuyStarsRequest"] = {"item_title": '<svg class="icon"><use></use></svg> 50'}

    async def go():
        async with make(fake) as fc:
            return await fc.init_stars_purchase("RCPT_ALICE", 50)

    assert run(go()).item_title == "50"
