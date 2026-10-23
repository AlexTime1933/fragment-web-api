"""Exception hierarchy of the library."""

from __future__ import annotations

from typing import Any


class FragmentAPIError(Exception):
    """Base class for all library errors."""


class ConfigError(FragmentAPIError):
    """Missing or invalid configuration (.env, cookies, seed)."""


# ----------------------------------------------------------------- fragment
class FragmentError(FragmentAPIError):
    """Fragment returned an error or an unexpected response."""

    def __init__(self, message: str, *, method: str | None = None, response: Any = None) -> None:
        super().__init__(message)
        self.method = method
        self.response = response


class AuthenticationError(FragmentError):
    """Cookies are missing, expired or the session is not logged in."""


class WalletNotConnectedError(FragmentError):
    """No TON wallet is linked to the Fragment session (``need_ton``)."""


class VerificationRequiredError(FragmentError):
    """Fragment requires KYC / extra verification before this purchase (``need_verify``)."""


class WalletLinkRequiredError(VerificationRequiredError):
    """Fragment asks to permanently link the connected wallet to the Telegram account.

    This is a one-time, irreversible account action ("Once linked, the wallet
    cannot be removed from the account"). Do it in the browser, or call
    :meth:`FragmentClient.link_wallet_to_account` after an explicit decision.
    ``action`` is ``"link"`` (``linkWallet``) or ``"check"`` (``checkWallet``).
    """

    def __init__(self, message: str, *, action: str, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.action = action


class RecipientNotFoundError(FragmentError):
    """Username not found or cannot receive the item."""


class InvalidAmountError(FragmentError):
    """Stars quantity / premium duration rejected by Fragment."""


# ---------------------------------------------------------------------- ton
class TonError(FragmentAPIError):
    """Errors talking to the TON network."""


class TonApiError(TonError):
    def __init__(self, message: str, *, status: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class InsufficientBalanceError(TonError):
    """Not enough TON (or jetton, e.g. USDT) on the wallet. Amounts are in the smallest units."""

    def __init__(self, required: int, available: int, *, currency: str = "TON", decimals: int = 9) -> None:
        unit = 10 ** decimals
        super().__init__(
            f"insufficient wallet balance: need {required / unit:.4f} {currency}, "
            f"have {available / unit:.4f} {currency}"
        )
        self.required = required
        self.available = available
        self.currency = currency


class TransactionSafetyError(TonError):
    """A transaction built by Fragment failed a local safety check and was NOT signed."""


class TransactionTimeoutError(TonError):
    """The message was sent but no confirmation was seen in time.

    The payment may still go through: check ``message_hash`` before retrying,
    otherwise you risk paying twice.
    """

    def __init__(self, message: str, *, message_hash: str) -> None:
        super().__init__(message)
        self.message_hash = message_hash


class TransactionFailedError(TonError):
    def __init__(self, message: str, *, message_hash: str, transaction: Any = None) -> None:
        super().__init__(message)
        self.message_hash = message_hash
        self.transaction = transaction
