"""Unit tests for access tokens (no database, no network).

Every key is generated in memory for the test run, and every credential is fake. Tokens with bad claims are signed
with the real test key on purpose: they show that a correct signature alone is not enough.
"""

import base64
import hashlib
import hmac
import json
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes

from app.auth import AuthenticatedUser
from app.config import Settings
from app.logging_config import REDACTED, configure_logging
from app.models import UserRole, UserStatus
from app.tokens import (
    ALGORITHM,
    GENERIC_TOKEN_FAILURE_MESSAGE,
    REQUIRED_CLAIMS,
    AccessTokenError,
    TokenConfigurationError,
    TokenIdentity,
    decode_access_token,
    issue_access_token,
)
from tests.conftest import RsaKeyPair, generate_rsa_key_pair

PASSWORD = "fake-Passw0rd-for-token-tests"
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZS1oYXNo"
EMAIL = "alice@acme.example"
FULL_NAME = "Alice Staff"

Claims = dict[str, Any]


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


@pytest.fixture(scope="module")
def signing_key(jwt_keys: RsaKeyPair) -> PrivateKeyTypes:
    """The test private key as an object, for crafting tokens directly with PyJWT."""
    return serialization.load_pem_private_key(jwt_keys.private_pem.encode(), password=None)


@pytest.fixture
def user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email=EMAIL,
        full_name=FULL_NAME,
        role=UserRole.STAFF,
        status=UserStatus.ACTIVE,
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _header_and_claims(token: str) -> tuple[dict[str, Any], Claims]:
    """Read a token's header and claims without verifying it (inspection only)."""
    header, payload, _ = token.split(".")
    return json.loads(_unb64(header)), json.loads(_unb64(payload))


def _claims_for(user: AuthenticatedUser) -> Claims:
    """Claims shaped exactly like the ones issue_access_token() produces, valid for 15 minutes from now."""
    now = int(datetime.now(UTC).timestamp())
    return {
        "sub": str(user.user_id),
        "company_id": str(user.company_id),
        "role": user.role.value,
        "iat": now,
        "exp": now + 900,
        "jti": str(uuid.uuid4()),
    }


def _unsigned(header: dict[str, Any], claims: Claims, signature: str = "") -> str:
    return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}.{signature}"


def _with_signature(token: str, signature: str) -> str:
    return token[: token.rindex(".") + 1] + signature


def _altered_signature(token: str) -> str:
    signature = token[token.rindex(".") + 1 :]
    return _with_signature(token, ("B" if signature[0] == "A" else "A") + signature[1:])


def _rejected(token: str, settings: Settings) -> AccessTokenError:
    with pytest.raises(AccessTokenError) as excinfo:
        decode_access_token(token, settings)
    return excinfo.value


# --- Issuance -------------------------------------------------------------------------------------------------


def test_issued_token_is_rs256_signed_with_exactly_the_required_claims(
    user: AuthenticatedUser, token_settings: Settings
) -> None:
    token = issue_access_token(user, token_settings)
    header, claims = _header_and_claims(token)

    assert ALGORITHM == "RS256"
    assert header == {"alg": "RS256", "typ": "JWT"}
    assert set(claims) == set(REQUIRED_CLAIMS) == {"sub", "company_id", "role", "iat", "exp", "jti"}
    assert claims["sub"] == str(user.user_id)
    assert claims["company_id"] == str(user.company_id)
    assert claims["role"] == "STAFF"
    assert type(claims["iat"]) is int and type(claims["exp"]) is int
    assert uuid.UUID(claims["jti"])
    assert token.split(".")[2]  # signed: the signature segment is not empty


@pytest.mark.security
def test_token_contains_no_credentials_or_personal_data(token_settings: Settings) -> None:
    # Credential fields offered to the identity model are dropped (Phase 1.2), so they cannot reach the token.
    user = AuthenticatedUser.model_validate(
        {
            "user_id": uuid.uuid4(),
            "company_id": uuid.uuid4(),
            "email": EMAIL,
            "full_name": FULL_NAME,
            "role": "STAFF",
            "status": "ACTIVE",
            "password": PASSWORD,
            "password_hash": PASSWORD_HASH,
        }
    )

    token = issue_access_token(user, token_settings)
    _, claims = _header_and_claims(token)
    payload_text = _unb64(token.split(".")[1]).decode()

    assert not {"password", "password_hash", "email", "full_name", "status"} & set(claims)
    for text in (token, payload_text):
        for value in (PASSWORD, PASSWORD_HASH, EMAIL, FULL_NAME):
            assert value not in text


def test_every_token_gets_a_unique_random_jti(user: AuthenticatedUser, token_settings: Settings) -> None:
    jtis = [_header_and_claims(issue_access_token(user, token_settings))[1]["jti"] for _ in range(50)]

    assert len(set(jtis)) == 50
    assert all(uuid.UUID(jti).version == 4 for jti in jtis)  # random UUIDs, not a counter


