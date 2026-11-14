"""Role-based access control: does this authenticated identity's role grant this permission? (D-018)

The role-to-permission mapping below is the only place permissions are granted. Code asks
``require_permission(identity, permission)``; it never compares roles itself.

The identity must be a ``TokenIdentity``, which only ``decode_access_token()`` produces from a validly signed,
unexpired token. Its role is trusted only because that signature was verified. A role, permission list or
company_id taken from request data is never an input here.

Everything else is denied (default deny, fail closed): an unknown or missing role, a role missing from the mapping,
an unknown or empty permission, a plain string instead of a ``Permission``, or anything that is not a
``TokenIdentity``. Nothing falls back to STAFF or COMPANY_ADMIN.

RBAC is not tenant isolation. A COMPANY_ADMIN holds ``company:user:manage`` for "users", but this module does not
decide whether a particular user, policy or document belongs to the admin's company. That is tenant authorization,
a later Phase 1 task.
"""

import logging
from enum import StrEnum
from types import MappingProxyType

from app.models import UserRole
from app.tokens import TokenIdentity

logger = logging.getLogger(__name__)

FORBIDDEN_MESSAGE = "Permission denied"


class Permission(StrEnum):
    """The V1 permission vocabulary, named ``resource:action`` or ``resource:subresource:action``."""

    COMPANY_READ = "company:read"
    COMPANY_USER_READ = "company:user:read"
    COMPANY_USER_MANAGE = "company:user:manage"
    POLICY_READ = "policy:read"
    POLICY_MANAGE = "policy:manage"
    SEARCH_USE = "search:use"
    CHAT_USE = "chat:use"


# Each role's permissions are listed in full, with no inheritance between roles. Immutable at runtime.
ROLE_PERMISSIONS: MappingProxyType[UserRole, frozenset[Permission]] = MappingProxyType(
    {
        UserRole.COMPANY_ADMIN: frozenset(
            {
                Permission.COMPANY_READ,
                Permission.COMPANY_USER_READ,
                Permission.COMPANY_USER_MANAGE,
                Permission.POLICY_READ,
                Permission.POLICY_MANAGE,
                Permission.SEARCH_USE,
                Permission.CHAT_USE,
            }
        ),
        UserRole.STAFF: frozenset(
            {
                Permission.POLICY_READ,
                Permission.SEARCH_USE,
                Permission.CHAT_USE,
            }
        ),
    }
)


class AuthorizationError(Exception):
    """An authenticated identity lacks a permission (HTTP 403). Always the same generic message, with no detail.

    Distinct from authentication failures (``AuthenticationError``, ``AccessTokenError``; HTTP 401).
    """

    def __init__(self) -> None:
        super().__init__(FORBIDDEN_MESSAGE)


def permissions_for(role: object) -> frozenset[Permission]:
    """The permissions a role grants; none for anything that is not a mapped ``UserRole`` member."""
    if not isinstance(role, UserRole):
        return frozenset()
    granted = ROLE_PERMISSIONS.get(role)
    return granted if isinstance(granted, frozenset) else frozenset()


def has_permission(identity: object, permission: object) -> bool:
    """True only for a ``TokenIdentity`` whose role grants this ``Permission`` member."""
    if not isinstance(identity, TokenIdentity) or not isinstance(permission, Permission):
        return False
    return permission in permissions_for(getattr(identity, "role", None))


def require_permission(identity: TokenIdentity, permission: Permission) -> None:
    """Return if the identity's role grants the permission; otherwise raise AuthorizationError."""
    if has_permission(identity, permission):
        return
    logger.info(
        "authorization denied user_id=%s permission=%s",
        getattr(identity, "user_id", None) if isinstance(identity, TokenIdentity) else None,
        permission.value if isinstance(permission, Permission) else "<unknown>",
    )
    raise AuthorizationError
