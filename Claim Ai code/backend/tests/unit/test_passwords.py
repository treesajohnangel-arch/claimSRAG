"""Unit tests for password hashing and verification (no database). All credentials here are fake."""

import pytest

from app.config import Settings
from app.logging_config import configure_logging
from app.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password, password_problem, verify_password

PASSWORD = "fake-correct-horse-battery-staple"


@pytest.fixture(scope="module")
def stored_hash() -> str:
    return hash_password(PASSWORD)


def test_hash_is_a_non_empty_argon2id_string_unrelated_to_the_password(stored_hash: str) -> None:
    assert stored_hash
    assert stored_hash != PASSWORD
    assert PASSWORD not in stored_hash
    # The parameters are pinned (RFC 9106 low-memory profile). The salt and hash parts are random.
    assert stored_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")


def test_correct_password_verifies(stored_hash: str) -> None:
    assert verify_password(PASSWORD, stored_hash) is True


@pytest.mark.parametrize(
    "attempt",
    ["", "wrong-password", PASSWORD.upper(), PASSWORD + " ", " " + PASSWORD],
    ids=["empty", "different", "case-changed", "trailing-space", "leading-space"],
)
def test_incorrect_password_fails(stored_hash: str, attempt: str) -> None:
    assert verify_password(attempt, stored_hash) is False


def test_the_same_password_hashed_twice_gives_different_hashes() -> None:
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)

    assert first != second  # random salt
    assert verify_password(PASSWORD, first) and verify_password(PASSWORD, second)


def test_an_empty_password_cannot_be_hashed() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        hash_password("")


@pytest.mark.security
@pytest.mark.parametrize(
    "stored",
    [
        "",
        "not-a-hash",
        "$argon2id$v=19$m=65536",
        "$argon2id$v=19$m=65536,t=3,p=4$!!!$!!!",
        "$2b$12$abcdefghijklmnopqrstuuK8Kb7yQ1VYJ6m3w2r2b1Yb9iZb8yX6",
    ],
    ids=["empty", "plain-text", "truncated", "bad-base64", "bcrypt"],
)
def test_malformed_stored_hash_fails_safely_without_exposing_it(
    stored: str, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)

    result = verify_password(PASSWORD, stored)
    logs = capsys.readouterr().err

    assert result is False
    assert "stored password hash is malformed or unsupported" in logs
    assert PASSWORD not in logs
    if stored:
        assert stored not in logs


@pytest.mark.parametrize(("password", "problem"),
                         [("a" * 11, "Use at least 12 characters."), (" " * 12, "The password can't be only spaces."),
                          ("a" * 257, "Use at most 256 characters."), ("a" * 12, None),
                          ("correct horse battery staple", None), ("x" * 256, None)],
                         ids=["too-short", "only-spaces", "too-long", "shortest", "passphrase", "longest"])
def test_the_new_account_password_policy_is_a_length_range(password: str, problem: str | None) -> None:
    # D-035: one policy for registration and the operator tooling (NIST SP 800-63B: length, no composition rules).
    assert (MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH) == (12, 256)
    assert password_problem(password) == problem

