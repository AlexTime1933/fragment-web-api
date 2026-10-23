"""Ed25519 keys backed by PyCA ``cryptography``."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class KeyPair:
    """Holds the private key; ``repr`` never reveals secret material."""

    __slots__ = ("_private", "public_key")

    def __init__(self, private: Ed25519PrivateKey) -> None:
        self._private = private
        self.public_key: bytes = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @classmethod
    def from_seed(cls, seed32: bytes) -> "KeyPair":
        if len(seed32) != 32:
            raise ValueError("Ed25519 seed must be 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(seed32))

    def sign(self, data: bytes) -> bytes:
        return self._private.sign(data)

    def __repr__(self) -> str:
        return f"KeyPair(public_key={self.public_key.hex()})"


def verify(public_key: bytes, signature: bytes, data: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature

    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, data)
    except InvalidSignature:
        return False
    return True
