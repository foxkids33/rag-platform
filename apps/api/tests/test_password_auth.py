from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from app.api.routes.auth import RegisterRequest
from app.core.access import principal_can_access_knowledge_base, principal_can_access_workspace
from app.core.config import Settings, settings
from app.db.models import AuthSession, AuthUser, KnowledgeBase, Workspace
from app.main import app
from app.services.password_auth import (
    authenticate_session,
    consume_rate_limit,
    cookie_name,
    csrf_for_session,
    hash_password,
    issue_session,
    principal_for_user,
    revoke_session,
    validate_auth_json,
    verify_password,
)


def _request(*, token: str | None = None, method: str = "GET", origin: str | None = None,
             csrf: str | None = None, content_type: str = "application/json") -> Request:
    headers = [(b"content-type", content_type.encode())]
    if token is not None:
        headers.append((b"cookie", f"{cookie_name()}={token}".encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if csrf is not None:
        headers.append((b"x-csrf-token", csrf.encode("latin-1")))
    return Request({"type": "http", "method": method, "path": "/api/v1/auth/me",
                    "headers": headers, "client": ("127.0.0.1", 1), "scheme": "https"})


def _user(name: str, *, admin: bool = False) -> AuthUser:
    return AuthUser(id=uuid.uuid4(), username=name, subject=f"user:{name}", tenant_id="local",
                    display_name=name, password_hash="not-a-real-hash", is_admin=admin, is_active=True)


class MemoryAuthDB:
    """Small fake for session primitives; database isolation is tested separately."""

    def __init__(self):
        self.users: dict[uuid.UUID, AuthUser] = {}
        self.sessions: dict[str, AuthSession] = {}
        self.limits: dict[str, int] = {}

    def add(self, item):
        if isinstance(item, AuthSession):
            self.sessions[item.token_hash] = item
        elif isinstance(item, AuthUser):
            self.users[item.id] = item

    async def commit(self):
        pass

    async def execute(self, statement):
        params = statement.compile().params
        if statement.__visit_name__ == "delete":
            if "token_hash_1" in params:
                self.sessions.pop(params["token_hash_1"], None)
            else:
                expired = [key for key, value in self.sessions.items()
                           if value.expires_at <= params["expires_at_1"]]
                for key in expired:
                    self.sessions.pop(key)
            return None
        session = self.sessions.get(params.get("token_hash_1"))
        user = self.users.get(session.user_id) if session else None
        row = (session, user) if session and user and user.is_active and session.expires_at > datetime.now(UTC) else None
        return SimpleNamespace(first=lambda: row)

    async def scalar(self, _statement, params):
        self.limits[params["key"]] = self.limits.get(params["key"], 0) + 1
        return self.limits[params["key"]]


@pytest.fixture
def password_settings(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_mode", "password")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "web_origin", "https://rag.test")


def test_password_hash_is_salted_and_verifies_without_plaintext():
    password = "Correct-example-password-8372"
    hashed = hash_password(password)
    assert password not in hashed
    assert verify_password(password, hashed)
    assert not verify_password("Wrong-password-8372", hashed)
    assert not verify_password(password, None)
    assert not verify_password(password, "malformed")


def test_password_registration_rejects_privilege_fields_and_short_passwords():
    for extra in ({"roles": ["rag-admin"]}, {"tenant_id": "victim"}, {"subject": "local-user"}, {"is_admin": True}):
        with pytest.raises(ValidationError):
            RegisterRequest.model_validate({"username": "alice", "password": "long-enough-secret-12", **extra})
    with pytest.raises(ValidationError):
        RegisterRequest(username="alice", password="short")
    assert RegisterRequest(username="ALICE", password="long-enough-secret-12").username == "alice"


def test_password_mode_requires_tls_in_server_environment():
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(_env_file=None, app_env="server", auth_enabled=True, auth_mode="password",
                 web_origin="http://rag.example", auth_cookie_secure=False)
    config = Settings(_env_file=None, app_env="server", auth_enabled=True, auth_mode="password",
                      web_origin="https://rag.example")
    assert config.auth_cookie_secure


@pytest.mark.parametrize("origin", [None, "null", "https://evil.test", "https://rag.test.evil.test", "https://rag.test/"])
def test_login_registration_reject_cross_origin(password_settings, origin):
    with pytest.raises(HTTPException) as error:
        validate_auth_json(_request(method="POST", origin=origin))
    assert error.value.status_code == 403


def test_login_registration_require_json(password_settings):
    with pytest.raises(HTTPException) as error:
        validate_auth_json(_request(method="POST", origin="https://rag.test", content_type="text/plain"))
    assert error.value.status_code == 415
    validate_auth_json(_request(method="POST", origin="https://rag.test"))


@pytest.mark.asyncio
async def test_session_cookie_hash_csrf_expiry_logout_and_revocation(password_settings):
    db, user = MemoryAuthDB(), _user("alice")
    db.add(user)
    response = Response()
    csrf = await issue_session(db, user, _request(), response)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Path=/" in cookie and "Domain=" not in cookie and "Max-Age=" not in cookie
    assert cookie.startswith("__Host-rag_session=")
    raw_token = cookie.split(";", 1)[0].split("=", 1)[1]
    assert raw_token not in db.sessions
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    assert digest in db.sessions
    assert csrf_for_session(raw_token) == csrf
    principal = await authenticate_session(_request(token=raw_token), db)
    assert principal.subject == user.subject
    for received in (None, "wrong", "é"):
        with pytest.raises(HTTPException) as error:
            await authenticate_session(_request(token=raw_token, method="POST", origin="https://rag.test", csrf=received), db)
        assert error.value.status_code == 403
    await authenticate_session(_request(token=raw_token, method="POST", origin="https://rag.test", csrf=csrf), db)
    with pytest.raises(HTTPException) as error:
        await authenticate_session(_request(token=raw_token, method="POST", origin="https://evil.test", csrf=csrf), db)
    assert error.value.status_code == 403
    user.is_active = False
    with pytest.raises(HTTPException) as error:
        await authenticate_session(_request(token=raw_token), db)
    assert error.value.status_code == 401
    user.is_active = True
    db.sessions[digest].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(HTTPException) as error:
        await authenticate_session(_request(token=raw_token), db)
    assert error.value.status_code == 401
    db.sessions[digest].expires_at = datetime.now(UTC) + timedelta(seconds=60)
    await revoke_session(db, _request(token=raw_token), response)
    with pytest.raises(HTTPException) as error:
        await authenticate_session(_request(token=raw_token), db)
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_login_rotates_existing_session_and_remember_sets_expiry(password_settings):
    db, user = MemoryAuthDB(), _user("alice")
    db.add(user)
    old_token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=hashlib.sha256(old_token.encode()).hexdigest(), user_id=user.id,
                       expires_at=datetime.now(UTC) + timedelta(seconds=60)))
    response = Response()
    await issue_session(db, user, _request(token=old_token), response, remember=True)
    assert hashlib.sha256(old_token.encode()).hexdigest() not in db.sessions
    assert len(db.sessions) == 1
    assert "Max-Age=604800" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_rate_limit_commits_and_blocks_repeated_attempts():
    db = MemoryAuthDB()
    await consume_rate_limit(db, key="login:alice", limit=2, window_seconds=900)
    await consume_rate_limit(db, key="login:alice", limit=2, window_seconds=900)
    with pytest.raises(HTTPException) as error:
        await consume_rate_limit(db, key="login:alice", limit=2, window_seconds=900)
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "900"
    assert "login:alice" not in db.limits


def test_admin_alice_bob_workspace_and_knowledge_base_isolation():
    admin, alice, bob = (principal_for_user(_user(name, admin=name == "admin"))
                         for name in ("admin", "alice", "bob"))
    a_workspace = Workspace(tenant_id="local", user_id=alice.subject, name="private-a")
    b_workspace = Workspace(tenant_id="local", user_id=bob.subject, name="private-b")
    legacy_kb = KnowledgeBase(tenant_id="local", name="corporate", slug="legacy", owner_subject=None)
    private_kb = KnowledgeBase(tenant_id="local", name="private", slug="alice", owner_subject=alice.subject)
    assert principal_can_access_workspace(alice, a_workspace)
    assert not principal_can_access_workspace(bob, a_workspace)
    assert not principal_can_access_workspace(alice, b_workspace)
    assert principal_can_access_workspace(admin, a_workspace)
    assert principal_can_access_workspace(admin, b_workspace)
    assert principal_can_access_knowledge_base(admin, legacy_kb)
    assert principal_can_access_knowledge_base(admin, private_kb)
    assert principal_can_access_knowledge_base(alice, private_kb)
    assert not principal_can_access_knowledge_base(bob, private_kb)
    assert not principal_can_access_knowledge_base(alice, legacy_kb)


@pytest.mark.asyncio
async def test_password_mode_rejects_anonymous_requests_before_database(password_settings):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://rag.test") as client:
        for path in ("/api/v1/auth/me", "/api/v1/workspaces", "/api/v1/knowledge-bases"):
            assert (await client.get(path)).status_code == 401
