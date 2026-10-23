"""In-memory fakes of fragment.com and tonapi.io for offline tests."""

from __future__ import annotations

import json
import time
import urllib.parse

import httpx

from fragment_web_api.ton import Address, Builder, Cell, comment_body
from fragment_web_api.ton.jetton import JETTON_TRANSFER_OP

from .conftest import load_fixture

FRAGMENT_ADDR = "EQBAjaOyi2wGWlk-EDkSabqqnF-MrrwMadnwqrurKpkla9nE"
#: our wallet's USDT jetton wallet in the fakes (FakeTonApi derives it, FakeFragment pays through it)
JETTON_WALLET = "0:" + "ab" * 32


def jetton_transfer(amount: int, destination: str, response: str | None, comment: str,
                    forward_ton: int = 1, custom: bool = False) -> Cell:
    b = (Builder().store_uint(JETTON_TRANSFER_OP, 32).store_uint(7, 64).store_coins(amount)
         .store_address(Address.parse(destination)).store_address(Address.parse(response) if response else None))
    b.store_bit(custom)
    if custom:
        b.store_ref(comment_body("x"))
    b.store_coins(forward_ton).store_bit(True).store_ref(comment_body(comment))
    return b.end_cell()


class FakeFragment:
    def __init__(self, wallet_address: str | None = None) -> None:
        page = load_fixture("stars_page.html")
        if wallet_address:
            page = page.replace('"address":"0:' + "11" * 32 + '"', f'"address":"{wallet_address}"')
        self.page = page
        self.premium_page = load_fixture("premium_page.html")
        self.calls: list[dict] = []
        self.pay_to = FRAGMENT_ADDR
        self.amount_nano = 1_102_100_000
        self.quoted = "1.1021"
        self.extra_response: dict = {}
        self.status = 200
        self.users = {"alice": "RCPT_ALICE", "bobby": "RCPT_BOB"}
        self.wallet_address = wallet_address
        self.method = "ton"  # payment_method of the last init*Request
        # USDT-on-TON payment (payment_method=usdt_ton)
        self.usdt_units = 1_500_000  # 1.5 USDT
        self.usdt_gas_nano = 50_000_000
        self.usdt_to = FRAGMENT_ADDR
        self.usdt_via = JETTON_WALLET
        self.usdt_response: str | None = None  # default: our wallet

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if self.status != 200:
                return httpx.Response(self.status)
            body = self.premium_page if request.url.path.startswith("/premium") else self.page
            return httpx.Response(200, text=body)
        assert request.url.path == "/api"
        assert request.url.params["hash"] == "0123456789abcdef01"
        assert request.headers["x-requested-with"] == "XMLHttpRequest"
        assert "stel_ssid=" in request.headers.get("cookie", "")
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.calls.append(form)
        if self.status != 200:
            return httpx.Response(self.status)
        method = form["method"]
        result = getattr(self, "m_" + method)(dict(form))
        result.update(self.extra_response.get(method, {}))
        return httpx.Response(200, json=result)

    # ----- api methods
    def m_updateStarsPrices(self, f):
        if int(f["quantity"]) < 50:
            return {"ok": True, "error": "You can buy a minimum of 50 stars", "cur_price": ""}
        return load_fixture("updateStarsPrices.json")

    def m_updateStarsBuyState(self, f):
        return load_fixture("updateStarsBuyState.json")

    def m_updatePremiumState(self, f):
        return load_fixture("updatePremiumState.json")

    def _search(self, f):
        rid = self.users.get(f["query"].lower())
        if not rid:
            return {"error": "No Telegram users found."}
        return {"ok": True, "found": {"myself": False, "recipient": rid, "name": f["query"].title(), "photo": ""}}

    m_searchStarsRecipient = _search
    m_searchPremiumGiftRecipient = _search

    def m_initBuyStarsRequest(self, f):
        self.method = f.get("payment_method", "ton")
        return {"req_id": "REQ123", "myself": False, "to_bot": False, "amount": self.quoted,
                "item_title": f"{f['quantity']} Telegram Stars", "content": "", "button": ""}

    def m_initGiftPremiumRequest(self, f):
        self.method = f.get("payment_method", "ton")
        return {"req_id": "PREQ9", "myself": False, "amount": "8.81", "item_title": "Premium"}

    def _link(self, f):
        comment = f"{'Stars' if 'REQ123' == f['id'] else 'Premium'} \n\nRef#{f['id']}"
        if self.method == "usdt_ton":
            payload = jetton_transfer(self.usdt_units, self.usdt_to, self.usdt_response or self.wallet_address,
                                      comment)
            message = {"address": self.usdt_via, "amount": str(self.usdt_gas_nano), "payload": payload.to_boc_b64()}
        else:
            message = {"address": self.pay_to, "amount": str(self.amount_nano),
                       "payload": comment_body(comment).to_boc_b64()}
        return {
            "ok": True,
            "transaction": {
                "validUntil": int(time.time()) + 600,
                "messages": [message],
            },
            "confirm_method": "confirmReq",
            "confirm_params": {"id": f["id"]},
        }

    m_getBuyStarsLink = _link
    m_getGiftPremiumLink = _link

    def m_confirmReq(self, f):
        return {"ok": True}

    def m_checkTonProofAuth(self, f):
        return {"verified": True}

    def m_tonLogOut(self, f):
        return {"ok": True}

    def m_linkWallet(self, f):
        self.extra_response.pop("getBuyStarsLink", None)
        return {"ok": True, "message": "Your wallet has been linked."}

    LINK_POPUP = {  # real shape of Fragment's response (text shortened)
        "error": "Unknown error",
        "need_verify": True,
        "popup": '<div class="popup-header">Link Wallet</div><p>To proceed, you need to link your wallet '
                 'to your account. Once linked, the wallet cannot be removed from the account.</p>'
                 '<button class="btn btn-link btn-lg js-wallet-link-btn">Link Wallet</button>',
    }