@pytest.mark.security
@pytest.mark.parametrize(
    "candidate",
    [
        {"user_id": str(uuid.uuid4()), "company_id": str(uuid.uuid4()), "role": "COMPANY_ADMIN"},
        TokenIdentity(user_id=uuid.uuid4(), company_id=uuid.uuid4(), role=UserRole.COMPANY_ADMIN),
    ],
    ids=["client-json", "token-identity"],
)
def test_tokens_are_issued_only_for_an_authenticated_user(candidate: object, token_settings: Settings) -> None:
    with pytest.raises(TypeError, match="only for an AuthenticatedUser"):
        issue_access_token(candidate, token_settings)  # type: ignore[arg-type]


# --- Lifetime and expiry --------------------------------------------------------------------------------------


def test_default_lifetime_is_15_minutes(user: AuthenticatedUser, token_settings: Settings) -> None:
    before = int(datetime.now(UTC).timestamp())
    _, claims = _header_and_claims(issue_access_token(user, token_settings))
    after = int(datetime.now(UTC).timestamp())

    assert token_settings.access_token_lifetime_minutes == 15
    assert before <= claims["iat"] <= after
    assert claims["exp"] - claims["iat"] == 15 * 60


def test_configured_lifetime_is_respected(user: AuthenticatedUser, jwt_keys: RsaKeyPair) -> None:
    settings = Settings(
        _env_file=None,
        jwt_private_key=jwt_keys.private_pem,
        jwt_public_key=jwt_keys.public_pem,
        access_token_lifetime_minutes=5,
    )
    issued_at = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)

    _, claims = _header_and_claims(issue_access_token(user, settings, now=issued_at))

    assert claims["iat"] == int(issued_at.timestamp())
    assert claims["exp"] - claims["iat"] == 5 * 60


def test_token_is_accepted_until_it_expires(user: AuthenticatedUser, token_settings: Settings) -> None:
    one_minute_left = issue_access_token(user, token_settings, now=datetime.now(UTC) - timedelta(minutes=14))

    assert decode_access_token(one_minute_left, token_settings).user_id == user.user_id


@pytest.mark.security
def test_expired_token_is_rejected(user: AuthenticatedUser, token_settings: Settings) -> None:
    # Issued 15 minutes and 1 second ago, so it expired 1 second ago. No sleeping needed.
    expired = issue_access_token(user, token_settings, now=datetime.now(UTC) - timedelta(minutes=15, seconds=1))

    _rejected(expired, token_settings)


# --- Validation -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole))
def test_valid_token_reconstructs_the_identity(user: AuthenticatedUser, token_settings: Settings, role: UserRole) -> None:
    user = user.model_copy(update={"role": role})

    identity = decode_access_token(issue_access_token(user, token_settings), token_settings)

    assert isinstance(identity, TokenIdentity)
    assert identity == TokenIdentity(user_id=user.user_id, company_id=user.company_id, role=role)


@pytest.mark.security
def test_identity_holds_only_user_company_and_role() -> None:
    assert set(TokenIdentity.model_fields) == {"user_id", "company_id", "role"}


@pytest.mark.security
@pytest.mark.parametrize(
    "make",
    [
        lambda token: "",
        lambda token: "not-a-jwt",
        lambda token: "a.b.c",
        lambda token: "...",
        lambda token: ".".join(token.split(".")[:2]),
        lambda token: token + ".extra",
        lambda token: token[: len(token) // 2],
        lambda token: token.replace(".", "", 1),
        lambda token: "!!!." + token.split(".", 1)[1],
    ],
    ids=["empty", "not-a-jwt", "garbage-segments", "empty-segments", "two-segments", "four-segments", "truncated",
         "joined-segments", "bad-header-encoding"],
)
def test_malformed_token_is_rejected(
    make: Callable[[str], str], user: AuthenticatedUser, token_settings: Settings
) -> None:
    _rejected(make(issue_access_token(user, token_settings)), token_settings)


@pytest.mark.security
@pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
def test_token_missing_a_required_claim_is_rejected(
    claim: str, user: AuthenticatedUser, token_settings: Settings, signing_key: PrivateKeyTypes
) -> None:
    claims = _claims_for(user)
    del claims[claim]

    _rejected(jwt.encode(claims, signing_key, algorithm=ALGORITHM), token_settings)


@pytest.mark.security
@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("sub", "not-a-uuid"),
        ("sub", 12345),
        ("sub", ""),
        ("sub", None),
        ("company_id", "acme"),
        ("company_id", 7),
        ("company_id", [str(uuid.uuid4())]),
        ("role", "SUPERUSER"),
        ("role", "staff"),
        ("role", ["STAFF"]),
        ("role", ""),
        ("iat", "1794400000"),
        ("iat", lambda now: now + 0.5),
        ("iat", True),
        ("iat", lambda now: now + 3600),
        ("exp", "9999999999"),
        ("exp", lambda now: now + 900.5),
        ("exp", True),
        ("exp", [1]),
        ("jti", ""),
        ("jti", "1"),
        ("jti", 42),
    ],
    ids=[
        "sub-not-uuid", "sub-int", "sub-empty", "sub-null",
        "company-not-uuid", "company-int", "company-list",
        "role-unknown", "role-lower-case", "role-list", "role-empty",
        "iat-string", "iat-float", "iat-bool", "iat-in-future",
        "exp-string", "exp-float", "exp-bool", "exp-list",
        "jti-empty", "jti-not-uuid", "jti-int",
    ],
)
def test_token_with_an_invalid_claim_value_is_rejected(
    claim: str, value: Any, user: AuthenticatedUser, token_settings: Settings, signing_key: PrivateKeyTypes
) -> None:
    claims = _claims_for(user)
    claims[claim] = value(claims["iat"]) if callable(value) else value

    _rejected(jwt.encode(claims, signing_key, algorithm=ALGORITHM), token_settings)


