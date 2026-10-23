"""Seed phrase -> Ed25519 key pair. Two formats are supported:

**TON mnemonic** (24 words, classic Tonkeeper / @ton/crypto):

* entropy = HMAC-SHA512(key=" ".join(words), msg=password)
* valid phrase: PBKDF2-SHA512(entropy, "TON seed version", 390)[0] == 0
* seed = PBKDF2-SHA512(entropy, "TON default seed", 100000)[:32]

**BIP39 mnemonic** (12/24 words with a BIP39 checksum, used by newer wallets
for 12-word phrases): BIP39 seed (PBKDF2-SHA512, salt "mnemonic"+passphrase,
2048 rounds) -> SLIP-0010 Ed25519 derivation along ``m/44'/607'/0'``
(607 = TON coin type).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata
from functools import lru_cache
from pathlib import Path

from .keys import KeyPair

_WORDLIST_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
_PBKDF_ITERATIONS = 100_000
BIP39_TON_PATH = (44, 607, 0)  # m/44'/607'/0'

TON = "ton"
BIP39 = "bip39"
AUTO = "auto"


class MnemonicError(ValueError):
    pass


@lru_cache(maxsize=1)
def wordlist() -> tuple[str, ...]:
    raw = (Path(__file__).with_name("bip39_english.txt")).read_bytes()
    # The file is shipped with the package; refuse to work if it was altered.
    if hashlib.sha256(raw).hexdigest() != _WORDLIST_SHA256:
        raise MnemonicError("BIP39 wordlist checksum mismatch")
    words = tuple(raw.decode("ascii").split())
    if len(words) != 2048:
        raise MnemonicError("BIP39 wordlist must contain 2048 words")
    return words


def normalize(mnemonic: "str | list[str] | tuple[str, ...]") -> list[str]:
    if isinstance(mnemonic, str):
        words = mnemonic.replace(",", " ").split()
    else:
        words = [w for w in mnemonic]
    return [w.strip().lower() for w in words if w.strip()]


def _known_words(words: list[str]) -> bool:
    known = set(wordlist())
    return all(w in known for w in words)


# --------------------------------------------------------------------- TON
def _entropy(words: list[str], password: str = "") -> bytes:
    return hmac.new(" ".join(words).encode(), password.encode(), hashlib.sha512).digest()


def _is_basic_seed(entropy: bytes) -> bool:
    seed = hashlib.pbkdf2_hmac("sha512", entropy, b"TON seed version", max(1, _PBKDF_ITERATIONS // 256), 64)
    return seed[0] == 0


def _is_password_seed(entropy: bytes) -> bool:
    seed = hashlib.pbkdf2_hmac("sha512", entropy, b"TON fast seed version", 1, 64)
    return seed[0] == 1


def validate(mnemonic: "str | list[str]", password: str = "") -> bool:
    """``True`` for a valid *TON-format* mnemonic."""
    words = normalize(mnemonic)
    if len(words) not in (12, 18, 24) or not _known_words(words):
        return False
    if password:
        passless = _entropy(words)
        if not (_is_password_seed(passless) and not _is_basic_seed(passless)):
            return False
    return _is_basic_seed(_entropy(words, password))


def _ton_keypair(words: list[str], password: str) -> KeyPair:
    seed = hashlib.pbkdf2_hmac("sha512", _entropy(words, password), b"TON default seed", _PBKDF_ITERATIONS, 64)
    return KeyPair.from_seed(seed[:32])


# ------------------------------------------------------------------- BIP39
def validate_bip39(mnemonic: "str | list[str]") -> bool:
    """``True`` if the words carry a correct BIP39 checksum."""
    words = normalize(mnemonic)
    if len(words) not in (12, 15, 18, 21, 24) or not _known_words(words):
        return False
    index = {w: i for i, w in enumerate(wordlist())}
    bits = "".join(format(index[w], "011b") for w in words)
    ent_len = len(bits) * 32 // 33
    entropy = int(bits[:ent_len], 2).to_bytes(ent_len // 8, "big")
    checksum = format(hashlib.sha256(entropy).digest()[0], "08b")[: len(bits) - ent_len]
    return bits[ent_len:] == checksum


def bip39_seed(words: list[str], passphrase: str = "") -> bytes:
    phrase = unicodedata.normalize("NFKD", " ".join(words)).encode()
    salt = unicodedata.normalize("NFKD", "mnemonic" + passphrase).encode()
    return hashlib.pbkdf2_hmac("sha512", phrase, salt, 2048)


def slip10_ed25519(seed: bytes, path: "tuple[int, ...] | list[int]") -> bytes:
    """SLIP-0010 private key for Ed25519; every path element is hardened."""
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key, chain = digest[:32], digest[32:]
    for i in path:
        data = b"\x00" + key + (i | 0x80000000).to_bytes(4, "big")
        digest = hmac.new(chain, data, hashlib.sha512).digest()
        key, chain = digest[:32], digest[32:]
    return key


def _bip39_keypair(words: list[str], passphrase: str) -> KeyPair:
    return KeyPair.from_seed(slip10_ed25519(bip39_seed(words, passphrase), BIP39_TON_PATH))


# ------------------------------------------------------------------ public
def detect(mnemonic: "str | list[str]", password: str = "") -> str | None:
    """``"ton"``, ``"bip39"`` or ``None`` (not a usable seed phrase)."""
    words = normalize(mnemonic)
    if validate(words, password):
        return TON
    if validate_bip39(words):
        return BIP39
    return None


def to_keypair(mnemonic: "str | list[str]", password: str = "", *, kind: str = AUTO) -> KeyPair:
    """Derive the wallet key pair.

    ``kind`` is ``"auto"`` (TON format first, then BIP39), ``"ton"`` or ``"bip39"``.
    ``password`` is the TON mnemonic password or the BIP39 passphrase.
    """
    words = normalize(mnemonic)
    if kind == AUTO:
        kind = detect(words, password) or ""
    if kind == TON:
        if not validate(words, password):
            raise MnemonicError("invalid TON mnemonic (wrong words, word count or password)")
        return _ton_keypair(words, password)
    if kind == BIP39:
        if not validate_bip39(words):
            raise MnemonicError("invalid BIP39 mnemonic (wrong words or checksum)")
        return _bip39_keypair(words, password)
    raise MnemonicError(
        "not a valid seed phrase: neither a TON mnemonic (24 words) nor a BIP39 mnemonic "
        "(check spelling and word order)"
    )


def generate(words_count: int = 24) -> list[str]:
    """Generate a new TON mnemonic (handy for tests; not needed for purchases)."""
    wl = wordlist()
    while True:
        words = [wl[secrets.randbelow(len(wl))] for _ in range(words_count)]
        if _is_basic_seed(_entropy(words)):
            return words
