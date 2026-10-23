"""TON Connect objects a wallet shows to a dApp: ``account``, ``device`` and ``ton_proof``.

Spec: https://github.com/ton-blockchain/ton-connect/blob/main/requests-responses.md
"""

from __future__ import annotations

import base64
import hashlib
import time

from .wallets import MAINNET_GLOBAL_ID, Wallet

DEFAULT_DEVICE = {
    "platform": "windows",
    "appName": "fragment_api",
    "appVersion": "0.1.0",
    "maxProtocolVersion": 2,
    "features": ["SendTransaction", {"name": "SendTransaction", "maxMessages": 4}],
}


def account_info(wallet: Wallet, network_global_id: int = MAINNET_GLOBAL_ID) -> dict:
    return {
        "address": wallet.address.to_raw(),
        "chain": str(network_global_id),
        "walletStateInit": wallet.state_init.to_boc_b64(),
        "publicKey": wallet.public_key.hex(),
    }


def device_info(**overrides) -> dict:
    device = dict(DEFAULT_DEVICE)
    device.update(overrides)
    return device


def ton_proof_message(wallet: Wallet, domain: str, timestamp: int, payload: str) -> bytes:
    addr = wallet.address
    domain_b = domain.encode()
    return (
        b"ton-proof-item-v2/"
        + addr.workchain.to_bytes(4, "big", signed=True)
        + addr.hash_part
        + len(domain_b).to_bytes(4, "little")
        + domain_b
        + timestamp.to_bytes(8, "little")
        + payload.encode()
    )


def ton_proof(wallet: Wallet, payload: str, domain: str = "fragment.com", timestamp: int | None = None) -> dict:
    """Sign a ``ton_proof`` the way a TON Connect wallet does."""
    ts = int(time.time()) if timestamp is None else timestamp
    msg = ton_proof_message(wallet, domain, ts, payload)
    to_sign = hashlib.sha256(b"\xff\xff" + b"ton-connect" + hashlib.sha256(msg).digest()).digest()
    signature = wallet.keypair.sign(to_sign)
    return {
        "timestamp": ts,
        "domain": {"lengthBytes": len(domain.encode()), "value": domain},
        "signature": base64.b64encode(signature).decode(),
        "payload": payload,
        "stateInit": wallet.state_init.to_boc_b64(),
    }
