"""Route-level security structure (Phase 1.8).

1. Regression tests for the startup permission guard (``routes_missing_permission``).

The Phase 1.7 guard exempted ``/`` and ``/health`` by path for every method, and skipped every non-API route. So
an unprotected ``POST /health``, ``DELETE /``, plain Starlette route, websocket or mounted application let the app
start with a route that declared no permission. Now only GET/HEAD on the public paths and FastAPI's own
documentation pages are exempt; everything else must declare ``permission_required``.

2. No route of the real application accepts a client parameter named like a tenant or role field, in any transport.

Phase 1.9 adds:

3. Authorization runs before any other work. FastAPI resolves a route's dependencies in declaration order, so a
   dependency declared before ``permission_required`` (for example one that loads or changes data) would run even
   for a request that is then denied. No route may resolve anything but authentication and tenant resolution before
   its permission check.
4. The application the server runs (``app.main:app``) is checked, not only fresh ``create_app()`` instances, and its
   public paths are served only by the two dependency-free public endpoints.
"""

import uuid
from collections.abc import Callable, Iterator
from typing import Annotated

import pytest
from fastapi import APIRouter, Body, Cookie, Depends, FastAPI, Header, WebSocket
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

import app.main as main
from app.api.auth import demo_session, login, register
from app.api.authorization import (PUBLIC_PATHS, PUBLIC_WRITES, current_identity, permission_required,
                                   routes_missing_permission)
from app.api.database import TenantDb, database_connection, database_transaction, tenant_database
from app.api.tenancy import current_tenant
from app.auth import AuthenticatedUser
from app.config import Settings
from app.main import app as served_app
from app.main import create_app
from app.models import UserRole, UserStatus
from app.rbac import Permission
from app.tenancy import TenantIdentity
from app.tokens import TokenIdentity, issue_access_token
from tests.conftest import RsaKeyPair


def _health_router_with(extra: Callable[[APIRouter], None]) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    extra(router)
    return router


def _post_health(router: APIRouter) -> None:
    @router.post("/health")
    def change_health() -> dict[str, str]:
        return {}


def _delete_root(router: APIRouter) -> None:
    @router.delete("/")
    def wipe() -> dict[str, str]:
        return {}


def _get_login(router: APIRouter) -> None:
    @router.get("/auth/login")
    def login_page() -> dict[str, str]:
        return {}


def _delete_login(router: APIRouter) -> None:
    @router.delete("/auth/login")
    def forget_login() -> dict[str, str]:
        return {}


def _post_beside_login(router: APIRouter) -> None:
    @router.post("/auth/register")
    def register() -> dict[str, str]:
        return {}


def _starlette_route(router: APIRouter) -> None:
    async def plain(request: Request) -> JSONResponse:
        return JSONResponse({"secret": "data"})

    router.add_route("/plain", plain)


def _websocket(router: APIRouter) -> None:
    @router.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()


@pytest.mark.security
@pytest.mark.parametrize(
    ("extra", "reported"),
    [
        (_post_health, "POST /health"),
        (_delete_root, "DELETE /"),
        (_starlette_route, "GET,HEAD /plain"),
        (_websocket, "APIWebSocketRoute /ws"),
        (_get_login, "GET /auth/login"),
        (_delete_login, "DELETE /auth/login"),
        (_post_beside_login, "POST /auth/register"),
    ],
    ids=["write-on-public-path", "delete-on-public-path", "plain-starlette-route", "websocket", "read-on-login-path",
         "delete-on-login-path", "write-beside-login"],
)
def test_create_app_refuses_routes_that_bypassed_the_phase_1_7_guard(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, extra: Callable[[APIRouter], None], reported: str
) -> None:
    monkeypatch.setattr(main, "health_router", _health_router_with(extra))

    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings)

    assert reported in str(excinfo.value)


@pytest.mark.security
def test_mounted_application_is_reported(settings: Settings) -> None:
    app = create_app(settings)
    inner = FastAPI()

    @inner.get("/inner")
    def inner_route() -> dict[str, str]:
        return {"secret": "data"}

    app.mount("/mounted", inner)

    assert routes_missing_permission(app) == ["Mount /mounted"]


def test_public_reads_and_documentation_pages_stay_allowed(settings: Settings) -> None:
    app = create_app(settings)

    assert routes_missing_permission(app) == []
    assert {"/openapi.json", "/docs", "/redoc"} <= {getattr(route, "path", "") for route in app.routes}


