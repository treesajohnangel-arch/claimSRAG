"""HTTP tenant resolution dependency. The policy and document API (Phase 2.4) uses it, through
app/api/database.py ``tenant_database``.

    Authorization header → current_identity (401 if missing or invalid) → TokenIdentity → current_tenant → TenantIdentity

``current_tenant`` takes the identity only from ``current_identity`` (app/api/authorization.py). It does not read
headers or decode the token again; FastAPI runs ``current_identity`` once per request even when a route also uses
``permission_required``. It never reads a company id from the query string, path, body, headers or cookies, so the
client cannot select the tenant.

It does not check permissions (``permission_required``) or resource ownership (Phase 1.6 and later).

Usage:
    def route(tenant: Annotated[TenantIdentity, Depends(current_tenant)]): ...
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.api.authorization import current_identity
from app.tenancy import TenantIdentity, TenantResolutionError, resolve_tenant
from app.tokens import GENERIC_TOKEN_FAILURE_MESSAGE, TokenIdentity


def current_tenant(identity: Annotated[TokenIdentity, Depends(current_identity)]) -> TenantIdentity:
    """The trusted tenant of an authenticated request.

    A verified token always carries a valid company id, so failure means a token this service would never issue
    (for example, a nil company id). It fails closed as an invalid credential: 401, with the same generic response
    as any invalid token.
    """
    try:
        return resolve_tenant(identity)
    except TenantResolutionError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_TOKEN_FAILURE_MESSAGE,
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
