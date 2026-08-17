"""Authentication primitives for local development and OIDC deployments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
from anyio import to_thread
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, settings

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[str]
    claims: Mapping[str, Any]

    def has_role(self, role: str) -> bool:
        return role in self.roles


def _unauthorized(detail: str = "Invalid or missing bearer token") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _claim_at_path(claims: Mapping[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _roles_from_claim(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(role for role in value.split() if role)
    if isinstance(value, list) and all(isinstance(role, str) for role in value):
        return frozenset(role.strip() for role in value if role.strip())
    raise ValueError("OIDC roles claim must be a string or a list of strings")


class OIDCVerifier:
    def __init__(self, config: Settings, jwks_client: jwt.PyJWKClient | None = None) -> None:
        self.config = config
        self.jwks_client = jwks_client or jwt.PyJWKClient(
            config.oidc_jwks_url,
            cache_keys=True,
            max_cached_keys=16,
            cache_jwk_set=True,
            lifespan=config.oidc_jwks_cache_seconds,
            timeout=config.oidc_jwks_timeout_seconds,
        )

    def verify(self, token: str) -> Principal:
        signing_key = self.jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=[self.config.oidc_algorithm],
            audience=self.config.oidc_audience,
            issuer=self.config.oidc_issuer,
            leeway=self.config.oidc_token_leeway_seconds,
            options={"require": ["exp", "iat", "sub"]},
        )
        subject = claims.get("sub")
        tenant_id = _claim_at_path(claims, self.config.oidc_tenant_claim)
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
            raise ValueError("OIDC subject claim is missing or invalid")
        if not isinstance(tenant_id, str) or not tenant_id.strip() or len(tenant_id) > 255:
            raise ValueError("OIDC tenant claim is missing or invalid")
        roles = _roles_from_claim(_claim_at_path(claims, self.config.oidc_roles_claim))
        return Principal(
            subject=subject.strip(),
            tenant_id=tenant_id.strip(),
            roles=roles,
            claims=claims,
        )


@lru_cache
def get_oidc_verifier() -> OIDCVerifier:
    return OIDCVerifier(settings)


def local_principal() -> Principal:
    return Principal(
        subject=settings.auth_local_subject.strip(),
        tenant_id=settings.auth_local_tenant.strip(),
        roles=frozenset({settings.auth_admin_role}),
        claims={"auth_mode": "local"},
    )


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if not settings.auth_enabled:
        return local_principal()
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise _unauthorized()
    if len(credentials.credentials) > 16_384:
        raise _unauthorized()
    try:
        return await to_thread.run_sync(
            get_oidc_verifier().verify,
            credentials.credentials,
        )
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity provider signing keys are unavailable",
        ) from exc
    except (jwt.PyJWTError, ValueError) as exc:
        raise _unauthorized() from exc


def require_tenant_admin(principal: Principal) -> None:
    if not principal.has_role(settings.auth_admin_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant administrator role is required",
        )