class FakeTonApi:
    def __init__(self, balance: int = 10**10, status: str = "active", seqno: int = 4) -> None:
        self.balance = balance
        self.status = status
        self.seqno = seqno
        self.sent: list[str] = []
        self.emulated: list[str] = []
        self.tx_success = True
        self.usdt_balance = 1000 * 10**6
        self.jetton_wallet = JETTON_WALLET

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer test-key"
        p = request.url.path
        if "/jettons/" in p:
            return httpx.Response(200, json={"balance": str(self.usdt_balance),
                                             "wallet_address": {"address": self.jetton_wallet}})
        if p.endswith("/methods/get_wallet_address"):
            return httpx.Response(200, json={"success": True, "exit_code": 0,
                                             "decoded": {"jetton_wallet_address": self.jetton_wallet}})
        if p.startswith("/v2/accounts/"):
            return httpx.Response(200, json={"address": p.split("/")[-1], "balance": self.balance,
                                             "status": self.status})
        if p.endswith("/seqno"):
            return httpx.Response(200, json={"seqno": self.seqno})
        if p == "/v2/blockchain/message":
            self.sent.append(json.loads(request.content)["boc"])
            return httpx.Response(200)
        if p == "/v2/wallet/emulate":
            self.emulated.append(json.loads(request.content)["boc"])
            return httpx.Response(200, json={"trace": {}, "risk": {}, "event": {"actions": []}})
        if p.startswith("/v2/blockchain/messages/"):
            if not self.sent:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={
                "hash": "aa" * 32, "success": self.tx_success, "aborted": not self.tx_success,
                "out_msgs": [{"value": 1}] if self.tx_success else [],
                "action_phase": {"success": self.tx_success, "skipped_actions": 0},
            })
        return httpx.Response(404, json={"error": f"unexpected {p}"})


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def decode_external(boc_b64: str) -> tuple[Cell | None, Cell]:
    c = Cell.from_boc(boc_b64)
    s = c.begin_parse()
    assert s.load_uint(2) == 0b10
    assert s.load_address() is None
    s.load_address()
    s.load_coins()
    init = None
    if s.load_bit():
        if s.load_bit():
            init = s.load_ref()
        else:
            init = Cell(s.load_uint(5), 5, [s.load_ref(), s.load_ref()])
    body = s.load_ref() if s.load_bit() else s.to_cell()
    return init, body


def parse_v5_body(body: Cell) -> dict:
    s = body.begin_parse()
    out = {"op": s.load_uint(32), "wallet_id": s.load_uint(32), "valid_until": s.load_uint(32),
           "seqno": s.load_uint(32)}
    actions = s.load_maybe_ref()
    msgs = []
    while actions is not None and actions.refs:
        a = actions.begin_parse()
        prev = a.load_ref()
        assert a.load_uint(32) == 0x0EC3C86D
        mode = a.load_uint(8)
        m = a.load_ref().begin_parse()
        assert m.load_uint(1) == 0
        m.load_bit()
        bounce = m.load_bit()
        m.load_bit()
        m.load_address()
        dest = m.load_address()
        value = m.load_coins()
        m.load_bit(); m.load_coins(); m.load_coins(); m.load_uint(64); m.load_uint(32)
        assert not m.load_bit()  # no init
        payload = m.load_ref() if m.load_bit() else m.to_cell()
        msgs.append({"mode": mode, "bounce": bounce, "dest": dest, "value": value, "payload": payload})
        actions = prev
    out["messages"] = msgs
    return out


def as_raw(address: str) -> str:
    return Address.parse(address).to_raw()
