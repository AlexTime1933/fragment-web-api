"""fragment_api — buy Telegram Stars and Premium on fragment.com, paid from your own TON wallet."""

from .exceptions import (
    AuthenticationError,
    ConfigError,
    FragmentAPIError,
    FragmentError,
    InsufficientBalanceError,
    InvalidAmountError,
    RecipientNotFoundError,
    TonApiError,
    TonError,
    TransactionFailedError,
    TransactionSafetyError,
    TransactionTimeoutError,
    VerificationRequiredError,
    WalletLinkRequiredError,
    WalletNotConnectedError,
)
from .fragment import (
    FragmentClient,
    PremiumPrice,
    PurchaseRequest,
    PurchaseResult,
    Recipient,
    StarsPrice,
    TonConnectTransaction,
)
from .service import FRAGMENT_PAYMENT_ADDRESSES, PAY_TON, PAY_USDT_TON, PAYMENT_METHODS, Fragment, SafetyPolicy
from .sync import FragmentSync
from .ton.jetton import USDT_MASTER
from .ton.tonapi import TonApiClient
from .ton.wallets import WalletV4R2, WalletV5R1

__version__ = "0.1.0"

__all__ = [
    "AuthenticationError",
    "ConfigError",
    "FRAGMENT_PAYMENT_ADDRESSES",
    "Fragment",
    "FragmentAPIError",
    "FragmentClient",
    "FragmentError",
    "FragmentSync",
    "InsufficientBalanceError",
    "InvalidAmountError",
    "PAYMENT_METHODS",
    "PAY_TON",
    "PAY_USDT_TON",
    "PremiumPrice",
    "PurchaseRequest",
    "PurchaseResult",
    "Recipient",
    "RecipientNotFoundError",
    "SafetyPolicy",
    "StarsPrice",
    "TonApiClient",
    "TonApiError",
    "TonConnectTransaction",
    "TonError",
    "TransactionFailedError",
    "TransactionSafetyError",
    "TransactionTimeoutError",
    "USDT_MASTER",
    "VerificationRequiredError",
    "WalletLinkRequiredError",
    "WalletNotConnectedError",
    "WalletV4R2",
    "WalletV5R1",
    "__version__",
]
