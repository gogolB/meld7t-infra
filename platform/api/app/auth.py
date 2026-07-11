"""Authentication and authorization primitives for an offline research deployment.

Two identity sources are supported:

* identity headers from a reverse proxy whose *direct ASGI peer address* is allowlisted; and
* locally configured high-entropy bearer/service tokens for break-glass and machine access.

No forwarded client-address header is used to decide whether a proxy is trusted.  Anonymous access
is denied in every mode unless the explicit development bypass is enabled in development/test.
Route-level policy is expressed with the reusable role dependencies at the end of this module.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Literal

from fastapi import Depends, HTTPException, Request, status
from starlette.responses import Response

from .config import LocalAuthToken, settings


class Role(str, Enum):
    submitter = "submitter"
    reviewer = "reviewer"
    admin = "admin"
    auditor = "auditor"
    service = "service"


AuthMethod = Literal["trusted_proxy", "bearer_token", "service_token", "development_bypass"]
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ROLE_SPLIT_RE = re.compile(r"[\s,]+")


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated request identity passed into API routes."""

    subject: str
    roles: frozenset[Role]
    auth_method: AuthMethod
    request_id: str
    service: bool = False

    @property
    def actor(self) -> str:
        """Stable audit actor string; never accept a body-supplied reviewer as the actor."""
        prefix = "service" if self.service else "user"
        return f"{prefix}:{self.subject}"

    def has_role(self, role: Role, *, admin_override: bool = True) -> bool:
        return role in self.roles or (admin_override and Role.admin in self.roles)


def correlation_id(request: Request) -> str:
    """Return a validated request ID and cache it on ``request.state``.

    Invalid/user-controlled values are replaced instead of reflected into logs or responses.
    """
    existing = getattr(request.state, "correlation_id", None)
    if existing:
        return existing
    supplied = request.headers.get(settings.auth_proxy_request_id_header, "").strip()
    value = supplied if _REQUEST_ID_RE.fullmatch(supplied) else str(uuid.uuid4())
    request.state.correlation_id = value
    return value


async def correlation_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """ASGI HTTP middleware helper that emits the request correlation ID."""
    request_id = correlation_id(request)
    response = await call_next(request)
    response.headers[settings.auth_proxy_request_id_header] = request_id
    return response


def _http_401(detail: str = "authentication required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _peer_is_trusted(request: Request) -> bool:
    if request.client is None or not settings.auth_trusted_proxy_networks:
        return False
    try:
        peer = ipaddress.ip_address(request.client.host.split("%", 1)[0])
    except ValueError:
        return False
    return any(peer in ipaddress.ip_network(network, strict=False)
               for network in settings.auth_trusted_proxy_networks)


def _parse_proxy_roles(raw: str | None) -> frozenset[Role]:
    names = [name.lower() for name in _ROLE_SPLIT_RE.split((raw or "").strip()) if name]
    if not names:
        names = list(settings.auth_proxy_default_roles)
    try:
        roles = frozenset(Role(name) for name in names)
    except ValueError as exc:
        raise _http_401("trusted proxy supplied an invalid role") from exc
    if not roles:
        raise _http_401("trusted proxy supplied no authorized role")
    return roles


def _principal_from_proxy(request: Request, request_id: str) -> Principal | None:
    user_header = settings.auth_proxy_user_header
    supplied_subject = request.headers.get(user_header)
    if supplied_subject is None:
        return None
    if not _peer_is_trusted(request):
        # Do not silently accept/fall through when a caller attempts to assert proxy identity.
        raise _http_401("identity headers are only accepted from a trusted proxy")

    expected_secret = settings.auth_proxy_shared_secret
    if expected_secret is not None:
        supplied_secret = request.headers.get(settings.auth_proxy_secret_header, "")
        if not secrets.compare_digest(
            supplied_secret.encode("utf-8"),
            expected_secret.get_secret_value().encode("utf-8"),
        ):
            raise _http_401("trusted proxy authentication failed")

    subject = supplied_subject.strip()
    if not subject or len(subject) > 128 or any(char in subject for char in "\r\n\0"):
        raise _http_401("trusted proxy supplied an invalid subject")
    roles = _parse_proxy_roles(request.headers.get(settings.auth_proxy_roles_header))
    return Principal(
        subject=subject,
        roles=roles,
        auth_method="trusted_proxy",
        request_id=request_id,
        service=Role.service in roles,
    )


def _find_local_credential(token: str, *, service_only: bool) -> LocalAuthToken | None:
    matched: LocalAuthToken | None = None
    supplied = token.encode("utf-8")
    # Compare every eligible entry rather than returning at the first match.  Token values are
    # unique by settings validation, and this avoids leaking token ordering through timing.
    for credential in settings.auth_local_tokens:
        eligible = credential.service if service_only else True
        expected = credential.token.get_secret_value().encode("utf-8")
        is_match = secrets.compare_digest(supplied, expected)
        if eligible and is_match:
            matched = credential
    return matched


def _principal_from_token(request: Request, request_id: str) -> Principal | None:
    service_value = request.headers.get(settings.auth_service_token_header)
    authorization = request.headers.get("Authorization")
    if service_value is not None and authorization is not None:
        raise _http_401("provide exactly one authentication credential")

    method: Literal["bearer_token", "service_token"]
    service_only = False
    if service_value is not None:
        token = service_value.strip()
        method = "service_token"
        service_only = True
    elif authorization is not None:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            raise _http_401("invalid Authorization scheme")
        token = token.strip()
        method = "bearer_token"
    else:
        return None

    if not token:
        raise _http_401("empty authentication credential")
    credential = _find_local_credential(token, service_only=service_only)
    if credential is None:
        raise _http_401("invalid authentication credential")
    return Principal(
        subject=credential.subject,
        roles=frozenset(Role(role) for role in credential.roles),
        auth_method=method,
        request_id=request_id,
        service=credential.service,
    )


def get_principal(request: Request) -> Principal:
    """Authenticate a request, failing closed unless deliberate dev bypass is active."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached

    request_id = correlation_id(request)
    principal = _principal_from_proxy(request, request_id)
    if principal is None:
        principal = _principal_from_token(request, request_id)
    if principal is None and settings.auth_dev_bypass:
        # Settings validation makes this unreachable in research/production.
        if settings.deployment_mode not in {"development", "test"}:
            raise _http_401()
        principal = Principal(
            subject="development",
            roles=frozenset({Role.submitter, Role.reviewer, Role.admin, Role.auditor}),
            auth_method="development_bypass",
            request_id=request_id,
        )
    if principal is None:
        raise _http_401()
    request.state.principal = principal
    return principal


def require_roles(*required: Role, require_all: bool = False):
    """Create a FastAPI dependency requiring one/all roles.

    Administrators satisfy human-role checks.  Service access is intentionally never implied by
    the admin role; routes that accept machine callers should opt in with ``require_service``.
    """
    if not required:
        raise ValueError("at least one role is required")
    expected = frozenset(required)

    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if Role.service in expected:
            allowed = principal.service and Role.service in principal.roles
            if require_all:
                allowed = allowed and expected.issubset(principal.roles)
        elif Role.admin in principal.roles:
            allowed = True
        elif require_all:
            allowed = expected.issubset(principal.roles)
        else:
            allowed = bool(expected.intersection(principal.roles))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient role",
            )
        return principal

    return dependency


require_submitter = require_roles(Role.submitter)
require_reviewer = require_roles(Role.reviewer)
require_admin = require_roles(Role.admin)
require_auditor = require_roles(Role.auditor)
require_service = require_roles(Role.service)

