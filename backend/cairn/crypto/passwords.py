"""Argon2id password hashing.

Not bcrypt, not PBKDF2 (docs/11). Parameters target ~64 MB and 3 iterations,
which is a few hundred milliseconds on NAS-class hardware — acceptable for a
login that happens rarely, and expensive enough to make offline cracking of a
stolen database impractical.
"""

from __future__ import annotations

import re
from contextlib import suppress

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# A dummy hash of a random value, used to burn the same CPU time on unknown
# usernames as on known ones. Without this, response timing reveals which
# accounts exist.
_DUMMY_HASH = _hasher.hash("cairn-timing-equalizer-not-a-real-password")

COMMON_PASSWORDS = frozenset(
    {
        "password", "password1", "password123", "passw0rd", "123456789",
        "1234567890", "qwertyuiop", "letmein123", "administrator", "changeme",
        "welcome123", "iloveyou123", "adminadmin", "qwerty123456",
    }
)  # fmt: skip


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant-ish time verification.

    Passing `None` (no such user) still runs a full hash comparison against a
    dummy, so the timing profile matches the known-user path.
    """
    if stored_hash is None:
        with suppress(VerifyMismatchError, VerificationError, InvalidHashError):
            _hasher.verify(_DUMMY_HASH, password)
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than current."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str, min_length: int = 12) -> list[str]:
    """Return a list of problems; empty means acceptable.

    Deliberately light on composition rules — length and not-obviously-common
    do more real work than forcing a symbol.
    """
    problems: list[str] = []
    if len(password) < min_length:
        problems.append(f"Must be at least {min_length} characters.")
    if len(password) > 1024:
        problems.append("Must be at most 1024 characters.")
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        problems.append("This is a commonly used password.")
    if password and re.fullmatch(r"(.)\1*", password):
        problems.append("Cannot be a single repeated character.")
    if re.fullmatch(r"\d+", password):
        problems.append("Cannot be only digits.")
    return problems