@pytest.mark.security
def test_write_on_a_public_path_is_allowed_only_with_a_declared_permission(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def protected_post(router: APIRouter) -> None:
        @router.post("/health", dependencies=[Depends(permission_required(Permission.COMPANY_READ))])
        def change_health() -> dict[str, str]:
            return {}

    monkeypatch.setattr(main, "health_router", _health_router_with(protected_post))

    assert routes_missing_permission(create_app(settings)) == []



_FORBIDDEN_PARAMETERS = {"company_id", "tenant_id", "role", "roles", "permission", "permissions"}


def _walk(dependant: Dependant) -> Iterator[Dependant]:
    yield dependant
    for sub in dependant.dependencies:
        yield from _walk(sub)


def _client_tenant_or_role_parameters(app: FastAPI) -> list[str]:
    """Client parameters (path, query, header, cookie, body), anywhere in a route's dependency tree, named like a
    tenant or role field."""
    found = []
    for context in iter_route_contexts(app.router.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        for dependant in _walk(context.dependant):
            for param in (*dependant.path_params, *dependant.query_params, *dependant.header_params,
                          *dependant.cookie_params, *dependant.body_params):
                names = {param.name.lower(), (param.alias or "").lower().replace("-", "_")}
                if names & _FORBIDDEN_PARAMETERS or any(n.startswith("x_") and n[2:] in _FORBIDDEN_PARAMETERS
                                                           for n in names):
                    found.append(f"{context.path}:{param.alias or param.name}")
    return found


@pytest.mark.security
def test_no_real_route_accepts_a_client_tenant_or_role_parameter(settings: Settings) -> None:
    # Tenant and role come only from the verified token (D-019, D-021). A parameter with one of these names would
    # invite a client value into authorization.
    assert _client_tenant_or_role_parameters(create_app(settings)) == []


def test_client_tenant_or_role_parameter_check_finds_every_transport() -> None:
    app = FastAPI()

    def sneaky_dependency(role: str | None = None) -> None: ...

    @app.post("/t/{company_id}", dependencies=[Depends(sneaky_dependency)])
    def offender(
        company_id: str,
        permissions: str | None = None,
        x_role: Annotated[str | None, Header()] = None,
        tenant_id: Annotated[str | None, Cookie()] = None,
        payload: Annotated[dict[str, str] | None, Body(alias="company_id")] = None,
    ) -> None: ...

    @app.get("/clean/{user_id}")
    def clean(user_id: str, q: str | None = None) -> None: ...

    assert sorted(_client_tenant_or_role_parameters(app)) == sorted(
        ["/t/{company_id}:company_id", "/t/{company_id}:permissions", "/t/{company_id}:x-role",
         "/t/{company_id}:tenant_id", "/t/{company_id}:company_id", "/t/{company_id}:role"]
    )


# --- 3. Authorization runs before any other work (Phase 1.9) -----------------------------------------------------

# Authentication and tenant resolution may be resolved before the permission check: they only verify the token and
# read its company id. Anything else must come after it.
_AUTHENTICATION_AND_TENANT = {dependant.call for dependant in _walk(get_dependant(path="", call=current_tenant))}


def _resolved_before_permission(dependant: Dependant, before: list[str]) -> bool:
    """Append to ``before`` what FastAPI resolves ahead of the first permission check; True once one is found.

    FastAPI resolves a dependant's dependencies in declaration order, each one after its own dependencies.
    """
    for sub in dependant.dependencies:
        if _resolved_before_permission(sub, before) or isinstance(getattr(sub.call, "required_permission", None),
                                                                  Permission):
            return True
        if sub.call not in _AUTHENTICATION_AND_TENANT:
            before.append(getattr(sub.call, "__name__", type(sub.call).__name__))
    return False


def _work_before_permission_check(app: FastAPI) -> list[str]:
    """Dependencies of protected routes that run before the permission check, other than authentication and tenant
    resolution, as "path:dependency". They would run even for a request that is then denied."""
    found = []
    for context in iter_route_contexts(app.router.routes):
        before: list[str] = []
        if isinstance(context.original_route, APIRoute) and _resolved_before_permission(context.dependant, before):
            found += [f"{context.path}:{name}" for name in before]
    return found


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


def _bearer(settings: Settings, role: UserRole) -> dict[str, str]:
    user = AuthenticatedUser(user_id=uuid.uuid4(), company_id=uuid.uuid4(), email="user@acme.example",
                             full_name="Test User", role=role, status=UserStatus.ACTIVE)
    return {"Authorization": f"Bearer {issue_access_token(user, settings)}"}


@pytest.mark.security
def test_permission_check_runs_before_other_dependencies_and_the_order_check_finds_each_exception(
    token_settings: Settings,
) -> None:
    ran: list[str] = []
    admin_only = Permission.COMPANY_USER_MANAGE

    def load_data(tenant: Annotated[TenantIdentity, Depends(current_tenant)]) -> None:
        ran.append("load_data")  # stands in for a dependency that reads or changes the tenant's data

    Permitted = Annotated[TokenIdentity, Depends(permission_required(admin_only))]
    Data = Annotated[None, Depends(load_data)]
    Tenant = Annotated[TenantIdentity, Depends(current_tenant)]

    def wrapper(data: Data, identity: Permitted) -> None: ...

    app = create_app(token_settings)
    router = APIRouter(dependencies=[Depends(permission_required(admin_only))])

    @app.post("/route-level", dependencies=[Depends(permission_required(admin_only))])
    def route_level(data: Data) -> None: ...

    @router.post("/router-level")
    def router_level(data: Data) -> None: ...

    @app.post("/first-parameter")
    def first_parameter(identity: Permitted, data: Data) -> None: ...

    @app.post("/after-tenant")
    def after_tenant(tenant: Tenant, identity: Permitted, data: Data) -> None: ...

    @app.post("/permission-last")
    def permission_last(data: Data, identity: Permitted) -> None: ...

    @app.post("/nested")
    def nested(_: Annotated[None, Depends(wrapper)]) -> None: ...

    app.include_router(router)
    requests = {"anonymous": {}, "staff": _bearer(token_settings, UserRole.STAFF),
                "admin": _bearer(token_settings, UserRole.COMPANY_ADMIN)}
    observed = {}
    with TestClient(app) as client:
        for path in ("/route-level", "/router-level", "/first-parameter", "/after-tenant", "/permission-last", "/nested"):
            for who, headers in requests.items():
                ran.clear()
                observed[path, who] = (client.post(path, headers=headers).status_code, list(ran))

    for path in ("/route-level", "/router-level", "/first-parameter", "/after-tenant"):
        assert [observed[path, who] for who in requests] == [(401, []), (403, []), (200, ["load_data"])], path
    # Positive control: declared after a data dependency, the permission check comes too late. The token is still
    # verified first (load_data needs the tenant), but a denied STAFF request has already run load_data.
    for path in ("/permission-last", "/nested"):
        assert [observed[path, who] for who in requests] == [(401, []), (403, ["load_data"]), (200, ["load_data"])]
    assert sorted(_work_before_permission_check(app)) == ["/nested:load_data", "/permission-last:load_data"]


@pytest.mark.security
def test_no_real_route_resolves_other_work_before_its_permission_check(settings: Settings) -> None:
    for app in (create_app(settings), served_app):
        assert _work_before_permission_check(app) == []


# --- 4. The served application and its public paths (Phase 1.9) --------------------------------------------------


def _route_table(app: FastAPI) -> set[tuple[str, str]]:
    return {(context.path, ",".join(sorted(context.methods or ())) or type(context.original_route).__name__)
            for context in iter_route_contexts(app.router.routes)}


@pytest.mark.security
def test_served_application_is_checked_like_create_app(settings: Settings) -> None:
    # The guard runs inside create_app(). A route added to the module-level app afterwards (for example below
    # `app = create_app()` in app/main.py) would never pass through it, so the served app is checked here.
    assert _route_table(served_app) == _route_table(create_app(settings))
    assert routes_missing_permission(served_app) == []
    assert _client_tenant_or_role_parameters(served_app) == []


@pytest.mark.security
def test_public_paths_are_served_only_by_the_dependency_free_public_endpoints(settings: Settings) -> None:
    # GET/HEAD on a public path is exempt from the guard, so nothing else may answer there: no second route
    # shadowing a public endpoint, and no dependency (identity, tenant, database) behind one.
    def describe(context: RouteContext) -> tuple[str, str, str, int]:
        """Path, methods, endpoint, and how many dependencies and client inputs it has."""
        dependants = list(_walk(context.dependant))
        inputs = sum(len(d.path_params) + len(d.query_params) + len(d.header_params) + len(d.cookie_params)
                     + len(d.body_params) for d in dependants)
        endpoint = getattr(context.original_route, "endpoint", None)
        return (context.path, ",".join(sorted(context.methods or ())), getattr(endpoint, "__name__", ""),
                len(dependants) - 1 + inputs)

    for app in (create_app(settings), served_app):
        public = sorted(describe(context) for context in iter_route_contexts(app.router.routes)
                        if context.path in PUBLIC_PATHS)
        assert public == [("/", "GET", "service_info", 0), ("/health", "GET", "health", 0)]


# --- 5. The policy and document API (Phase 2.4) ----------------------------------------------------------------

_READ, _MANAGE = Permission.POLICY_READ, Permission.POLICY_MANAGE
POLICY_API = {
    ("/policies", "GET"): _READ,
    ("/policies", "POST"): _MANAGE,
    ("/policies/{policy_id}", "GET"): _READ,
    ("/policies/{policy_id}", "PATCH"): _MANAGE,
    ("/policies/{policy_id}", "DELETE"): _MANAGE,
    ("/policies/{policy_id}/documents", "GET"): _READ,
    ("/policies/{policy_id}/documents", "POST"): _MANAGE,
    ("/documents/{document_id}", "GET"): _READ,
    ("/documents/{document_id}", "PATCH"): _MANAGE,
    ("/documents/{document_id}", "DELETE"): _MANAGE,
    ("/documents/{document_id}/status", "PUT"): _MANAGE,
    # D-035: the PDF upload, which indexes the file in the tenant's transaction.
    ("/policies/{policy_id}/documents/upload", "POST"): _MANAGE,
}
# Phase 6 added the search route (search:use) and Phase 8 the chat route (chat:use); there are no others.
PROTECTED_ROUTES = {**POLICY_API, ("/search", "POST"): Permission.SEARCH_USE, ("/chat", "POST"): Permission.CHAT_USE}
# Never a field of a request body: the tenant, role and permissions come from the token, ids and the parent policy
# from the path, and the storage reference from the server.
_UNTRUSTED_BODY_FIELDS = _FORBIDDEN_PARAMETERS | {"id", "policy_id", "storage_reference"}


_SIGN_IN_PATHS = {path for path, _ in PUBLIC_WRITES}


def _protected_routes(app: FastAPI) -> Iterator[RouteContext]:
    for context in iter_route_contexts(app.router.routes):
        if (isinstance(context.original_route, APIRoute) and context.path not in PUBLIC_PATHS
                and context.path not in _SIGN_IN_PATHS):
            yield context


# The public writes (D-032, D-035, D-036): each path, its one endpoint, the only dependencies it may resolve, and its
# body's fields (None: no body at all).
_PUBLIC_WRITE_ROUTES: dict[str, tuple[Callable[..., object], set[Callable[..., object]], set[str] | None]] = {
    "/auth/login": (login, {database_connection}, {"company_slug", "email", "password"}),
    "/auth/register-company": (register, {database_transaction, database_connection},
                               {"company_name", "company_slug", "admin_full_name", "admin_email", "admin_password"}),
    # The demo session takes no input at all: its user and company are server settings (D-036).
    "/auth/demo-session": (demo_session, {database_connection}, None),
}


@pytest.mark.security
def test_the_only_public_writes_are_sign_in_and_registration_with_no_identity_tenant_or_client_tenant_behind_them(
    settings: Settings,
) -> None:
    # Sign-in (D-032), company registration (D-035) and the demo session (D-036) are exempt from the permission guard
    # because they run before there is a token. So they must be exactly these three routes, reading only a database
    # connection (registration's in a transaction), with a body of their own fields (the demo session has none):
    # never a company id, tenant, role or permission.
    assert PUBLIC_WRITES == {("/auth/login", "POST"), ("/auth/register-company", "POST"),
                             ("/auth/demo-session", "POST")}
    for app in (create_app(settings), served_app):
        public = [context for context in iter_route_contexts(app.router.routes) if context.path in _SIGN_IN_PATHS]
        assert sorted((c.path, sorted(c.methods or ()), getattr(c.original_route, "endpoint", None)) for c in public) \
            == sorted((path, ["POST"], endpoint) for path, (endpoint, _, _) in _PUBLIC_WRITE_ROUTES.items())
        for context in public:
            endpoint, allowed, fields = _PUBLIC_WRITE_ROUTES[context.path]
            calls = {dependant.call for dependant in _walk(context.dependant)} - {endpoint}
            assert calls == allowed, context.path  # no identity, permission, tenant or tenant database
            dependants = list(_walk(context.dependant))
            assert not any(d.path_params or d.query_params or d.header_params or d.cookie_params for d in dependants)
            bodies = [param for d in dependants for param in d.body_params]
            if fields is None:
                assert bodies == [], context.path
                continue
            (body,) = bodies
            model = body.field_info.annotation
            assert model.model_config.get("extra") == "forbid"
            assert set(model.model_fields) == fields
            assert not {name.lower() for name in model.model_fields} & (_UNTRUSTED_BODY_FIELDS | {"role", "status"})


@pytest.mark.security
def test_every_policy_and_document_route_is_served_with_its_permission_declared_first(settings: Settings) -> None:
    for app in (create_app(settings), served_app):
        found = {}
        for context in _protected_routes(app):
            before: list[str] = []
            declared = {getattr(d.call, "required_permission", None) for d in _walk(context.dependant)} - {None}
            assert _resolved_before_permission(context.dependant, before) and before == [], context.path
            found |= {(context.path, method): declared for method in context.methods or ()}

        # Exactly these routes are protected, each with exactly its own permission: policy reads need policy:read,
        # every change needs policy:manage, search needs search:use and chat needs chat:use.
        assert found == {route: {permission} for route, permission in PROTECTED_ROUTES.items()}


@pytest.mark.security
def test_every_policy_and_document_route_reaches_data_only_through_the_verified_tenant(settings: Settings) -> None:
    for context in _protected_routes(create_app(settings)):
        calls = {dependant.call for dependant in _walk(context.dependant)}
        scopes = {dependant.scope for dependant in _walk(context.dependant)
                  if dependant.call in (tenant_database, database_connection)}

        assert {current_identity, current_tenant, tenant_database, database_connection} <= calls, context.path
        # "function" scope: the transaction ends (commit or rollback) before the response is sent.
        assert scopes == {"function"}, context.path


@pytest.mark.security
def test_request_bodies_reject_unknown_fields_and_carry_no_tenant_role_or_storage_field(settings: Settings) -> None:
    models = {}
    for context in _protected_routes(create_app(settings)):
        for dependant in _walk(context.dependant):
            for param in dependant.body_params:
                models[context.path, ",".join(sorted(context.methods or ()))] = param.field_info.annotation

    # POST /policies, PATCH /policies/{id}, POST .../documents, PATCH and PUT on documents, POST /search, POST /chat,
    # and the upload (D-035), whose body is the raw PDF: bytes, not a model, so it can carry no field at all.
    assert len(models) == 8
    assert models.pop(("/policies/{policy_id}/documents/upload", "POST")) is bytes
    search = models.pop(("/search", "POST"))
    chat = models.pop(("/chat", "POST"))
    for route, model in models.items():
        assert model.model_config.get("extra") == "forbid", route  # an unknown field (company_id, ...) is a 422
        assert not {name.lower() for name in model.model_fields} & _UNTRUSTED_BODY_FIELDS, route
    # The search body may narrow by policy and document (only within the tenant, in SQL), and nothing else.
    filters = search.model_fields["filters"].annotation
    assert search.model_config.get("extra") == filters.model_config.get("extra") == "forbid"
    assert set(search.model_fields) == {"query", "top_k", "filters"}
    # The chat body is a question and the same filters: never evidence, a model, a prompt or a tenant.
    assert chat.model_config.get("extra") == "forbid" and chat.model_fields["filters"].annotation is filters
    assert set(chat.model_fields) == {"question", "top_k", "filters"}
    assert set(filters.model_fields) == {"policy_id", "document_id", "insurance_type"}
    assert not {name.lower() for name in (*search.model_fields, *filters.model_fields)} & (
        _FORBIDDEN_PARAMETERS | {"id", "storage_reference"})


@pytest.mark.security
def test_create_app_refuses_a_tenant_route_that_forgot_its_permission(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A route that authenticates and resolves the tenant (through TenantDb) but declares no permission is still
    # refused at startup: tenant scoping is not authorization.
    router = APIRouter()

    @router.get("/policies")
    def list_policies(db: TenantDb) -> list[str]:
        return []

    monkeypatch.setattr(main, "policies_router", router)

    with pytest.raises(RuntimeError, match=r"GET /policies"):
        create_app(settings)
