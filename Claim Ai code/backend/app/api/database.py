"""Per-request tenant database access for API routes (Phase 2.4).

    route permission (declared first, D-023) → current_tenant → tenant_database → TenantDatabase(conn, tenant)

``tenant_database`` gives a route the ``TenantDatabase`` of the verified token's tenant, inside one transaction:
committed when the route returns, rolled back when it raises (including an ``HTTPException`` such as a 404 or a
mapped database error). Declare it with ``scope="function"`` (the ``TenantDb`` alias does), so the commit happens
before the response is sent: a client never receives a success for a write that was not committed.

``TenantDatabase`` methods never commit (D-020); this dependency is the only transaction boundary. The connection is
opened only when a route that needs it is resolved, which is after its permission check, so a request denied with
401 or 403 never opens one. Tests replace ``database_connection`` with their own rolled-back connection.
"""

import logging
from collections.abc import Iterator
from typing import Annotated, Any

import psycopg
from fastapi import Depends, HTTPException, Request, status

from app.api.tenancy import current_tenant
from app.db import connect
from app.tenancy import TenantIdentity
from app.tenant_db import TenantDatabase

logger = logging.getLogger(__name__)

DATABASE_UNAVAILABLE = "The service is temporarily unavailable"


def database_connection(request: Request) -> Iterator[psycopg.Connection[Any]]:
    """A database connection for this request, closed when the route finishes.

    If the database cannot be reached (down, or misconfigured: e.g. no POSTGRES_PASSWORD) the request gets a generic
    503 instead of an unexplained 500, and the server log says why. libpq's message names the host, port and reason,
    never the password."""
    try:
        conn = connect(request.app.state.settings)
    except psycopg.OperationalError as exc:
        logger.error("database connection failed error=%s reason=%s", type(exc).__name__,
                     str(exc).strip().splitlines()[0] if str(exc).strip() else "none")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=DATABASE_UNAVAILABLE) from None
    with conn:
        yield conn


def database_transaction(
    conn: Annotated[psycopg.Connection[Any], Depends(database_connection, scope="function")],
) -> Iterator[psycopg.Connection[Any]]:
    """A connection inside one transaction, for the one write that has no tenant yet: company registration
    (D-035). Committed when the route returns, before the response is sent; rolled back if it raises."""
    with conn.transaction():
        yield conn


def tenant_database(
    tenant: Annotated[TenantIdentity, Depends(current_tenant)],
    conn: Annotated[psycopg.Connection[Any], Depends(database_connection, scope="function")],
) -> Iterator[TenantDatabase]:
    """The tenant's database access for one route, in one transaction."""
    with conn.transaction():
        yield TenantDatabase(conn, tenant)


TenantDb = Annotated[TenantDatabase, Depends(tenant_database, scope="function")]
