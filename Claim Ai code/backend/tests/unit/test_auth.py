"""Unit tests for the authentication result types (no database)."""

from uuid import uuid4

import pytest

from app.auth import GENERIC_FAILURE_MESSAGE, AuthenticatedUser, AuthenticationError


@pytest.mark.security
def test_identity_model_has_no_credential_fields_and_drops_them_if_given() -> None:
    identity = AuthenticatedUser.model_validate(
        {
            "user_id": uuid4(),
            "company_id": uuid4(),
            "email": "alice@example.com",
            "full_name": "Alice",
            "role": "STAFF",
            "status": "ACTIVE",
            "password_hash": "$argon2id$fake",
            "password": "fake-password",
        }
    )

    assert set(AuthenticatedUser.model_fields) == {"user_id", "company_id", "email", "full_name", "role", "status"}
    assert "password" not in identity.model_dump_json()
    assert "$argon2id$fake" not in repr(identity)


@pytest.mark.security
def test_authentication_error_is_always_the_same_generic_message() -> None:
    error = AuthenticationError()

    assert str(error) == GENERIC_FAILURE_MESSAGE == "Invalid credentials"
    assert error.args == ("Invalid credentials",)
