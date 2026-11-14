"""Canonical email normalisation: the one definition for user lookup, authentication and (later) user creation.

It produces the form the database requires for users.email (migration 0001): trimmed, lower-case, exactly one "@",
no whitespace, at most 254 characters. There is no provider-specific rewriting (no Gmail dot or "+tag" removal).
"""

import re

MAX_EMAIL_LENGTH = 254
_EMAIL_FORMAT = re.compile(r"[^@\s]+@[^@\s]+")


class InvalidEmailError(ValueError):
    """The value is not a usable email address. The message never repeats the input."""


def normalize_email(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidEmailError("Email must be a string")
    email = value.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_FORMAT.fullmatch(email):
        raise InvalidEmailError("Invalid email address")
    return email
