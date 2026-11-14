"""Unit tests for the canonical email normalisation (no database)."""

import pytest

from app.emails import InvalidEmailError, normalize_email


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  alice@example.com  ", "alice@example.com"),
        ("\talice@example.com\n", "alice@example.com"),
        ("Alice@Example.COM", "alice@example.com"),
        ("alice@example.com", "alice@example.com"),
        ("ÉLODIE@EXAMPLE.FR", "élodie@example.fr"),
    ],
    ids=["surrounding-spaces", "surrounding-tab-newline", "upper-case", "already-normalised", "non-ascii"],
)
def test_email_is_trimmed_and_lower_cased(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


def test_normalisation_is_idempotent() -> None:
    once = normalize_email("  Bob.Smith@Example.org ")

    assert normalize_email(once) == once


def test_no_provider_specific_rewriting() -> None:
    # Dots and "+tags" are kept: different addresses stay different.
    assert normalize_email("First.Last+Claims@Gmail.com") == "first.last+claims@gmail.com"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "alice", "alice@", "@example.com", "a@b@example.com", "alice smith@example.com", "a" * 250 + "@e.com"],
    ids=["empty", "blank", "no-at", "no-domain", "no-local-part", "two-ats", "inner-space", "too-long"],
)
def test_invalid_email_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidEmailError):
        normalize_email(raw)


def test_non_string_is_rejected() -> None:
    with pytest.raises(InvalidEmailError):
        normalize_email(None)  # type: ignore[arg-type]


@pytest.mark.security
def test_rejection_message_does_not_repeat_the_input() -> None:
    # People sometimes type a password into the email field, so the input must not reach messages or logs.
    typed = "fake-secret-typed-into-email-field"

    with pytest.raises(InvalidEmailError) as excinfo:
        normalize_email(typed)

    assert typed not in str(excinfo.value)
