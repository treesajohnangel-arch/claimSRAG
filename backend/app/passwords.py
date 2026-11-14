"""Password hashing and verification with Argon2id (argon2-cffi).

Parameters are set explicitly to RFC 9106's low-memory profile, so a library upgrade cannot change them silently:
Argon2id, t=3, m=64 MiB, p=4, a 16-byte random salt and a 32-byte hash. Hashes are PHC strings,
"$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>".

Passwords and hashes never appear in logs or exception messages.
"""

import logging

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.profiles import RFC_9106_LOW_MEMORY

logger = logging.getLogger(__name__)

_hasher = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)

# The password policy for new accounts (company registration and the operator tooling, D-035): a length range, as
# NIST SP 800-63B recommends, rather than composition rules. The upper bound only caps hashing work.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256


def password_problem(password: str) -> str | None:
    """Why ``password`` is not acceptable for a new account, as a user-facing sentence, or None if it is."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    if not password.strip():
        return "The password can't be only spaces."
    return None


def hash_password(password: str) -> str:
    """Hash a password for storage. Each call uses a new random salt."""
    if not password:
        raise ValueError("Password must not be empty")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True only if ``password`` matches ``password_hash``. Never raises for a bad stored hash."""
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError):
        # The stored value is not a usable Argon2 hash (corrupt, truncated or another scheme). Report the fact, never
        # the value; the caller just sees a failed verification.
        logger.warning("password verification failed: stored password hash is malformed or unsupported")
        return False
