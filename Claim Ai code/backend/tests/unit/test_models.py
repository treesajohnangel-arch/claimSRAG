"""Unit tests for the company, user, policy and policy-document models (no database)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models import (
    CompanyStatus,
    DocumentStatus,
    InsuranceType,
    Policy,
    PolicyDocument,
    PolicyStatus,
    User,
    UserRole,
    UserStatus,
)

FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZS1oYXNo"


def _user(**overrides: object) -> User:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "company_id": uuid4(),
        "email": "alice@example.com",
        "password_hash": FAKE_HASH,
        "full_name": "Alice",
        "role": "STAFF",
        "status": "ACTIVE",
        "created_at": now,
        "updated_at": now,
    }
    return User.model_validate({**values, **overrides})


def test_allowed_values_match_the_v1_specification() -> None:
    assert {r.value for r in UserRole} == {"COMPANY_ADMIN", "STAFF"}
    assert {s.value for s in UserStatus} == {"ACTIVE", "INACTIVE"}
    assert {s.value for s in CompanyStatus} == {"ACTIVE", "INACTIVE"}


@pytest.mark.security
def test_password_hash_is_masked_in_every_representation_of_a_user() -> None:
    user = _user()

    assert user.password_hash.get_secret_value() == FAKE_HASH
    for text in (repr(user), str(user), user.model_dump_json(), str(user.model_dump())):
        assert FAKE_HASH not in text


@pytest.mark.parametrize(("field", "value"), [("role", "ADMIN"), ("status", "DELETED")])
def test_user_model_rejects_values_outside_the_allowed_sets(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _user(**{field: value})


def test_policy_vocabularies_match_the_v1_specification() -> None:
    assert {t.value for t in InsuranceType} == {"HEALTH", "MOTOR", "TRAVEL", "HOME"}
    assert {s.value for s in PolicyStatus} == {"ACTIVE", "INACTIVE"}
    # Phase 2.1 documents are only recorded or withdrawn; processing and version-lifecycle states come later.
    assert {s.value for s in DocumentStatus} == {"UPLOADED", "INACTIVE"}


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [("policy", "insurance_type", "LIFE"), ("policy", "status", "DELETED"), ("document", "status", "ACTIVE")],
)
def test_policy_models_reject_values_outside_the_allowed_sets(model: str, field: str, value: str) -> None:
    now = datetime.now(UTC)
    policy = {"id": uuid4(), "company_id": uuid4(), "policy_number": "POL-001", "name": "Health Cover",
              "insurance_type": "HEALTH", "status": "ACTIVE", "created_at": now, "updated_at": now}
    document = {"id": uuid4(), "company_id": uuid4(), "policy_id": uuid4(), "document_key": "policy-wording",
                "version": 1, "status": "UPLOADED", "original_filename": "p.pdf", "storage_reference": "docs/p.pdf",
                "content_type": "application/pdf", "size_bytes": 10, "checksum_sha256": "0" * 64, "created_at": now,
                "updated_at": now}
    target, values = (Policy, policy) if model == "policy" else (PolicyDocument, document)

    assert target.model_validate(values)  # the valid row is accepted
    with pytest.raises(ValidationError):
        target.model_validate({**values, field: value})
