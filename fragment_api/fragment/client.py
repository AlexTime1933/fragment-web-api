"""Low-level async client for Fragment's web API (``POST /api?hash=...``).

Fragment has no public API; this mirrors what the site's own JavaScript
(``/js/auction.js``) sends. Every call needs the session cookies of a
browser that is logged in to Fragment with Telegram.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal
from typing import Any, Mapping

import httpx

from ..exceptions import (
    AuthenticationError,
    ConfigError,
    FragmentError,
    InvalidAmountError,
    RecipientNotFoundError,
    VerificationRequiredError,
    WalletLinkRequiredError,
    WalletNotConnectedError,
)
from . import parser
from .models import (
    PremiumPrice,
    PurchaseRequest,
    Recipient,
    StarsPrice,
    TonConnectTransaction,
    WalletSession,
)

log = logging.getLogger(__name__)

BASE_URL = "https://fragment.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
REQUIRED_COOKIES = ("stel_ssid", "stel_token")

STARS_MIN = 50
STARS_MAX = 10_000_000
PREMIUM_MONTHS = (3, 6, 12)

_USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")


def parse_cookies(source: "str | Mapping[str, str]") -> dict[str, str]:
    """Cookies from a dict, a JSON string (object or browser-export list) or a ``k=v; k=v`` string.

    Only values are accepted: reading cookie files is the caller's job.
    """
    if isinstance(source, Mapping):
        return {str(k): str(v) for k, v in source.items()}
    text = str(source)
    text = text.strip()
    if not text:
        raise ConfigError("cookies are empty")
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        if isinstance(data, list):  # browser-extension export: [{name, value, ...}]
            return {c["name"]: c["value"] for c in data if "name" in c and "value" in c}
        return {str(k): str(v) for k, v in data.items()}
    cookies = {}
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def normalize_username(username: str) -> str:
    username = username.strip()
    if username.startswith("https://t.me/"):
        username = username[len("https://t.me/"):]
    username = username.lstrip("@")
    if not _USERNAME_RE.match(username):
        raise RecipientNotFoundError(f"invalid Telegram username: {username!r}")
    return username


class FragmentClient:
    """Async client. Use as ``async with FragmentClient(cookies) as fc: ...``."""

    def __init__(
        self,
        cookies: "str | Mapping[str, str]",
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        retries: int = 2,
        base_url: str = BASE_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        cookie_map = parse_cookies(cookies)
        missing = [c for c in REQUIRED_COOKIES if not cookie_map.get(c)]
        if missing:
            raise ConfigError(f"missing Fragment cookies: {', '.join(missing)}")
        self._base = base_url.rstrip("/")
        self._own_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0),
                                                     follow_redirects=False)
        self._http.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"})
        domain = httpx.URL(self._base).host
        for k, v in cookie_map.items():
            self._http.cookies.set(k, v, domain=domain)
        self._retries = retries
        self._api_hash: str | None = None
        self._page_state: dict[str, Any] = {}
        self._wallet: WalletSession | None = None
        self._logged_in: bool | None = None

    async def aclose(self) -> None:
        if self._own_client:
            await self._http.aclose()

    async def __aenter__(self) -> "FragmentClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------ cookies
    @property
    def cookies(self) -> dict[str, str]:
        """Current cookies (Fragment may rotate ``stel_ton_token`` etc.)."""
        return {c.name: c.value for c in self._http.cookies.jar}

    # -------------------------------------------------------------- pages
    async def fetch_page(self, path: str = "/stars/buy") -> str:
        resp = await self._send(
            "GET", f"{self._base}{path}", idempotent=True,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            raise AuthenticationError(f"Fragment redirected {path} to {resp.headers.get('location')!r}")
        if resp.status_code != 200:
            raise FragmentError(f"GET {path} failed with HTTP {resp.status_code}")
        page = resp.text
        h = parser.api_hash(page)
        if h:
            self._api_hash = h
        self._page_state = parser.aj_state(page) or self._page_state
        self._wallet = parser.wallet_session(page) or self._wallet
        self._logged_in = parser.is_telegram_logged_in(page)
        return page

    async def _send(self, http_method: str, url: str, *, idempotent: bool = False,
                    method_name: str | None = None, **kwargs: Any) -> httpx.Response:
        """HTTP request with retries on connection failures.

        A failed *connect* means nothing reached Fragment, so it is always safe
        to retry; other network errors are retried only for idempotent GETs.
        """
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                return await self._http.request(http_method, url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
            except httpx.TransportError as exc:
                last = exc
                if not idempotent:
                    break
            if attempt < self._retries:
                log.warning("fragment.com network error (%s), retrying", type(last).__name__)
                await asyncio.sleep(1 + attempt)
        raise FragmentError(f"network error talking to fragment.com: {type(last).__name__}",
                            method=method_name) from last

    async def _ensure_hash(self) -> str:
        if not self._api_hash:
            await self.fetch_page("/stars/buy")
        if not self._api_hash:
            raise FragmentError("could not find the API hash on the Fragment page (site layout changed?)")
        return self._api_hash

    # ---------------------------------------------------------------- api
    async def call(self, method: str, referer: str = "/stars/buy", **params: Any) -> dict:
        """Call ``/api`` like the site does and return the decoded JSON."""
        api_hash = await self._ensure_hash()
        data = {k: _form_value(v) for k, v in params.items() if v is not None}
        data["method"] = method
        resp = await self._send(
            "POST", f"{self._base}/api", method_name=method,
            params={"hash": api_hash},
            data=data,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self._base,
                "Referer": f"{self._base}{referer}",
            },
        )
        if resp.status_code == 401:
            raise AuthenticationError("Fragment session is not authorized (cookies expired?)", method=method)
        if resp.status_code != 200:
            raise FragmentError(f"{method}: HTTP {resp.status_code}", method=method, response=resp.text[:500])
        try:
            result = resp.json()
        except ValueError as exc:
            raise FragmentError(f"{method}: non-JSON response", method=method, response=resp.text[:500]) from exc
        if not isinstance(result, dict):
            raise FragmentError(f"{method}: unexpected response", method=method, response=result)
        if result.get("need_ton"):
            raise WalletNotConnectedError("no TON wallet is connected to this Fragment session",
                                          method=method, response=result)
        if result.get("need_verify"):
            popup = str(result.get("popup") or "")
            if "js-wallet-link-btn" in popup or "js-wallet-check-btn" in popup:
                action = "link" if "js-wallet-link-btn" in popup else "check"
                raise WalletLinkRequiredError(
                    "Fragment asks to link the connected wallet to your Telegram account "
                    f"(one-time, irreversible): {parser.text(popup)[:300]}",
                    action=action, method=method, response=result,
                )
            raise VerificationRequiredError(
                "Fragment requires verification (KYC) for this account; complete it in the browser",
                method=method, response=result,
            )
        return result

    @staticmethod
    def _raise_error(result: dict, method: str, exc_type: type[FragmentError] = FragmentError) -> None:
        if result.get("error"):
            raise exc_type(parser.text(str(result["error"])), method=method, response=result)

    # ------------------------------------------------------- session info
    async def wallet_session(self, refresh: bool = False) -> WalletSession | None:
        if refresh or self._wallet is None:
            await self.fetch_page("/stars/buy")
        return self._wallet

    async def is_logged_in(self) -> bool:
        await self.fetch_page("/stars/buy")
        return bool(self._logged_in)

    async def ton_rate_usd(self) -> Decimal | None:
        """TON/USD rate Fragment uses for its prices (from page state)."""
        await self.fetch_page("/stars/buy")
        rate = self._page_state.get("tonRate")
        return Decimal(str(rate)) if rate else None

    async def myself(self, kind: str = "stars") -> Recipient | None:
        """The logged-in Telegram account as a recipient ("buy for myself")."""
        await self.fetch_page("/stars/buy" if kind == "stars" else "/premium/gift")
        res = self._page_state.get("myselfResult") or {}
        found = res.get("found")
        return _recipient(found, None) if found else None

    # ----------------------------------------------------------- prices
    async def get_stars_price(self, quantity: int) -> StarsPrice:
        _check_stars(quantity)
        result = await self.call("updateStarsPrices", stars=0, quantity=quantity)
        self._raise_error(result, "updateStarsPrices", InvalidAmountError)
        price = parser.parse_stars_cur_price(result.get("cur_price") or "", quantity)
        if price is None:
            raise FragmentError("could not parse stars price", method="updateStarsPrices", response=result)
        return price

    async def get_stars_packages(self) -> list[StarsPrice]:
        result = await self.call("updateStarsBuyState", mode="new", lv="false", dh=0)
        html_ = result.get("options_html") or ""
        if not html_:
            page = await self.fetch_page("/stars/buy")
            html_ = parser.extract_options_html(page, "stars") or ""
        return parser.parse_stars_options(html_)

    async def get_premium_prices(self) -> list[PremiumPrice]:
        result = await self.call("updatePremiumState", referer="/premium/gift", mode="new", lv="false", dh=0)
        html_ = result.get("options_html") or ""
        if not html_:
            page = await self.fetch_page("/premium/gift")
            html_ = parser.extract_options_html(page, "premium") or ""
        return parser.parse_premium_options(html_)

    # -------------------------------------------------------- recipients
    async def search_stars_recipient(self, username: str, quantity: int | None = None) -> Recipient:
        username = normalize_username(username)
        result = await self.call("searchStarsRecipient", query=username, quantity=quantity or "")
        self._raise_error(result, "searchStarsRecipient", RecipientNotFoundError)
        if not result.get("found"):
            raise RecipientNotFoundError(f"@{username} not found", method="searchStarsRecipient", response=result)
        return _recipient(result["found"], username)

    async def search_premium_recipient(self, username: str, months: int = 3) -> Recipient:
        username = normalize_username(username)
        _check_months(months)
        result = await self.call("searchPremiumGiftRecipient", referer="/premium/gift", query=username, months=months)
        self._raise_error(result, "searchPremiumGiftRecipient", RecipientNotFoundError)
        if not result.get("found"):
            raise RecipientNotFoundError(f"@{username} not found", method="searchPremiumGiftRecipient",
                                         response=result)
        return _recipient(result["found"], username)

    # ---------------------------------------------------------- purchases
    async def init_stars_purchase(self, recipient: "Recipient | str", quantity: int,
                                  payment_method: str = "ton") -> PurchaseRequest:
        _check_stars(quantity)
        rid = recipient.recipient if isinstance(recipient, Recipient) else recipient
        result = await self.call("initBuyStarsRequest", recipient=rid, quantity=quantity,
                                 payment_method=payment_method)
        self._raise_error(result, "initBuyStarsRequest")
        return _purchase(result, "stars", quantity, rid, "initBuyStarsRequest", payment_method)

    async def init_premium_purchase(self, recipient: "Recipient | str", months: int,
                                    payment_method: str = "ton") -> PurchaseRequest:
        _check_months(months)
        rid = recipient.recipient if isinstance(recipient, Recipient) else recipient
        result = await self.call("initGiftPremiumRequest", referer="/premium/gift", recipient=rid,
                                 months=months, payment_method=payment_method)
        self._raise_error(result, "initGiftPremiumRequest")
        return _purchase(result, "premium", months, rid, "initGiftPremiumRequest", payment_method)

    async def get_transaction(
        self,
        request: PurchaseRequest,
        *,
        show_sender: bool = False,
        account: dict | None = None,
        device: dict | None = None,
    ) -> TonConnectTransaction:
        """Ask Fragment for the TON Connect transaction paying ``request``."""
        if request.kind == "stars":
            method, referer = "getBuyStarsLink", "/stars/buy"
        elif request.kind == "premium":
            method, referer = "getGiftPremiumLink", "/premium/gift"
        else:
            raise ValueError(f"unknown purchase kind {request.kind!r}")
        params: dict[str, Any] = {"id": request.req_id, "show_sender": 1 if show_sender else 0, "transaction": 1}
        if account is not None:
            params["account"] = json.dumps(account, separators=(",", ":"))
        if device is not None:
            params["device"] = json.dumps(device, separators=(",", ":"))
        result = await self.call(method, referer=referer, **params)
        self._raise_error(result, method)
        if result.get("evm"):
            raise FragmentError("Fragment returned an EVM payment flow; only TON is supported",
                                method=method, response=result)
        try:
            return TonConnectTransaction.from_response(result)
        except (ValueError, KeyError) as exc:
            raise FragmentError(f"{method}: unexpected transaction format: {exc}", method=method,
                                response=result) from exc

    async def confirm_transaction(self, tx: TonConnectTransaction, *, boc: str,
                                  account: dict | None, device: dict | None) -> dict | None:
        """Report the sent message to Fragment, as the site does after TON Connect returns."""
        if not tx.confirm_method:
            return None
        params = dict(tx.confirm_params)
        params["boc"] = boc
        if account is not None:
            params["account"] = json.dumps(account, separators=(",", ":"))
        if device is not None:
            params["device"] = json.dumps(device, separators=(",", ":"))
        return await self.call(tx.confirm_method, **params)

    async def repeat_stars(self) -> dict:
        """Reset the stars purchase form (the "Buy more" button)."""
        return await self.call("repeatStars")

    async def repeat_premium(self) -> dict:
        return await self.call("repeatPremium", referer="/premium/gift")

    # ------------------------------------------------------------ wallet
    async def check_ton_proof(self, account: dict, device: dict, proof: dict) -> dict:
        """Link a wallet to the session (``checkTonProofAuth``)."""
        return await self.call(
            "checkTonProofAuth",
            account=json.dumps(account, separators=(",", ":")),
            device=json.dumps(device, separators=(",", ":")),
            proof=json.dumps(proof, separators=(",", ":")),
        )

    async def ton_logout(self) -> dict:
        return await self.call("tonLogOut")

    async def link_wallet_to_account(self, action: str = "link") -> str:
        """Answer Fragment's "Link Wallet" popup (``linkWallet`` / ``checkWallet``).

        IRREVERSIBLE: the connected wallet stays bound to the Telegram account
        for good. Only call this after the account owner explicitly agreed.
        Returns Fragment's message.
        """
        method = {"link": "linkWallet", "check": "checkWallet"}[action]
        result = await self.call(method)
        self._raise_error(result, method)
        return parser.text(str(result.get("message") or "ok"))


# ------------------------------------------------------------------ helpers
def _form_value(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def _check_stars(quantity: int) -> None:
    if not isinstance(quantity, int) or not STARS_MIN <= quantity <= STARS_MAX:
        raise InvalidAmountError(f"stars quantity must be an integer in [{STARS_MIN}, {STARS_MAX}]")


def _check_months(months: int) -> None:
    if months not in PREMIUM_MONTHS:
        raise InvalidAmountError(f"premium duration must be one of {PREMIUM_MONTHS} months")


def _recipient(found: dict, username: str | None) -> Recipient:
    return Recipient(
        recipient=str(found["recipient"]),
        name=parser.text(str(found.get("name") or username or "")),
        username=username,
        myself=bool(found.get("myself")),
        photo_html=found.get("photo"),
    )


def _purchase(result: dict, kind: str, quantity: int, rid: str, method: str,
              payment_method: str = "ton") -> PurchaseRequest:
    if not result.get("req_id"):
        raise FragmentError(f"{method}: no req_id in response", method=method, response=result)
    amount = parser.number(str(result.get("amount", "")))
    if amount is None:
        raise FragmentError(f"{method}: no amount in response", method=method, response=result)
    return PurchaseRequest(
        req_id=str(result["req_id"]),
        amount_ton=amount,
        kind=kind,
        quantity=quantity,
        recipient=rid,
        myself=bool(result.get("myself")),
        to_bot=bool(result.get("to_bot")),
        item_title=parser.strip_html(result.get("item_title")),
        raw=result,
        payment_method=payment_method,
    )
