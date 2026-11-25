"""Real SQL/session/IDOR proof against an explicitly named disposable database.

Run migrations against PASSWORD_TEST_DATABASE_URL first. This test never uses
the application's DATABASE_URL as its database target and refuses other names.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.models import (
    AuthSession,
    AuthUser,
    ChatMessage,
    ChatSession,
    Document,
    DocumentChunk,
    KnowledgeBase,
    Workspace,
)
from app.db.session import get_db
from app.main import app
from app.services.password_auth import cookie_name, password_hash_async

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PASSWORD_DATABASE_TESTS") != "1",
    reason="requires an explicitly named, migrated disposable password-auth database",
)


@pytest.mark.asyncio
async def test_password_accounts_sessions_and_sql_resource_isolation(monkeypatch):
    url = os.getenv("PASSWORD_TEST_DATABASE_URL", "")
    if not url or not (make_url(url).database or "").startswith("rag_auth_test_"):
        pytest.fail("PASSWORD_TEST_DATABASE_URL must explicitly target rag_auth_test_*; refusing default/production")
    engine = create_async_engine(url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex[:12]
    tenant, origin = f"password-test:{nonce}", "https://rag.test"
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_mode", "password")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "auth_local_tenant", tenant)
    monkeypatch.setattr(settings, "auth_local_subject", f"legacy-{nonce}")
    monkeypatch.setattr(settings, "web_origin", origin)
    password = "Integration-example-password-83"
    usernames = [f"admin-{nonce}", f"alice-{nonce}", f"bob-{nonce}"]
    admin = AuthUser(
        id=uuid.uuid4(), username=usernames[0], subject=settings.auth_local_subject,
        tenant_id=tenant, display_name="Test admin", password_hash=await password_hash_async(password),
        is_admin=True, is_active=True,
    )
    legacy_workspace = Workspace(id=uuid.uuid4(), tenant_id=tenant, user_id=settings.auth_local_subject,
                                 name="Legacy documents", source_mode="USER_DOCUMENTS")
    legacy_kb = KnowledgeBase(id=uuid.uuid4(), tenant_id=tenant, owner_subject=None,
                             slug=f"legacy-{nonce}", name="Legacy private KB", graph_enabled=False)
    owned_workspace_ids = [legacy_workspace.id]
    owned_kb_ids = [legacy_kb.id]

    async def database_dependency():
        async with session_factory() as db:
            yield db

    previous_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = database_dependency
    try:
        async with session_factory() as db:
            db.add_all([admin, legacy_workspace, legacy_kb])
            await db.commit()
        transport = httpx.ASGITransport(app=app, client=(f"test-{nonce}", 1234))
        async with (
            httpx.AsyncClient(transport=transport, base_url=origin) as a,
            httpx.AsyncClient(transport=transport, base_url=origin) as b,
            httpx.AsyncClient(transport=transport, base_url=origin) as administrator,
            httpx.AsyncClient(transport=transport, base_url=origin) as anonymous,
        ):
            for path in ("/api/v1/workspaces", "/api/v1/knowledge-bases", "/api/v1/auth/me"):
                assert (await anonymous.get(path)).status_code == 401
            assert (await anonymous.post("/api/v1/auth/register", json={
                "username": f"forged-{nonce}", "password": password, "is_admin": True,
            }, headers={"Origin": origin})).status_code == 422
            assert (await anonymous.post("/api/v1/auth/register", json={
                "username": f"cross-{nonce}", "password": password,
            }, headers={"Origin": "https://evil.example"})).status_code == 403
            registration_a = await a.post("/api/v1/auth/register", json={
                "username": usernames[1], "password": password,
            }, headers={"Origin": origin})
            registration_b = await b.post("/api/v1/auth/register", json={
                "username": usernames[2], "password": password,
            }, headers={"Origin": origin})
            assert registration_a.status_code == 201, registration_a.text
            assert registration_b.status_code == 201, registration_b.text
            assert registration_a.json()["roles"] == registration_b.json()["roles"] == []
            assert registration_a.json()["subject"] != registration_b.json()["subject"]
            a_headers = {"Origin": origin, "X-CSRF-Token": registration_a.json()["csrf_token"]}
            b_headers = {"Origin": origin, "X-CSRF-Token": registration_b.json()["csrf_token"]}
            admin_login = await administrator.post("/api/v1/auth/login", json={
                "username": usernames[0], "password": password,
            }, headers={"Origin": origin})
            assert admin_login.status_code == 200, admin_login.text
            assert settings.auth_admin_role in admin_login.json()["roles"]
            assert (await a.get("/api/v1/workspaces")).json() == []
            assert (await b.get("/api/v1/knowledge-bases")).json() == []
            assert (await a.post("/api/v1/workspaces", json={"name": "No CSRF"},
                                 headers={"Origin": origin})).status_code == 403
            a_ws_response = await a.post("/api/v1/workspaces", json={"name": "Alice private"}, headers=a_headers)
            b_ws_response = await b.post("/api/v1/workspaces", json={"name": "Bob private"}, headers=b_headers)
            assert a_ws_response.status_code == b_ws_response.status_code == 201
            a_ws, b_ws = a_ws_response.json()["id"], b_ws_response.json()["id"]
            owned_workspace_ids.extend([uuid.UUID(a_ws), uuid.UUID(b_ws)])
            assert {row["id"] for row in (await a.get("/api/v1/workspaces")).json()} == {a_ws}
            assert {row["id"] for row in (await b.get("/api/v1/workspaces")).json()} == {b_ws}
            assert {a_ws, b_ws, str(legacy_workspace.id)} <= {
                row["id"] for row in (await administrator.get("/api/v1/workspaces")).json()
            }
            # Seed synthetic source records only: object storage/model services
            # are deliberately not called by this security integration test.
            a_document = Document(id=uuid.uuid4(), workspace_id=uuid.UUID(a_ws), filename="alice.txt",
                                  object_key=f"auth-test/{nonce}/alice.txt", mime_type="text/plain",
                                  sha256="a" * 64, status="READY", search_enabled=True)
            a_conversation = ChatSession(id=uuid.uuid4(), workspace_id=uuid.UUID(a_ws), title="Private chat")
            async with session_factory() as db:
                db.add_all([a_document, a_conversation])
                await db.flush()
                db.add(ChatMessage(id=uuid.uuid4(), session_id=a_conversation.id,
                                   role="assistant", content="Alice-only private answer"))
                db.add(DocumentChunk(id=uuid.uuid4(), document_id=a_document.id, chunk_index=0,
                                     text="ALICEDOC synthetic private evidence", normalized_text="alicedocsyntheticprivateevidence"))
                await db.commit()
            assert len((await a.get(f"/api/v1/workspaces/{a_ws}/documents")).json()) == 1
            assert (await a.get(f"/api/v1/workspaces/{a_ws}/conversations/{a_conversation.id}")).json()["messages"][0]["content"] == "Alice-only private answer"
            for path in (
                f"/api/v1/workspaces/{a_ws}/documents",
                f"/api/v1/workspaces/{a_ws}/conversations",
                f"/api/v1/workspaces/{a_ws}/conversations/{a_conversation.id}",
            ):
                assert (await b.get(path)).status_code == 404
                assert (await administrator.get(path)).status_code == 200
            assert (await b.get(f"/api/v1/workspaces/{b_ws}/conversations/{a_conversation.id}")).status_code == 404
            assert (await b.post(f"/api/v1/workspaces/{a_ws}/search", json={
                "query": "ALICEDOC", "mode": "lexical", "rerank": False,
            }, headers=b_headers)).status_code == 404
            assert (await a.post(f"/api/v1/workspaces/{a_ws}/search", json={
                "query": "ALICEDOC", "mode": "lexical", "rerank": False,
            }, headers=a_headers)).status_code == 200
            assert (await b.patch(f"/api/v1/workspaces/{a_ws}", json={"name": "Hijacked"}, headers=b_headers)).status_code == 404
            assert (await b.delete(f"/api/v1/workspaces/{a_ws}", headers=b_headers)).status_code == 404
            assert (await a.post("/api/v1/workspaces", json={"name": "Steal KB", "base_knowledge_base_id": str(legacy_kb.id)}, headers=a_headers)).status_code == 404
            a_kb_response = await a.post("/api/v1/knowledge-bases", json={"name": "Alice KB", "slug": f"alice-{nonce}"}, headers=a_headers)
            assert a_kb_response.status_code == 201, a_kb_response.text
            a_kb_id = a_kb_response.json()["id"]
            owned_kb_ids.append(uuid.UUID(a_kb_id))
            assert {row["id"] for row in (await a.get("/api/v1/knowledge-bases")).json()} == {a_kb_id}
            assert (await b.get(f"/api/v1/knowledge-bases/{a_kb_id}/versions")).status_code == 404
            assert (await a.get(f"/api/v1/knowledge-bases/{legacy_kb.id}/versions")).status_code == 404
            assert (await administrator.get(f"/api/v1/knowledge-bases/{a_kb_id}/versions")).status_code == 200
            # Existing connector service credentials are not exposed to a newly
            # registered local user through global Confluence browsing.
            assert (await a.get("/api/v1/integrations/confluence/connector")).status_code == 403
            assert (await a.get("/api/v1/confluence/spaces")).status_code in {403, 404}
            assert (await a.post("/api/v1/auth/logout", headers={"Origin": origin})).status_code == 403
            old_cookie = a.cookies.get(cookie_name())
            assert (await a.post("/api/v1/auth/logout", headers=a_headers)).status_code == 204
            assert (await a.get("/api/v1/auth/me")).status_code == 401
            assert (await a.get("/api/v1/auth/me", headers={"Cookie": f"{cookie_name()}={old_cookie}"})).status_code == 401
            # B and admin sessions remain valid after A logs out.
            assert (await b.get("/api/v1/auth/me")).status_code == 200
            assert (await administrator.get("/api/v1/auth/me")).status_code == 200
            async with session_factory() as db:
                await db.execute(delete(AuthSession).where(AuthSession.user_id == admin.id))
                await db.commit()
            assert (await administrator.get("/api/v1/auth/me")).status_code == 401
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_override
        async with session_factory() as db:
            await db.execute(delete(Workspace).where(Workspace.id.in_(owned_workspace_ids)))
            await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(owned_kb_ids)))
            await db.execute(delete(AuthUser).where(AuthUser.username.in_(usernames)))
            await db.commit()
        await engine.dispose()
