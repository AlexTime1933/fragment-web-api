"""High-level API: prices, buying Stars and gifting Premium, paid from your TON wallet.

Flow of one purchase (the same steps the Fragment site + Tonkeeper perform):

1. ``search*Recipient``   – resolve @username to Fragment's recipient id
2. ``init*Request``       – create an order, Fragment returns ``req_id`` and price
3. ``get*Link``           – Fragment returns a TON Connect transaction
4. local safety checks    – destination, amount, limits, balance
5. sign with the seed     – W5 / V4R2 external message, sent through tonapi
6. wait for the chain     – the wallet transaction must succeed
7. ``confirm_method``     – report the BoC back to Fragment (if it asks for it)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Mapping

from .exceptions import (
    ConfigError,
    FragmentError,
    InsufficientBalanceError,
    InvalidAmountError,
    TonApiError,
    TransactionSafetyError,
    TransactionTimeoutError,
)
from .fragment.client import PREMIUM_MONTHS, FragmentClient
from .fragment.models import (
    PremiumPrice,
    PurchaseRequest,
    PurchaseResult,
    Recipient,
    StarsPrice,
    TonConnectTransaction,
    from_nano,
    to_nano,
)
from .ton import tonconnect
from .ton.address import Address
from .ton.jetton import USDT_DECIMALS, USDT_MASTER, JettonTransfer, from_units, parse_transfer, to_units
from .ton.messages import SEND_MODE_DEFAULT, InternalMessage
from .ton.tonapi import MAINNET_URL, TESTNET_URL, TonApiClient
from .ton.wallets import MAINNET_GLOBAL_ID, TESTNET_GLOBAL_ID, Wallet, WalletV5R1, wallet_class

log = logging.getLogger(__name__)

# Fragment's payment wallets, both named "Fragment" in tonapi and receiving
# the corresponding payments from many senders (checked on-chain 2026-10-22).
FRAGMENT_PAYMENT_ADDRESSES = (
    "EQBAjaOyi2wGWlk-EDkSabqqnF-MrrwMadnwqrurKpkla9nE",  # Premium
    "UQCFJEP4WZ_mpdo0_kMEmsTgvrMHG7K_tWY16pQhKHwoOtFz",  # Stars
)

#: How Fragment is paid: TON, or USDT on TON (a jetton transfer from our USDT wallet).
PAY_TON = "ton"
PAY_USDT_TON = "usdt_ton"
PAYMENT_METHODS = (PAY_TON, PAY_USDT_TON)


@dataclass
class SafetyPolicy:
    """Checks applied to every transaction Fragment asks us to sign.

    A failed check raises :class:`TransactionSafetyError` *before* signing.
    """

    #: Destinations Fragment may ask to pay. ``None`` disables the check.
    allowed_destinations: list[str] | None = field(default_factory=lambda: list(FRAGMENT_PAYMENT_ADDRESSES))
    #: Hard cap per purchase, in TON. ``None`` disables the cap.
    max_amount_ton: Decimal | None = Decimal("100")
    #: Allowed excess of the transaction total over the price quoted by ``init*Request``.
    amount_tolerance: Decimal = Decimal("0.02")  # 2 %
    amount_tolerance_abs_ton: Decimal = Decimal("0.05")
    #: Network fees to keep on the wallet in addition to the transfer.
    fee_reserve_ton: Decimal = Decimal("0.05")
    max_messages: int = 4
    #: Require the wallet linked to the Fragment session to be our wallet.
    require_session_wallet_match: bool = True
    # --- USDT on TON
    #: USDT jetton master (mainnet Tether USD); our USDT wallet is derived from it on-chain.
    usdt_master: str = USDT_MASTER
    #: Hard cap per purchase, in USDT. ``None`` disables the cap.
    max_amount_usdt: Decimal | None = Decimal("100")
    #: Allowed excess of the USDT amount over Fragment's USD price.
    amount_tolerance_abs_usdt: Decimal = Decimal("0.05")
    #: Max TON attached to the jetton transfer for gas (Fragment asks 0.05; the rest comes back).
    max_jetton_gas_ton: Decimal = Decimal("0.1")
    #: Max TON forwarded to the recipient with the jetton notification.
    max_forward_ton: Decimal = Decimal("0.01")

    def check(self, tx: TonConnectTransaction, request: PurchaseRequest) -> None:
        if not tx.messages:
            raise TransactionSafetyError("Fragment transaction has no messages")
        if len(tx.messages) > self.max_messages:
            raise TransactionSafetyError(f"too many messages in transaction: {len(tx.messages)}")
        if self.allowed_destinations is not None:
            allowed = {Address.parse(a) for a in self.allowed_destinations}
            for m in tx.messages:
                if m.address not in allowed:
                    raise TransactionSafetyError(
                        f"unexpected payment destination {m.address.to_str()} (not in allowed_destinations)"
                    )
        for m in tx.messages:
            if m.state_init is not None:
                raise TransactionSafetyError("transaction tries to deploy a contract (stateInit present)")
            if m.amount <= 0:
                raise TransactionSafetyError("message with non-positive amount")
        total = tx.total_amount
        quoted = to_nano(request.amount_ton)
        limit = quoted + to_nano(request.amount_ton * self.amount_tolerance) + to_nano(self.amount_tolerance_abs_ton)
        if total > limit:
            raise TransactionSafetyError(
                f"transaction total {from_nano(total)} TON exceeds quoted price {request.amount_ton} TON"
            )
        if self.max_amount_ton is not None and total > to_nano(self.max_amount_ton):
            raise TransactionSafetyError(
                f"transaction total {from_nano(total)} TON exceeds max_amount_ton={self.max_amount_ton}"
            )

    def check_usdt(self, tx: TonConnectTransaction, request: PurchaseRequest, *, owner: Address,
                   jetton_wallet: Address) -> JettonTransfer:
        """Checks for a USDT-on-TON payment; returns the decoded jetton transfer.

        The only allowed shape: ONE message to OUR OWN USDT jetton wallet, with a
        little TON for gas and a ``transfer`` payload sending at most the quoted USD
        price in USDT to a Fragment address, excess returned to our wallet.
        """
        if len(tx.messages) != 1:
            raise TransactionSafetyError(f"USDT payment must be one message, got {len(tx.messages)}")
        m = tx.messages[0]
        if m.address != jetton_wallet:
            raise TransactionSafetyError(
                f"USDT payment goes to {m.address.to_str()}, not to our USDT wallet {jetton_wallet.to_str()}"
            )
        if m.state_init is not None:
            raise TransactionSafetyError("transaction tries to deploy a contract (stateInit present)")
        if not 0 < m.amount <= to_nano(self.max_jetton_gas_ton):
            raise TransactionSafetyError(
                f"USDT payment attaches {from_nano(m.amount)} TON for gas (max {self.max_jetton_gas_ton})"
            )
        if m.payload is None:
            raise TransactionSafetyError("USDT payment has no jetton transfer payload")
        try:
            transfer = parse_transfer(m.payload)
        except ValueError as exc:
            raise TransactionSafetyError(f"USDT payment payload: {exc}") from None
        if self.allowed_destinations is not None:
            allowed = {Address.parse(a) for a in self.allowed_destinations}
            if transfer.destination not in allowed:
                raise TransactionSafetyError(
                    f"unexpected USDT destination {transfer.destination.to_str()} (not in allowed_destinations)"
                )
        if transfer.response_destination != owner:
            raise TransactionSafetyError("USDT transfer returns the excess to a foreign address")
        if transfer.has_custom_payload:
            raise TransactionSafetyError("USDT transfer has a custom payload")
        if transfer.forward_ton_amount > to_nano(self.max_forward_ton) or transfer.forward_ton_amount >= m.amount:
            raise TransactionSafetyError(f"USDT transfer forwards {from_nano(transfer.forward_ton_amount)} TON")
        if request.amount_usd is None:
            raise TransactionSafetyError("no USD price for this purchase; cannot check the USDT amount")
        limit = to_units(request.amount_usd * (1 + self.amount_tolerance) + self.amount_tolerance_abs_usdt)
        if transfer.amount <= 0 or transfer.amount > limit:
            raise TransactionSafetyError(
                f"USDT amount {from_units(transfer.amount)} does not match the price {request.amount_usd} USD"
            )
        if self.max_amount_usdt is not None and transfer.amount > to_units(self.max_amount_usdt):
            raise TransactionSafetyError(
                f"USDT amount {from_units(transfer.amount)} exceeds max_amount_usdt={self.max_amount_usdt}"
            )
        return transfer


class Fragment:
    """Buy Telegram Stars and Premium on fragment.com for any Telegram user.

    The library never reads or writes files or environment variables: the
    caller (bot, site) passes cookies, seed phrase and API key as values.

    >>> async with Fragment.create(cookies=my_cookies, mnemonic=my_seed, tonapi_key=my_key) as f:
    ...     print(await f.get_stars_price(100))
    ...     result = await f.buy_stars("durov", 100)
    """

    def __init__(
        self,
        client: FragmentClient,
        *,
        wallet: Wallet | None = None,
        tonapi: TonApiClient | None = None,
        safety: SafetyPolicy | None = None,
        network_global_id: int = MAINNET_GLOBAL_ID,
        device: dict | None = None,
        confirm_timeout: float = 180.0,
    ) -> None:
        self.client = client
        self.wallet = wallet
        self.tonapi = tonapi
        self.safety = safety or SafetyPolicy()
        self.network_global_id = network_global_id
        self.device = device or tonconnect.device_info()
        self.confirm_timeout = confirm_timeout
        # One wallet can only have one outgoing message per seqno.
        self._pay_lock = asyncio.Lock()

    # ----------------------------------------------------------- creation
    @classmethod
    def create(
        cls,
        cookies: "Mapping[str, str] | str",
        *,
        mnemonic: "str | list[str] | None" = None,
        mnemonic_password: str = "",
        mnemonic_kind: str = "auto",
        tonapi_key: str | None = None,
        wallet_version: str = "v5r1",
        network: str = "mainnet",
        safety: SafetyPolicy | None = None,
        user_agent: str | None = None,
        **kwargs,
    ) -> "Fragment":
        """Build a ready client from plain values.

        ``cookies``: dict or ``"k=v; k=v"`` / JSON string. ``mnemonic``: seed
        phrase (24-word TON or 12/24-word BIP39). Without ``mnemonic`` and
        ``tonapi_key`` only prices and recipient lookups work.
        """
        if network not in ("mainnet", "testnet"):
            raise ConfigError(f"unknown network {network!r}")
        testnet = network == "testnet"
        global_id = TESTNET_GLOBAL_ID if testnet else MAINNET_GLOBAL_ID
        client = FragmentClient(cookies, **({"user_agent": user_agent} if user_agent else {}))
        wallet = None
        if mnemonic:
            wcls = wallet_class(wallet_version)
            extra = {"network_global_id": global_id} if wcls is WalletV5R1 else {}
            wallet = wcls.from_mnemonic(mnemonic, mnemonic_password, kind=mnemonic_kind, **extra)
        tonapi = TonApiClient(tonapi_key, base_url=TESTNET_URL if testnet else MAINNET_URL) if tonapi_key else None
        return cls(client, wallet=wallet, tonapi=tonapi, safety=safety, network_global_id=global_id, **kwargs)

    @property
    def cookies(self) -> dict[str, str]:
        """Current session cookies; Fragment may rotate them, the caller decides whether to persist them."""
        return self.client.cookies

    async def aclose(self) -> None:
        await self.client.aclose()
        if self.tonapi:
            await self.tonapi.aclose()

    async def __aenter__(self) -> "Fragment":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------- prices
    async def get_stars_price(self, quantity: int) -> StarsPrice:
        return await self.client.get_stars_price(quantity)

    async def get_stars_packages(self) -> list[StarsPrice]:
        return await self.client.get_stars_packages()

    async def get_premium_prices(self) -> list[PremiumPrice]:
        return await self.client.get_premium_prices()

    async def get_premium_price(self, months: int) -> PremiumPrice:
        if months not in PREMIUM_MONTHS:
            raise InvalidAmountError(f"premium duration must be one of {PREMIUM_MONTHS} months")
        for p in await self.get_premium_prices():
            if p.months == months:
                return p
        raise FragmentError(f"no premium option for {months} months")

    async def get_ton_rate(self) -> Decimal | None:
        return await self.client.ton_rate_usd()

    # ------------------------------------------------------------- wallet
    def _require_wallet(self) -> Wallet:
        if self.wallet is None:
            raise ConfigError("no TON wallet: pass mnemonic= to Fragment.create()")
        return self.wallet

    def _require_tonapi(self) -> TonApiClient:
        if self.tonapi is None:
            raise ConfigError("no tonapi key: pass tonapi_key= to Fragment.create()")
        return self.tonapi

    @property
    def wallet_address(self) -> str:
        return self._require_wallet().address.to_str()

    async def get_balance(self) -> Decimal:
        return from_nano(await self._require_tonapi().get_balance(self._require_wallet().address))

    async def get_usdt_balance(self) -> Decimal:
        """USDT (on TON) balance of the wallet."""
        _, units = await self._require_tonapi().get_jetton_wallet(self._require_wallet().address,
                                                                  self.safety.usdt_master)
        return from_units(units)

    async def check_session_wallet(self) -> bool:
        """``True`` if the wallet linked to the Fragment session is our wallet."""
        session = await self.client.wallet_session(refresh=True)
        if not session or not session.address:
            return False
        return Address.parse(session.address) == self._require_wallet().address

    async def link_wallet(self, *, force: bool = False) -> bool:
        """Connect our wallet to the Fragment session via ``ton_proof``.

        Normally the wallet is already linked through the browser cookies;
        this is for fresh sessions. Returns ``True`` when the wallet is linked.
        """
        wallet = self._require_wallet()
        session = await self.client.wallet_session(refresh=True)
        if session and session.address and not force:
            if Address.parse(session.address) == wallet.address:
                return True
            raise FragmentError(
                f"another wallet ({Address.parse(session.address).to_str()}) is linked to this session; "
                "call link_wallet(force=True) to replace it"
            )
        if session and session.address and force:
            await self.client.ton_logout()
            session = await self.client.wallet_session(refresh=True)
        if not session or not session.ton_proof:
            raise FragmentError("Fragment page has no ton_proof payload; cannot link wallet")
        account = tonconnect.account_info(wallet, self.network_global_id)
        proof = tonconnect.ton_proof(wallet, session.ton_proof)
        for attempt in range(8):
            result = await self.client.check_ton_proof(account, self.device, proof)
            if result.get("verified"):
                return True
            if result.get("pending"):
                await asyncio.sleep(2)
                continue
            raise FragmentError(f"wallet link rejected: {result.get('error') or result}", response=result)
        return False

    # ---------------------------------------------------------- recipients
    async def find_stars_recipient(self, username: str, quantity: int | None = None) -> Recipient:
        return await self.client.search_stars_recipient(username, quantity)

    async def find_premium_recipient(self, username: str, months: int = 3) -> Recipient:
        return await self.client.search_premium_recipient(username, months)

    # ------------------------------------------------------------ purchase
    async def buy_stars(
        self,
        username: str | None,
        quantity: int,
        *,
        show_sender: bool = False,
        dry_run: bool = False,
        payment_method: str = PAY_TON,
    ) -> PurchaseResult:
        """Buy ``quantity`` Stars for ``@username`` (``None`` = the logged-in account).

        ``payment_method``: ``"ton"`` (default) or ``"usdt_ton"`` (USDT on TON).
        """
        _check_method(payment_method)
        if username is None:
            recipient = await self.client.myself("stars")
            if recipient is None:
                raise FragmentError("cannot resolve the logged-in account as recipient")
        else:
            recipient = await self.client.search_stars_recipient(username, quantity)
        await self._preflight()
        request = await self.client.init_stars_purchase(recipient, quantity, payment_method=payment_method)
        if payment_method == PAY_USDT_TON:
            request = replace(request, amount_usd=(await self.get_stars_price(quantity)).price_usd)
        return await self._pay(request, show_sender=show_sender, dry_run=dry_run)

    async def gift_premium(
        self,
        username: str | None,
        months: int,
        *,
        show_sender: bool = False,
        dry_run: bool = False,
        payment_method: str = PAY_TON,
    ) -> PurchaseResult:
        """Gift Telegram Premium for ``months`` (3, 6 or 12) to ``@username``.

        ``payment_method``: ``"ton"`` (default) or ``"usdt_ton"`` (USDT on TON).
        """
        _check_method(payment_method)
        if username is None:
            recipient = await self.client.myself("premium")
            if recipient is None:
                raise FragmentError("cannot resolve the logged-in account as recipient")
        else:
            recipient = await self.client.search_premium_recipient(username, months)
        await self._preflight()
        request = await self.client.init_premium_purchase(recipient, months, payment_method=payment_method)
        if payment_method == PAY_USDT_TON:
            request = replace(request, amount_usd=(await self.get_premium_price(months)).price_usd)
        return await self._pay(request, show_sender=show_sender, dry_run=dry_run)

    async def _preflight(self) -> None:
        """Checks done before an order is created on Fragment."""
        wallet = self._require_wallet()
        self._require_tonapi()
        if self.safety.require_session_wallet_match:
            session = await self.client.wallet_session(refresh=True)
            if not session or not session.address:
                raise TransactionSafetyError("no wallet is linked to the Fragment session; call link_wallet()")
            if Address.parse(session.address) != wallet.address:
                raise TransactionSafetyError(
                    f"Fragment session is linked to {Address.parse(session.address).to_str(bounceable=False)}, "
                    f"but the seed belongs to {wallet.address.to_str()} (wrong seed or wallet version?)"
                )

    async def pay(self, request: PurchaseRequest, *, show_sender: bool = False, dry_run: bool = False) -> PurchaseResult:
        """Fetch, verify, sign and send the transaction for an initialized order.

        With ``dry_run=True`` the message is signed and emulated by tonapi but
        never sent.
        """
        await self._preflight()
        return await self._pay(request, show_sender=show_sender, dry_run=dry_run)

    async def _pay(self, request: PurchaseRequest, *, show_sender: bool, dry_run: bool) -> PurchaseResult:
        wallet = self._require_wallet()
        tonapi = self._require_tonapi()
        account = tonconnect.account_info(wallet, self.network_global_id)

        tx = await self.client.get_transaction(request, show_sender=show_sender, account=account, device=self.device)
        raw_tx = tx.raw.get("transaction") or {}
        network = raw_tx.get("network")
        if network is not None and str(network) != str(self.network_global_id):
            raise TransactionSafetyError(f"transaction is for network {network}, wallet is on {self.network_global_id}")
        sender = raw_tx.get("from")
        if sender and Address.parse(sender) != wallet.address:
            raise TransactionSafetyError(
                f"Fragment expects payment from {Address.parse(sender).to_str(bounceable=False)}, "
                f"not from {wallet.address.to_str()}"
            )
        usdt = request.payment_method == PAY_USDT_TON
        transfer = None
        if usdt:
            jetton_wallet, _ = await tonapi.get_jetton_wallet(wallet.address, self.safety.usdt_master)
            transfer = self.safety.check_usdt(tx, request, owner=wallet.address, jetton_wallet=jetton_wallet)
            log.info("fragment asks to pay %s USDT to %s (+%s TON gas), comment=%r",
                     from_units(transfer.amount), transfer.destination.to_str(),
                     from_nano(tx.total_amount), transfer.comment)
        else:
            self.safety.check(tx, request)
            for m in tx.messages:
                log.info("fragment asks to pay %s TON to %s, comment=%r",
                         from_nano(m.amount), m.address.to_str(), m.comment)

        async with self._pay_lock:
            seqno = await tonapi.get_seqno(wallet.address)
            balance = await tonapi.get_balance(wallet.address)
            required = tx.total_amount + to_nano(self.safety.fee_reserve_ton)
            if balance < required and not dry_run:
                raise InsufficientBalanceError(required, balance)
            if transfer is not None:
                _, usdt_balance = await tonapi.get_jetton_wallet(wallet.address, self.safety.usdt_master)
                if usdt_balance < transfer.amount and not dry_run:
                    raise InsufficientBalanceError(transfer.amount, usdt_balance, currency="USDT",
                                                   decimals=USDT_DECIMALS)

            now = int(time.time())
            valid_until = now + 120
            if tx.valid_until:
                if tx.valid_until <= now + 5:
                    raise TransactionSafetyError("Fragment transaction already expired")
                valid_until = min(valid_until, tx.valid_until)
            messages = [
                InternalMessage(
                    destination=m.address,
                    amount=m.amount,
                    body=m.payload,
                    bounce=_bounce_flag(m.raw_address),
                    send_mode=SEND_MODE_DEFAULT,
                )
                for m in tx.messages
            ]
            signed = wallet.create_transfer(messages, seqno=seqno, valid_until=valid_until)
            result_kwargs = dict(
                request=request,
                transaction=tx,
                message_hash=signed.hash_hex,
                boc=signed.boc_b64,
                wallet_address=wallet.address.to_str(),
            )
            if dry_run:
                emulation = None
                try:
                    emulation = await tonapi.emulate_wallet_message(signed.boc_b64)
                except TonApiError as exc:
                    emulation = {"error": str(exc)}
                return PurchaseResult(sent=False, extra={"emulation": emulation, "balance": balance,
                                                         **_paid_extra(transfer)}, **result_kwargs)

            log.info("sending %s from %s (seqno %s, msg %s)",
                     f"{from_units(transfer.amount)} USDT" if transfer else f"{from_nano(tx.total_amount)} TON",
                     wallet.address.to_str(), seqno, signed.hash_hex)
            try:
                await tonapi.send_boc(signed.boc_b64)
            except TonApiError as exc:
                # The message may still have reached the network: never resend blindly.
                raise TransactionTimeoutError(
                    f"sending failed ({exc}); check message {signed.hash_hex} before retrying",
                    message_hash=signed.hash_hex,
                ) from exc

            chain_tx = await tonapi.wait_for_transaction(signed.hash_hex, timeout=self.confirm_timeout)

        confirm = None
        try:
            confirm = await self.client.confirm_transaction(tx, boc=signed.boc_b64, account=account,
                                                            device=self.device)
        except FragmentError as exc:
            log.warning("confirm_method call failed (payment itself succeeded): %s", exc)
        return PurchaseResult(sent=True, confirmed=True, chain_transaction=chain_tx,
                              extra={"confirm": confirm, **_paid_extra(transfer)}, **result_kwargs)

    async def check_payment(self, message_hash: str) -> dict | None:
        """Look up the wallet transaction for a previously sent message."""
        return await self._require_tonapi().get_transaction_by_message(message_hash)


def _check_method(payment_method: str) -> None:
    if payment_method not in PAYMENT_METHODS:
        raise ConfigError(f"unknown payment_method {payment_method!r}; use one of {PAYMENT_METHODS}")


def _paid_extra(transfer: JettonTransfer | None) -> dict:
    if transfer is None:
        return {"currency": "TON"}
    return {"currency": "USDT", "amount_usdt": from_units(transfer.amount)}


def _bounce_flag(raw_address: str) -> bool:
    """TON Connect rule: bounce unless the address is given in non-bounceable form."""
    try:
        return Address.parse(raw_address).bounceable
    except ValueError:
        return True
