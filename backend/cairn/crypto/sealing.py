"""AES-GCM sealing for secrets at rest.

Cookie jars are credentials (docs/11). Everything sensitive that reaches the
database goes through a `Sealer`:

    blob = sealer.seal(cookies_txt, context="profile.cookies")
    raw  = sealer.unseal(blob, context="profile.cookies")

`context` provides domain separation via HKDF `info`, so a blob sealed for one
purpose cannot be unsealed as another even with the same master key. Getting
the context wrong is a decryption failure, not a silent success.

Blob layout::

    b"CRN1" | nonce (12 bytes) | ciphertext+tag

The master key never encrypts directly; per-context subkeys are derived from
it with HKDF-SHA256.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"CRN1"
NONCE_BYTES = 12
KEY_BYTES = 32
_FINGERPRINT_INFO = b"cairn/key-fingerprint/v1"


class SealError(RuntimeError):
    """Sealing or unsealing failed."""


def _derive(master_key: bytes, context: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=None,
        info=f"cairn/seal/v1/{context}".encode(),
    ).derive(master_key)


def key_fingerprint(master_key: bytes) -> str:
    """A stable, non-reversible identifier for a master key.

    Stored in the `settings` table so startup can detect a changed or missing
    key and refuse to run rather than leaving sealed data undecryptable with
    no explanation (docs/10).
    """
    digest = hmac.new(master_key, _FINGERPRINT_INFO, hashlib.sha256).hexdigest()
    return f"sha256:{digest[:32]}"


class Sealer:
    """Seals and unseals secrets under a master key."""

    __slots__ = ("_cache", "_master_key")

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) < KEY_BYTES:
            raise SealError(f"master key must be at least {KEY_BYTES} bytes, got {len(master_key)}")
        self._master_key = master_key
        self._cache: dict[str, AESGCM] = {}

    def _cipher(self, context: str) -> AESGCM:
        cipher = self._cache.get(context)
        if cipher is None:
            cipher = AESGCM(_derive(self._master_key, context))
            self._cache[context] = cipher
        return cipher

    def seal(self, plaintext: bytes | str, *, context: str) -> bytes:
        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")
        nonce = os.urandom(NONCE_BYTES)
        # The context is authenticated as AAD as well as being mixed into the
        # subkey, so a blob cannot be replayed into a different field.
        ciphertext = self._cipher(context).encrypt(nonce, plaintext, context.encode("utf-8"))
        return MAGIC + nonce + ciphertext

    def unseal(self, blob: bytes, *, context: str) -> bytes:
        if not blob.startswith(MAGIC):
            raise SealError("not a sealed blob (bad magic)")
        body = blob[len(MAGIC) :]
        if len(body) < NONCE_BYTES + 16:
            raise SealError("sealed blob is truncated")
        nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
        try:
            return self._cipher(context).decrypt(nonce, ciphertext, context.encode("utf-8"))
        except InvalidTag as exc:
            raise SealError(
                "could not unseal: wrong master key, wrong context, or corrupted data"
            ) from exc

    def unseal_text(self, blob: bytes, *, context: str) -> str:
        return self.unseal(blob, context=context).decode("utf-8")

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self._master_key)
