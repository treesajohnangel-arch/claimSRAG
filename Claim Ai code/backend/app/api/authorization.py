"""HTTP authentication and authorization dependencies. The policy and document API (Phase 2.4) uses them.

- ``current_identity``: the ``TokenIdentity`` from an ``Authorization: Bearer <token>`` header.
  A missing or invalid token → **401**.
- ``permission_required(permission)``: ``current_identity`` plus the RBAC check. A valid token whose role lacks the
  permission → **403**.

The identity comes only from the verified token. Request bodies, query parameters and other headers (a "role",
"permissions" or "company_id" field) are never consulted. Response bodies are generic, and the token is never logged.

These dependencies do not resolve or enforce the tenant: see app/rbac.py.

Usage:
    @router.get("/policies", dependencies=[Depends(permission_required(Permission.POLICY_READ))])

Every route must declare its permission this way unless its path is in ``PUBLIC_PATHS`` (reads) or it is one of the
entries of ``PUBLIC_WRITES`` (sign-in, company registration and the demo session, D-032, D-035, D-036).
``create_app()`` refuses to start otherwise (``routes_missing_permission``), so a route cannot become reachable just
because a developer forgot to declare one.
"""

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.rbac import FORBIDDEN_MESSAGE, AuthorizationError, Permission, require_permission
from app.tokens import GENERIC_TOKEN_FAILURE_MESSAGE, AccessTokenError, TokenIdentity, decode_access_token

NOT_AUTHENTICATED_MESSAGE = "Not authenticated"

# API routes that are deliberately public: service information and the liveness probe, for reads only (GET/HEAD).
# Every other API route must declare a permission. FastAPI's own documentation pages are the only other routes
# allowed; any other route that cannot carry a permission dependency (plain Starlette route, websocket, mounted
# application) is reported as missing one.
PUBLIC_PATHS = frozenset({"/", "/health"})
_PUBLIC_METHODS = frozenset({"GET", "HEAD"})
# The public writes: signing in (D-032), registering a company (D-035) and the development-only demo session (D-036),
# which by definition happen before there is a token. They are served only by app/api/auth.py ``login``, ``register``
# and ``demo_session``, which read no identity or tenant; structural tests pin them. Any other method on these paths, and any other public write, still needs a permission.
PUBLIC_WRITES = frozenset({("/auth/login", "POST"), ("/auth/register-company", "POST"),
                           ("/auth/demo-session", "POST")})

# auto_error=False: a missing, empty or non-Bearer Authorization header yields None, and the 401 below is raised
# here explicitly (older FastAPI versions answered a missing token with 403).
_bearer = HTTPBearer(auto_error=False)


def current_identity(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> TokenIdentity:
    """Authenticate the request from its bearer token, or respond 401."""
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail=NOT_AUTHENTICATED_MESSAGE, headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        # Settings come from the application (create_app stores them), so tokens are verified with its keys.
        return decode_access_token(credentials.credentials, request.app.state.settings)
    except AccessTokenError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_TOKEN_FAILURE_MESSAGE,
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None


def permission_required(permission: Permission) -> Callable[[TokenIdentity], TokenIdentity]:
    """A dependency that authenticates the request (401) and then requires ``permission`` (403)."""
    if not isinstance(permission, Permission):
        # Fail when the route is defined, not on every request: a string here would silently deny everyone.
        raise TypeError("permission_required() takes a Permission member")

    def check(identity: Annotated[TokenIdentity, Depends(current_identity)]) -> TokenIdentity:
        try:
            require_permission(identity, permission)
        except AuthorizationError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_MESSAGE) from None
        return identity

    check.required_permission = permission  # type: ignore[attr-defined]  # read by routes_missing_permission
    return check


def routes_missing_permission(app: FastAPI) -> list[str]:
    """Routes without a declared permission, as "METHODS /path" (or "RouteType /path").

    These are API routes outside the public reads (GET/HEAD on ``PUBLIC_PATHS``) and the public sign-in and
    registration and the demo session (``PUBLIC_WRITES``) with no ``permission_required``
    dependency. Dependencies are found wherever they are declared: on the route, as a parameter, on its router, or in
    ``include_router``. They also include any non-API route other than FastAPI's documentation pages, because such a
    route cannot declare a permission.
    """
    docs = {url for url in (app.openapi_url, app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url) if url}
    missing = []
    for context in iter_route_contexts(app.router.routes):
        methods = context.methods or set()
        label = f"{','.join(sorted(methods)) or type(context.original_route).__name__} {context.path}"
        if not isinstance(context.original_route, APIRoute):
            if not (context.path in docs and methods <= _PUBLIC_METHODS):
                missing.append(label)
            continue
        if context.path in PUBLIC_PATHS and methods <= _PUBLIC_METHODS:
            continue
        if methods and {(context.path, method) for method in methods} <= PUBLIC_WRITES:
            continue
        declared = (getattr(dependency.call, "required_permission", None) for dependency in _all(context.dependant))
        if not any(isinstance(permission, Permission) for permission in declared):
            missing.append(label)
    return missing


def _all(dependant: Dependant) -> Iterator[Dependant]:
    for dependency in dependant.dependencies:
        yield dependency
        yield from _all(dependency)
