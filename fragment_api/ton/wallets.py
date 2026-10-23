"""Wallet contracts: W5 (V5R1, Tonkeeper default) and V4R2.

The contract code is taken verbatim from the official TypeScript SDK
(github.com/ton-org/ton, src/wallets) and pinned by its code hash, so a
corrupted constant is detected at import time.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import ClassVar, Sequence

from .address import Address
from .cell import Builder, Cell
from .keys import KeyPair
from .messages import (
    SEND_MODE_IGNORE_ERRORS,
    InternalMessage,
    contract_address,
    external_message,
    normalized_external_hash,
    state_init_cell,
)
from . import mnemonic as _mnemonic

MAINNET_GLOBAL_ID = -239
TESTNET_GLOBAL_ID = -3

_V5R1_CODE_HEX = (
    "b5ee9c7241021401000281000114ff00f4a413f4bcf2c80b01020120020d020148030402dcd020d749c120915b8f6320d70b1f20"
    "82106578746ebd21821073696e74bdb0925f03e082106578746eba8eb48020d72101d074d721fa4030fa44f828fa443058bd915b"
    "e0ed44d0810141d721f4058307f40e6fa1319130e18040d721707fdb3ce03120d749810280b99130e070e2100f02012005"
    "0c020120060902016e07080019adce76a2684020eb90eb85ffc00019af1df6a2684010eb90eb858fc00201480a0b0017b325fb"
    "51341c75c875c2c7e00011b262fb513435c280200019be5f0f6a2684080a0eb90fa02c0102f20e011e20d70b1f82107369676eba"
    "f2e08a7f0f01e68ef0eda2edfb218308d722028308d723208020d721d31fd31fd31fed44d0d200d31f20d31fd3ffd70a000af901"
    "40ccf9109a28945f0adb31e1f2c087df02b35007b0f2d0845125baf2e0855036baf2e086f823bbf2d0882292f800de01a47fc8ca"
    "00cb1f01cf16c9ed542092f80fde70db3cd81003f6eda2edfb02f404216e926c218e4c0221d73930709421c700b38e2d01d72820"
    "761e436c20d749c008f2e09320d74ac002f2e09320d71d06c712c2005230b0f2d089d74cd7393001a4e86c128407bbf2e093d74a"
    "c000f2e093ed55e2d20001c000915be0ebd72c08142091709601d72c081c12e25210b1e30f20d74a111213009601fa4001fa44"
    "f828fa443058baf2e091ed44d0810141d718f405049d7fc8ca0040048307f453f2e08b8e14038307f45bf2e08c22d70a00216e01"
    "b3b0f2d090e2c85003cf1612f400c9ed54007230d72c08248e2d21f2e092d200ed44d0d2005113baf2d08f54503091319c018101"
    "40d721d70a00f2e08ee2c8ca0058cf16c9ed5493f2c08de20010935bdb31e1d74cd0b4d6c35e"
)
_V5R1_CODE_HASH = "20834b7b72b112147e1b2fb457b84e74d1a30f04f737d4f62a668e9552d2b72f"

_V4R2_CODE_B64 = (
    "te6ccgECFAEAAtQAART/APSkE/S88sgLAQIBIAIDAgFIBAUE+PKDCNcYINMf0x/THwL4I7vyZO1E0NMf0x/T//QE0VFDuvKhUVG68qIF"
    "+QFUEGT5EPKj+AAkpMjLH1JAyx9SMMv/UhD0AMntVPgPAdMHIcAAn2xRkyDXSpbTB9QC+wDoMOAhwAHjACHAAuMAAcADkTDjDQOkyMsf"
    "Essfy/8QERITAubQAdDTAyFxsJJfBOAi10nBIJJfBOAC0x8hghBwbHVnvSKCEGRzdHK9sJJfBeAD+kAwIPpEAcjKB8v/ydDtRNCBAUDX"
    "IfQEMFyBAQj0Cm+hMbOSXwfgBdM/yCWCEHBsdWe6kjgw4w0DghBkc3RyupJfBuMNBgcCASAICQB4AfoA9AQw+CdvIjBQCqEhvvLgUIIQ"
    "cGx1Z4MesXCAGFAEywUmzxZY+gIZ9ADLaRfLH1Jgyz8gyYBA+wAGAIpQBIEBCPRZMO1E0IEBQNcgyAHPFvQAye1UAXKwjiOCEGRzdHKD"
    "HrFwgBhQBcsFUAPPFiP6AhPLassfyz/JgED7AJJfA+ICASAKCwBZvSQrb2omhAgKBrkPoCGEcNQICEekk30pkQzmkD6f+YN4EoAbeBAU"
    "iYcVnzGEAgFYDA0AEbjJftRNDXCx+AA9sp37UTQgQFA1yH0BDACyMoHy//J0AGBAQj0Cm+hMYAIBIA4PABmtznaiaEAga5Drhf/AABmv"
    "HfaiaEAQa5DrhY/AAG7SB/oA1NQi+QAFyMoHFcv/ydB3dIAYyMsFywIizxZQBfoCFMtrEszMyXP7AMhAFIEBCPRR8qcCAHCBAQjXGPoA"
    "0z/IVCBHgQEI9FHyp4IQbm90ZXB0gBjIywXLAlAGzxZQBPoCFMtqEssfyz/Jc/sAAgBsgQEI1xj6ANM/MFIkgQEI9Fnyp4IQZHN0cnB0"
    "gBjIywXLAlAFzxZQA/oCE8tqyx8Syz/Jc/sAAAr0AMntVA=="
)
_V4R2_CODE_HASH = "feb5ff6820e2ff0d9483e7e0d62c817d846789fb4ae580c878866d959dabd5c0"


def _load_code(boc: bytes, expected_hash: str, name: str) -> Cell:
    cell = Cell.from_boc(boc)
    if cell.hash().hex() != expected_hash:
        raise RuntimeError(f"{name} wallet code hash mismatch; refusing to continue")
    return cell


V5R1_CODE = _load_code(bytes.fromhex(_V5R1_CODE_HEX), _V5R1_CODE_HASH, "V5R1")
V4R2_CODE = _load_code(base64.b64decode(_V4R2_CODE_B64), _V4R2_CODE_HASH, "V4R2")

WALLET_CODE_HASHES = {_V5R1_CODE_HASH: "v5r1", _V4R2_CODE_HASH: "v4r2"}

_OP_ACTION_SEND_MSG = 0x0EC3C86D
_OP_V5_SIGNED_EXTERNAL = 0x7369676E  # "sign"


@dataclass(frozen=True)
class SignedTransfer:
    """A signed external message ready to be sent to the network."""

    cell: Cell
    seqno: int
    valid_until: int
    wallet_address: Address
    normalized_hash: bytes

    @property
    def boc(self) -> bytes:
        return self.cell.to_boc()

    @property
    def boc_b64(self) -> str:
        return self.cell.to_boc_b64()

    @property
    def hash_hex(self) -> str:
        """Normalized external message hash (hex) to look the transaction up."""
        return self.normalized_hash.hex()


class Wallet:
    VERSION: ClassVar[str] = ""
    MAX_MESSAGES: ClassVar[int] = 4

    def __init__(self, public_key: bytes, *, keypair: KeyPair | None = None, workchain: int = 0) -> None:
        if len(public_key) != 32:
            raise ValueError("public key must be 32 bytes")
        self.public_key = public_key
        self._keypair = keypair
        self.workchain = workchain

    @classmethod
    def from_mnemonic(cls, mnemonic: "str | list[str]", password: str = "", *, kind: str = "auto",
                      **kwargs) -> "Wallet":
        """``kind``: ``"auto"``, ``"ton"`` (24-word TON) or ``"bip39"`` (12/24-word BIP39)."""
        kp = _mnemonic.to_keypair(mnemonic, password, kind=kind)
        return cls(kp.public_key, keypair=kp, **kwargs)

    @property
    def keypair(self) -> KeyPair:
        if self._keypair is None:
            raise RuntimeError("wallet was created without a private key")
        return self._keypair

    @property
    def can_sign(self) -> bool:
        return self._keypair is not None

    @property
    def code(self) -> Cell:
        raise NotImplementedError

    def initial_data(self) -> Cell:
        raise NotImplementedError

    @property
    def state_init(self) -> Cell:
        return state_init_cell(self.code, self.initial_data())

    @property
    def address(self) -> Address:
        addr = contract_address(self.code, self.initial_data(), self.workchain)
        # Wallets are shown non-bounceable by Tonkeeper (UQ...).
        return Address(addr.workchain, addr.hash_part, bounceable=False)

    def _signing_payload(self, messages: Sequence[InternalMessage], seqno: int, valid_until: int) -> Builder:
        raise NotImplementedError

    def _pack(self, signature: bytes, payload: Builder) -> Cell:
        raise NotImplementedError

    def create_transfer(
        self,
        messages: Sequence[InternalMessage],
        *,
        seqno: int,
        valid_until: int | None = None,
        timeout: int = 60,
        deploy: bool | None = None,
    ) -> SignedTransfer:
        """Build and sign an external message carrying ``messages``.

        ``deploy`` attaches the state init (needed when the wallet is not yet
        deployed); by default it is attached when ``seqno == 0``.
        """
        if not messages:
            raise ValueError("at least one message is required")
        if len(messages) > self.MAX_MESSAGES:
            raise ValueError(f"{self.VERSION} supports at most {self.MAX_MESSAGES} messages")
        now = int(time.time())
        if valid_until is None:
            valid_until = now + timeout
        if valid_until <= now:
            raise ValueError("valid_until is in the past")
        payload = self._signing_payload(messages, seqno, valid_until)
        signature = self.keypair.sign(payload.end_cell().hash())
        body = self._pack(signature, payload)
        if deploy is None:
            deploy = seqno == 0
        addr = self.address
        cell = external_message(addr, body, self.state_init if deploy else None)
        return SignedTransfer(
            cell=cell,
            seqno=seqno,
            valid_until=valid_until,
            wallet_address=addr,
            normalized_hash=normalized_external_hash(addr, body),
        )


def _out_list(messages: Sequence[InternalMessage], *, force_ignore_errors: bool) -> Cell:
    """``OutList`` as used in c5 / W5: a chain of ``action_send_msg``."""
    cell = Cell()
    for m in messages:
        mode = m.send_mode | (SEND_MODE_IGNORE_ERRORS if force_ignore_errors else 0)
        cell = (
            Builder()
            .store_ref(cell)
            .store_uint(_OP_ACTION_SEND_MSG, 32)
            .store_uint(mode, 8)
            .store_ref(m.to_cell())
            .end_cell()
        )
    return cell


class WalletV5R1(Wallet):
    """W5 — the default wallet of current Tonkeeper / Telegram wallet apps."""

    VERSION = "v5r1"
    MAX_MESSAGES = 255

    def __init__(
        self,
        public_key: bytes,
        *,
        keypair: KeyPair | None = None,
        workchain: int = 0,
        network_global_id: int = MAINNET_GLOBAL_ID,
        subwallet_number: int = 0,
    ) -> None:
        super().__init__(public_key, keypair=keypair, workchain=workchain)
        self.network_global_id = network_global_id
        self.subwallet_number = subwallet_number

    @property
    def code(self) -> Cell:
        return V5R1_CODE

    @property
    def wallet_id(self) -> int:
        """``wallet_id`` as uint32: client context XOR network global id."""
        context = (1 << 31) | ((self.workchain & 0xFF) << 23) | (0 << 15) | (self.subwallet_number & 0x7FFF)
        return (context ^ self.network_global_id) & 0xFFFFFFFF

    def initial_data(self) -> Cell:
        return (
            Builder()
            .store_bit(1)  # signature auth allowed
            .store_uint(0, 32)  # seqno
            .store_uint(self.wallet_id, 32)
            .store_bytes(self.public_key)
            .store_bit(0)  # extensions dict: empty
            .end_cell()
        )

    def _signing_payload(self, messages, seqno, valid_until) -> Builder:
        return (
            Builder()
            .store_uint(_OP_V5_SIGNED_EXTERNAL, 32)
            .store_uint(self.wallet_id, 32)
            .store_uint(valid_until, 32)
            .store_uint(seqno, 32)
            # External messages must not throw on action phase, see toSafeV5R1SendMode.
            .store_maybe_ref(_out_list(messages, force_ignore_errors=True))
            .store_bit(0)  # no extended actions
        )

    def _pack(self, signature: bytes, payload: Builder) -> Cell:
        return Builder().store_builder(payload).store_bytes(signature).end_cell()


class WalletV4R2(Wallet):
    VERSION = "v4r2"
    MAX_MESSAGES = 4

    def __init__(self, public_key: bytes, *, keypair: KeyPair | None = None, workchain: int = 0,
                 wallet_id: int | None = None) -> None:
        super().__init__(public_key, keypair=keypair, workchain=workchain)
        self.wallet_id = 698983191 + workchain if wallet_id is None else wallet_id

    @property
    def code(self) -> Cell:
        return V4R2_CODE

    def initial_data(self) -> Cell:
        return (
            Builder()
            .store_uint(0, 32)
            .store_uint(self.wallet_id, 32)
            .store_bytes(self.public_key)
            .store_bit(0)  # plugins dict: empty
            .end_cell()
        )

    def _signing_payload(self, messages, seqno, valid_until) -> Builder:
        b = (
            Builder()
            .store_uint(self.wallet_id, 32)
            .store_uint(valid_until, 32)
            .store_uint(seqno, 32)
            .store_uint(0, 8)  # op: simple send
        )
        for m in messages:
            b.store_uint(m.send_mode, 8).store_ref(m.to_cell())
        return b

    def _pack(self, signature: bytes, payload: Builder) -> Cell:
        return Builder().store_bytes(signature).store_builder(payload).end_cell()


WALLET_CLASSES: dict[str, type[Wallet]] = {"v5r1": WalletV5R1, "w5": WalletV5R1, "v4r2": WalletV4R2, "v4": WalletV4R2}


def wallet_class(version: str) -> type[Wallet]:
    try:
        return WALLET_CLASSES[version.lower()]
    except KeyError:
        raise ValueError(f"unsupported wallet version {version!r}; use one of v5r1, v4r2") from None
