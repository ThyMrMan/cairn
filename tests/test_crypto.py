"""Sealing and password hashing."""

from __future__ import annotations

import pytest

from cairn.crypto.passwords import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from cairn.crypto.sealing import Sealer, SealError, key_fingerprint

KEY_A = b"a-master-key-that-is-long-enough-32"
KEY_B = b"a-different-master-key-also-long-32!"


def test_seal_roundtrip(sealer: Sealer) -> None:
    blob = sealer.seal("cookie jar contents", context="profile.cookies")
    assert sealer.unseal_text(blob, context="profile.cookies") == "cookie jar contents"


def test_ciphertext_does_not_contain_plaintext(sealer: Sealer) -> None:
    blob = sealer.seal("SUPER_SECRET_VALUE", context="profile.cookies")
    assert b"SUPER_SECRET_VALUE" not in blob


def test_nonce_is_unique_per_seal(sealer: Sealer) -> None:
    a = sealer.seal("same input", context="x")
    b = sealer.seal("same input", context="x")
    assert a != b, "identical plaintexts must not produce identical ciphertexts"


def test_wrong_context_fails(sealer: Sealer) -> None:
    """Context is both an HKDF input and AAD, so a blob cannot be replayed
    into a different field even with the right key."""
    blob = sealer.seal("secret", context="profile.cookies")
    with pytest.raises(SealError):
        sealer.unseal(blob, context="user.totp")


def test_wrong_key_fails() -> None:
    blob = Sealer(KEY_A).seal("secret", context="x")
    with pytest.raises(SealError):
        Sealer(KEY_B).unseal(blob, context="x")


def test_tampered_ciphertext_fails(sealer: Sealer) -> None:
    blob = bytearray(sealer.seal("secret value here", context="x"))
    blob[-1] ^= 0xFF
    with pytest.raises(SealError):
        sealer.unseal(bytes(blob), context="x")


def test_truncated_blob_fails(sealer: Sealer) -> None:
    blob = sealer.seal("secret", context="x")
    with pytest.raises(SealError):
        sealer.unseal(blob[:8], context="x")


def test_short_key_rejected() -> None:
    with pytest.raises(SealError):
        Sealer(b"too-short")


def test_fingerprint_is_stable_and_distinct() -> None:
    assert key_fingerprint(KEY_A) == key_fingerprint(KEY_A)
    assert key_fingerprint(KEY_A) != key_fingerprint(KEY_B)
    # Non-reversible: the key must not be recoverable from it.
    assert KEY_A.decode() not in key_fingerprint(KEY_A)


def test_password_roundtrip() -> None:
    stored = hash_password("correct-horse-battery")
    assert verify_password(stored, "correct-horse-battery")
    assert not verify_password(stored, "wrong")


def test_password_hash_is_salted() -> None:
    assert hash_password("same") != hash_password("same")


def test_verify_against_missing_user_returns_false() -> None:
    """None means 'no such user'; it must still burn the hash time."""
    assert verify_password(None, "anything") is False


@pytest.mark.parametrize(
    ("password", "expect_problem"),
    [
        ("short", True),
        ("aaaaaaaaaaaaaaa", True),  # single repeated character
        ("123456789012345", True),  # all digits
        ("password", True),  # common
        ("correct-horse-battery-staple", False),
    ],
)
def test_password_strength(password: str, expect_problem: bool) -> None:
    assert bool(validate_password_strength(password, 12)) is expect_problem