# --- Tampering and foreign keys -------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    "change",
    [
        {"role": "COMPANY_ADMIN"},
        {"company_id": str(uuid.uuid4())},
        {"sub": str(uuid.uuid4())},
        {"exp": 4102444800},  # 2100-01-01
    ],
    ids=["role-escalation", "other-company", "other-user", "extended-expiry"],
)
def test_tampered_payload_is_rejected(change: Claims, user: AuthenticatedUser, token_settings: Settings) -> None:
    token = issue_access_token(user, token_settings)
    header, payload, signature = token.split(".")
    claims = json.loads(_unb64(payload)) | change
    forged = f"{header}.{_b64(json.dumps(claims).encode())}.{signature}"

    assert decode_access_token(token, token_settings).role is UserRole.STAFF  # the original is valid
    _rejected(forged, token_settings)


@pytest.mark.security
@pytest.mark.parametrize(
    "forge",
    [
        lambda token, other: _altered_signature(token),
        lambda token, other: _with_signature(token, ""),
        lambda token, other: _with_signature(token, other.rsplit(".", 1)[1]),
    ],
    ids=["altered-signature", "signature-removed", "signature-from-another-token"],
)
def test_tampered_signature_is_rejected(
    forge: Callable[[str, str], str], user: AuthenticatedUser, token_settings: Settings
) -> None:
    token, other = issue_access_token(user, token_settings), issue_access_token(user, token_settings)

    _rejected(forge(token, other), token_settings)


@pytest.mark.security
def test_token_signed_with_another_key_is_rejected(user: AuthenticatedUser, token_settings: Settings) -> None:
    attacker = generate_rsa_key_pair()
    attacker_key = serialization.load_pem_private_key(attacker.private_pem.encode(), password=None)
    attacker_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(attacker_key.public_key(), as_dict=True)  # type: ignore[union-attr]

    plain = jwt.encode(_claims_for(user), attacker_key, algorithm=ALGORITHM)
    # The header offers the attacker's own public key; verification must ignore it and use the configured key.
    with_embedded_key = jwt.encode(_claims_for(user), attacker_key, algorithm=ALGORITHM, headers={"jwk": attacker_jwk})

    _rejected(plain, token_settings)
    _rejected(with_embedded_key, token_settings)


# --- Algorithm protection -------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("alg", ["none", "None", "NONE", "nOnE"])
@pytest.mark.parametrize("signature", ["", "fake-signature"], ids=["empty-signature", "bogus-signature"])
def test_unsigned_alg_none_token_is_rejected(
    alg: str, signature: str, user: AuthenticatedUser, token_settings: Settings
) -> None:
    _rejected(_unsigned({"alg": alg, "typ": "JWT"}, _claims_for(user), signature), token_settings)


@pytest.mark.security
def test_hs256_token_keyed_with_the_public_key_is_rejected(
    user: AuthenticatedUser, token_settings: Settings, jwt_keys: RsaKeyPair
) -> None:
    # Classic algorithm confusion: an attacker who knows the (public) verification key uses it as an HMAC secret.
    unsigned = _unsigned({"alg": "HS256", "typ": "JWT"}, _claims_for(user))
    signing_input = unsigned[:-1].encode()
    forged = unsigned + _b64(hmac.new(jwt_keys.public_pem.encode(), signing_input, hashlib.sha256).digest())

    _rejected(forged, token_settings)


