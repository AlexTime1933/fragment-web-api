"""TON account addresses (raw ``wc:hex`` and 48-char user-friendly forms)."""

from __future__ import annotations

import base64
import binascii

from .crc import crc16

_BOUNCEABLE_TAG = 0x11
_NON_BOUNCEABLE_TAG = 0x51
_TEST_FLAG = 0x80


class AddressError(ValueError):
    pass


class Address:
    """A standard (``addr_std``) TON address.

    ``bounceable`` / ``test_only`` are only presentation hints taken from the
    user-friendly form; equality compares workchain and account hash only.
    """

    __slots__ = ("workchain", "hash_part", "bounceable", "test_only")

    def __init__(
        self,
        workchain: int,
        hash_part: bytes,
        *,
        bounceable: bool = True,
        test_only: bool = False,
    ) -> None:
        if len(hash_part) != 32:
            raise AddressError("address hash must be 32 bytes")
        if not -128 <= workchain <= 127:
            raise AddressError("workchain out of int8 range")
        self.workchain = workchain
        self.hash_part = bytes(hash_part)
        self.bounceable = bounceable
        self.test_only = test_only

    # ------------------------------------------------------------------ parse
    @classmethod
    def parse(cls, value: "str | Address") -> "Address":
        if isinstance(value, Address):
            return value
        if not isinstance(value, str):
            raise AddressError(f"unsupported address type: {type(value)!r}")
        value = value.strip()
        if ":" in value:
            return cls._parse_raw(value)
        return cls._parse_friendly(value)

    @classmethod
    def _parse_raw(cls, value: str) -> "Address":
        wc_str, _, hex_str = value.partition(":")
        try:
            wc = int(wc_str)
            hash_part = bytes.fromhex(hex_str)
        except ValueError as exc:
            raise AddressError(f"invalid raw address: {value!r}") from exc
        # Raw form carries no flags; wallets treat it as bounceable.
        return cls(wc, hash_part, bounceable=True)

    @classmethod
    def _parse_friendly(cls, value: str) -> "Address":
        if len(value) != 48:
            raise AddressError(f"invalid user-friendly address length: {value!r}")
        try:
            data = base64.b64decode(value.replace("-", "+").replace("_", "/"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AddressError(f"invalid base64 in address: {value!r}") from exc
        if len(data) != 36:
            raise AddressError(f"invalid user-friendly address: {value!r}")
        if crc16(data[:34]).to_bytes(2, "big") != data[34:]:
            raise AddressError(f"address checksum mismatch: {value!r}")
        tag = data[0]
        test_only = bool(tag & _TEST_FLAG)
        tag &= ~_TEST_FLAG
        if tag == _BOUNCEABLE_TAG:
            bounceable = True
        elif tag == _NON_BOUNCEABLE_TAG:
            bounceable = False
        else:
            raise AddressError(f"unknown address tag 0x{tag:02x}: {value!r}")
        wc = int.from_bytes(data[1:2], "big", signed=True)
        return cls(wc, data[2:34], bounceable=bounceable, test_only=test_only)

    @staticmethod
    def is_valid(value: str) -> bool:
        try:
            Address.parse(value)
        except AddressError:
            return False
        return True

    # ----------------------------------------------------------------- format
    def to_raw(self) -> str:
        return f"{self.workchain}:{self.hash_part.hex()}"

    def to_str(
        self,
        *,
        user_friendly: bool = True,
        url_safe: bool = True,
        bounceable: bool | None = None,
        test_only: bool | None = None,
    ) -> str:
        if not user_friendly:
            return self.to_raw()
        b = self.bounceable if bounceable is None else bounceable
        t = self.test_only if test_only is None else test_only
        tag = _BOUNCEABLE_TAG if b else _NON_BOUNCEABLE_TAG
        if t:
            tag |= _TEST_FLAG
        data = bytes([tag]) + self.workchain.to_bytes(1, "big", signed=True) + self.hash_part
        data += crc16(data).to_bytes(2, "big")
        text = base64.b64encode(data).decode()
        if url_safe:
            text = text.replace("+", "-").replace("/", "_")
        return text

    def __str__(self) -> str:
        return self.to_str()

    def __repr__(self) -> str:
        return f"Address({self.to_str()!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            try:
                other = Address.parse(other)
            except AddressError:
                return False
        if not isinstance(other, Address):
            return NotImplemented
        return self.workchain == other.workchain and self.hash_part == other.hash_part

    def __hash__(self) -> int:
        return hash((self.workchain, self.hash_part))
