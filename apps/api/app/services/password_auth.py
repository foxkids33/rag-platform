"""Password authentication with opaque, revocable sessions.

Passwords are scrypt hashes; raw session tokens exist only in HttpOnly cookies.
No local account can choose its subject, tenant, role or session identifier.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from anyio import CapacityLimiter, to_thread
from fastapi import HTTPException, Request, Response
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import AuthSession, AuthUser

if TYPE_CHECKING:
    from app.core.security import Principal

SCRYPT_N = 131_072
SCRYPT_R = 8
SCRYPT_P = 1
PASSWORD_HASH_VERSION = "scrypt-v1"
PASSWORD_THREADS = CapacityLimiter(2)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def cookie_name() -> str:
    return "__Host-rag_session" if settings.auth_cookie_secure else "rag_session"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_for_session(raw_token: str) -> str:
    return hashlib.sha256(b"rag-csrf-v1\0" + raw_token.encode("ascii")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=32, maxmem=256 * 1024 * 1024,
    )
    return "$".join((PASSWORD_HASH_VERSION, base64.b64encode(salt).decode(),
                     base64.b64encode(derived).decode()))


def verify_password(password: str, encoded: str | None) -> bool:
    # Unknown accounts perform the same expensive derivation to avoid a cheap
    # username oracle. Fixed salt here is only for the discarded dummy result.
    salt = b"rag-dummy-salt-v1"
    expected = b"\0" * 32
    valid = False
    if encoded:
        try:
            version, salt_text, digest_text = encoded.split("$")
            decoded_salt = base64.b64decode(salt_text, validate=True)
            decoded_digest = base64.b64decode(digest_text, validate=True)
            if version == PASSWORD_HASH_VERSION and len(decoded_salt) == 16 and len(decoded_digest) == 32:
                salt, expected, valid = decoded_salt, decoded_digest, True
        except (ValueError, TypeError):
            pass
    actual = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=32, maxmem=256 * 1024 * 1024,
    )
    return hmac.compare_digest(actual, expected) and valid


async def password_hash_async(password: str) -> str:
    return await to_thread.run_sync(hash_password, password, limiter=PASSWORD_THREADS)


async def password_matches_async(password: str, encoded: str | None) -> bool:
    return await to_thread.run_sync(verify_password, password, encoded, limiter=PASSWORD_THREADS)


def validate_browser_origin(request: Request) -> None:
    """Fail closed on cross-origin browser writes, including login CSRF."""
    origin = request.headers.get("origin", "")
    expected = settings.web_origin.rstrip("/")
    parsed = urlsplit(origin)
    if (
        origin != expected or parsed.scheme not in {"http", "https"}
        or not parsed.netloc or parsed.username or parsed.password or parsed.path
        or request.headers.get("sec-fetch-site", "").lower() == "cross-site"
    ):
        raise HTTPException(status_code=403, detail="Request origin is not allowed")


def validate_auth_json(request: Request) -> None:
    validate_browser_origin(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Authentication requires application/json")


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


async def consume_rate_limit(
    db: AsyncSession, *, key: str, limit: int, window_seconds: int
) -> None:
    if secrets.randbelow(32) == 0:
        # A bounded, opportunistic cleanup; attackers cannot grow permanent
        # username buckets and this never deletes an active limiting window.
        await db.execute(text("""
            DELETE FROM auth_rate_limits WHERE bucket_key IN (
                SELECT bucket_key FROM auth_rate_limits
                WHERE window_started_at < now() - interval '2 days' LIMIT 100
            )
        """))
    count = await db.scalar(text("""
        INSERT INTO auth_rate_limits (bucket_key, window_started_at, attempts)
        VALUES (:key, now(), 1)
        ON CONFLICT (bucket_key) DO UPDATE SET
            attempts = CASE
                WHEN auth_rate_limits.window_started_at <= now() - make_interval(secs => :window)
                THEN 1 ELSE auth_rate_limits.attempts + 1 END,
            window_started_at = CASE
                WHEN auth_rate_limits.window_started_at <= now() - make_interval(secs => :window)
                THEN now() ELSE auth_rate_limits.window_started_at END
        RETURNING attempts
    """), {"key": _digest(key), "window": window_seconds})
    # Commit even failed attempts. A subsequent auth rollback must not erase limits.
    await db.commit()
    if count is None or count > limit:
        raise HTTPException(status_code=429, detail="Too many attempts; try again later",
                            headers={"Retry-After": str(window_seconds)})


async def limit_auth_request(db: AsyncSession, request: Request, username: str, *, register: bool) -> None:
    # Do not trust an arbitrary X-Forwarded-For header. Reverse-proxy trust is
    # configured at Uvicorn; behind an untrusted proxy this fails conservatively.
    address = request.client.host if request.client else "unknown"
    action = "register" if register else "login"
    await consume_rate_limit(db, key=f"{action}:ip:{address}",
                             limit=10 if register else 60, window_seconds=900)
    if not register:
        await consume_rate_limit(db, key=f"login:username:{username}", limit=15, window_seconds=900)


async def issue_session(
    db: AsyncSession, user: AuthUser, request: Request, response: Response, *, remember: bool = False
) -> str:
    now = datetime.now(UTC)
    old_token = request.cookies.get(cookie_name())
    if old_token and len(old_token) <= 128:
        await db.execute(delete(AuthSession).where(AuthSession.token_hash == _digest(old_token)))
    # Bound storage growth without touching any non-expired session.
    await db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    raw_token = secrets.token_urlsafe(32)
    ttl = settings.auth_remember_session_seconds if remember else settings.auth_session_seconds
    db.add(AuthSession(token_hash=_digest(raw_token), user_id=user.id,
                       expires_at=now + timedelta(seconds=ttl)))
    await db.commit()
    response.set_cookie(cookie_name(), raw_token, max_age=ttl if remember else None,
                        httponly=True, secure=settings.auth_cookie_secure, samesite="lax", path="/")
    no_store(response)
    return csrf_for_session(raw_token)


def principal_for_user(user: AuthUser) -> Principal:
    from app.core.security import Principal

    return Principal(
        subject=user.subject, tenant_id=user.tenant_id,
        roles=frozenset({settings.auth_admin_role}) if user.is_admin else frozenset(),
        claims={"auth_mode": "password", "username": user.username, "display_name": user.display_name},
    )


async def authenticate_session(request: Request, db: AsyncSession) -> Principal:
    from app.core.security import _unauthorized

    raw_token = request.cookies.get(cookie_name(), "")
    if not (32 <= len(raw_token) <= 128) or not raw_token.isascii():
        raise _unauthorized("Authentication required")
    result = await db.execute(
        select(AuthSession, AuthUser).join(AuthUser, AuthUser.id == AuthSession.user_id)
        .where(AuthSession.token_hash == _digest(raw_token),
               AuthSession.expires_at > datetime.now(UTC), AuthUser.is_active.is_(True))
    )
    row = result.first()
    if row is None:
        raise _unauthorized("Authentication required")
    session, user = row
    csrf_token = csrf_for_session(raw_token)
    if request.method.upper() in UNSAFE_METHODS:
        validate_browser_origin(request)
        received = request.headers.get("x-csrf-token", "")
        if not received or not received.isascii() or not hmac.compare_digest(received, csrf_token):
            raise HTTPException(status_code=403, detail="CSRF token is missing or invalid")
    request.state.auth_csrf_token = csrf_token
    request.state.auth_session_hash = session.token_hash
    return principal_for_user(user)


async def revoke_session(db: AsyncSession, request: Request, response: Response) -> None:
    raw_token = request.cookies.get(cookie_name(), "")
    if raw_token and len(raw_token) <= 128:
        await db.execute(delete(AuthSession).where(AuthSession.token_hash == _digest(raw_token)))
        await db.commit()
    response.delete_cookie(cookie_name(), path="/", secure=settings.auth_cookie_secure,
                           httponly=True, samesite="lax")
    no_store(response)