@pytest.mark.security
@pytest.mark.parametrize("alg", ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512"])
def test_only_the_configured_algorithm_is_accepted(
    alg: str, user: AuthenticatedUser, token_settings: Settings, signing_key: PrivateKeyTypes
) -> None:
    # Each token carries a genuine signature by the configured key under the algorithm its header names. If the
    # header chose the verification algorithm, all of them would pass; only the configured RS256 may.
    token = jwt.encode(_claims_for(user), signing_key, algorithm=alg)

    if alg == ALGORITHM:
        assert decode_access_token(token, token_settings).user_id == user.user_id
    else:
        _rejected(token, token_settings)


@pytest.mark.security
@pytest.mark.parametrize("alg", ["RS512", "PS256", "ES256", "HS256", "none"])
def test_token_header_cannot_select_the_verification_algorithm(
    alg: str, user: AuthenticatedUser, token_settings: Settings
) -> None:
    token = issue_access_token(user, token_settings)
    _, payload, signature = token.split(".")
    relabelled = f"{_b64(json.dumps({'alg': alg, 'typ': 'JWT'}).encode())}.{payload}.{signature}"

    _rejected(relabelled, token_settings)


@pytest.mark.security
def test_token_signed_with_an_elliptic_curve_key_is_rejected(user: AuthenticatedUser, token_settings: Settings) -> None:
    _rejected(jwt.encode(_claims_for(user), ec.generate_private_key(ec.SECP256R1()), algorithm="ES256"), token_settings)


# --- Errors, configuration and logs ---------------------------------------------------------------------------


@pytest.mark.security
def test_every_invalid_token_raises_the_same_generic_error_without_internal_details(
    user: AuthenticatedUser, token_settings: Settings, signing_key: PrivateKeyTypes
) -> None:
    valid = issue_access_token(user, token_settings)
    claims = _claims_for(user)
    del claims["jti"]
    invalid_tokens = [
        issue_access_token(user, token_settings, now=datetime.now(UTC) - timedelta(hours=1)),  # expired
        "a.b.c",  # malformed
        _altered_signature(valid),  # bad signature
        _unsigned({"alg": "none"}, _claims_for(user)),  # alg=none
        jwt.encode(claims, signing_key, algorithm=ALGORITHM),  # missing claim
        jwt.encode(_claims_for(user) | {"role": "ROOT"}, signing_key, algorithm=ALGORITHM),  # invalid claim
    ]

    for token in invalid_tokens:
        error = _rejected(token, token_settings)
        formatted = "".join(traceback.format_exception(error))

        assert str(error) == GENERIC_TOKEN_FAILURE_MESSAGE == "Invalid or expired token"
        assert error.args == (GENERIC_TOKEN_FAILURE_MESSAGE,)
        assert error.__cause__ is None and error.__suppress_context__ is True
        for detail in ("Error:", "Signature", "segments", "claim", "alg", "ValidationError"):
            assert detail not in formatted.replace("AccessTokenError: Invalid or expired token", "")
        if token:
            assert token not in formatted


def test_without_keys_tokens_fail_clearly_as_a_configuration_error(user: AuthenticatedUser, settings: Settings) -> None:
    assert settings.jwt_private_key is None and settings.jwt_public_key is None

    with pytest.raises(TokenConfigurationError, match="JWT_PRIVATE_KEY is not configured"):
        issue_access_token(user, settings)
    with pytest.raises(TokenConfigurationError, match="JWT_PUBLIC_KEY is not configured"):
        decode_access_token("a.b.c", settings)
    assert not issubclass(TokenConfigurationError, AccessTokenError)  # a server problem, not a bad token


@pytest.mark.security
def test_logs_record_issue_and_rejection_but_never_tokens_claims_or_keys(
    user: AuthenticatedUser, token_settings: Settings, jwt_keys: RsaKeyPair, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(token_settings)

    token = issue_access_token(user, token_settings)
    decode_access_token(token, token_settings)
    expired = issue_access_token(user, token_settings, now=datetime.now(UTC) - timedelta(hours=1))
    _rejected(expired, token_settings)
    _rejected("a.b.c", token_settings)
    _rejected(_altered_signature(token), token_settings)
    logs = capsys.readouterr().err

    jti = _header_and_claims(token)[1]["jti"]
    assert f"access token issued user_id={user.user_id} jti={jti}" in logs
    assert "access token rejected reason=ExpiredSignatureError" in logs
    assert "access token rejected reason=DecodeError" in logs
    assert "access token rejected reason=InvalidSignatureError" in logs
    # Nothing secret was even attempted: no redaction was needed.
    assert REDACTED not in logs
    private_key_body = jwt_keys.private_pem.splitlines()[1]
    for value in (token, expired, *token.split(".")[1:], *expired.split(".")[1:], jwt_keys.private_pem,
                  private_key_body, EMAIL, FULL_NAME):
        assert value not in logs
