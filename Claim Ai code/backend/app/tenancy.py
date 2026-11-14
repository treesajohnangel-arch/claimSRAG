"""Tenant resolution: which company does this authenticated identity belong to? (D-019)

The only source of a request's tenant is ``TokenIdentity.company_id``, the company id inside a verified access token
(D-017). ``resolve_tenant()`` turns that identity into a ``TenantIdentity``. It never takes a company id from request
data: a company id in a query string, path, body, header or cookie is never an input.

The responsibility is deliberately narrow. This module does not query the database, check permissions (RBAC is
app/rbac.py), decide whether a resource belongs to the tenant, or filter data. Tenant-aware data access is Phase 1.6.

It fails closed. Anything but a ``TokenIdentity`` holding a real ``UUID`` company id raises
TenantResolutionError. There is no default, first, empty (nil UUID), global or client-supplied fallback tenant.
"""

import logging
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.tokens import TokenIdentity

logger = logging.getLogger(__name__)

TENANT_RESOLUTION_FAILURE_MESSAGE = "Tenant could not be resolved"


class TenantIdentity(BaseModel):
    """The trusted tenant (company) of the current request. Build it only with ``resolve_tenant()``.

    A separate type from a bare UUID, so code that needs the tenant can require one that was resolved from verified
    authentication rather than any company id. It holds no credentials, token or personal data.
    """

    model_config = ConfigDict(frozen=True)

    company_id: UUID

    @field_validator("company_id")
    @classmethod
    def _not_the_nil_uuid(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("company_id must not be the nil UUID")
        return value


class TenantResolutionError(Exception):
    """No trusted tenant could be resolved. Always the same generic message, with no detail."""

    def __init__(self) -> None:
        super().__init__(TENANT_RESOLUTION_FAILURE_MESSAGE)


def resolve_tenant(identity: TokenIdentity) -> TenantIdentity:
    """Return the tenant of a verified identity, or raise TenantResolutionError."""
    company_id = getattr(identity, "company_id", None) if isinstance(identity, TokenIdentity) else None
    if not isinstance(company_id, UUID) or company_id.int == 0:
        logger.warning(
            "tenant resolution failed: no valid company_id user_id=%s",
            getattr(identity, "user_id", None) if isinstance(identity, TokenIdentity) else None,
        )
        raise TenantResolutionError
    return TenantIdentity(company_id=company_id)
