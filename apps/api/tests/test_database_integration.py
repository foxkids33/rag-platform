import os
import uuid
from io import BytesIO

import pytest
from sqlalchemy import delete, func, select
from starlette.datastructures import Headers, UploadFile

from app.api.routes import confluence as confluence_route
from app.api.routes import documents
from app.core.config import settings
from app.core.security import Principal
from app.db.models import Document, IngestionJob, Workspace
from app.db.session import SessionLocal, engine
from app.services.confluence import ConfluenceError, ConfluencePage

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DATABASE_TESTS") != "1",
    reason="set RUN_DATABASE_TESTS=1 against a migrated disposable database",
)


@pytest.fixture(autouse=True)
async def dispose_database_pool_after_test():
    yield
    await engine.dispose()


def _principal(*, admin: bool = False) -> Principal:
    roles = frozenset({settings.auth_admin_role}) if admin else frozenset()
    return Principal(
        subject="database-integration-user",
        tenant_id="database-integration-tenant",
        roles=roles,
        claims={},
    )


async def _create_workspace(db, principal: Principal) -> Workspace:
    workspace = Workspace(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        name="Database integration test",
        user_id=principal.subject,
        source_mode="USER_DOCUMENTS",
    )
    db.add(workspace)
    await db.commit()
    return workspace


@pytest.mark.asyncio
async def test_upload_persists_document_before_durable_job(monkeypatch) -> None:
    principal = _principal()
    uploads: list[str] = []
    notifications: list[uuid.UUID] = []

    def upload(*, object_key, **_kwargs) -> None:
        uploads.append(object_key)

    async def notify(job_id: uuid.UUID) -> bool:
        notifications.append(job_id)
        return True

    monkeypatch.setattr(documents.storage, "upload", upload)
    monkeypatch.setattr(documents.ingestion_queue, "notify", notify)

    async with SessionLocal() as db:
        workspace = await _create_workspace(db, principal)
        try:
            uploaded_file = UploadFile(
                BytesIO(b"A durable upload body"),
                filename="manual.txt",
                headers=Headers({"content-type": "text/plain"}),
            )
            document = await documents.upload_workspace_document(
                workspace.id,
                uploaded_file,
                db,
                principal,
            )

            job = await db.scalar(
                select(IngestionJob).where(IngestionJob.document_id == document.id)
            )
            assert document.status == "QUEUED"
            assert job is not None
            assert job.status == "QUEUED"
            assert uploads == [document.object_key]
            assert notifications == [job.id]
        finally:
            await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await db.commit()


@pytest.mark.asyncio
async def test_confluence_import_is_idempotent_in_database(monkeypatch) -> None:
    principal = _principal(admin=True)
    page = ConfluencePage(
        id="12345",
        title="Recovery runbook",
        storage_html="<h1>Recovery</h1><p>Use code BLUE-KB-942.</p>",
        version="7",
        source_url="https://docs.example.test/wiki/spaces/OPS/pages/12345",
    )
    uploads: list[str] = []

    async def resolve_page_ids(payload, _client):
        return payload.page_ids

    async def get_page(page_id: str):
        if page_id == "missing":
            raise ConfluenceError("Confluence page or space was not found")
        return page

    class FakeConfluenceClient:
        async def get_page(self, page_id: str):
            return await get_page(page_id)

        async def close(self) -> None:
            return None

    def upload(*, object_key, **_kwargs) -> None:
        uploads.append(object_key)

    async def notify(_job_id: uuid.UUID) -> bool:
        return True

    monkeypatch.setattr(settings, "confluence_enabled", True)
    monkeypatch.setattr(confluence_route, "_resolve_page_ids", resolve_page_ids)
    monkeypatch.setattr(
        confluence_route,
        "_client_for_credentials",
        lambda _credentials: FakeConfluenceClient(),
    )
    monkeypatch.setattr(confluence_route.storage, "upload", upload)
    monkeypatch.setattr(confluence_route.ingestion_queue, "notify", notify)

    async with SessionLocal() as db:
        workspace = await _create_workspace(db, principal)
        try:
            request = confluence_route.ConfluenceImportRequest(
                page_ids=["missing", page.id],
                credentials={"email": "user@example.test", "token": "test-token"},
            )
            first = await confluence_route.import_confluence(
                workspace.id,
                request,
                db,
                principal,
            )
            assert first.queued == 1
            assert first.failed == 1
            assert first.results[0].status == "failed"
            created = next(item for item in first.results if item.status == "created")
            assert created.document_id is not None

            document = await db.get(Document, created.document_id)
            assert document is not None
            document.status = "READY"
            job = await db.scalar(
                select(IngestionJob).where(IngestionJob.document_id == document.id)
            )
            assert job is not None
            job.status = "READY"
            await db.commit()

            second = await confluence_route.import_confluence(
                workspace.id,
                confluence_route.ConfluenceImportRequest(
                    page_ids=[page.id],
                    credentials={"email": "user@example.test", "token": "test-token"},
                ),
                db,
                principal,
            )
            document_count = await db.scalar(
                select(func.count(Document.id)).where(
                    Document.workspace_id == workspace.id,
                    Document.source_type == "CONFLUENCE",
                    Document.external_id == page.id,
                )
            )
            job_count = await db.scalar(
                select(func.count(IngestionJob.id)).where(
                    IngestionJob.document_id == document.id
                )
            )

            assert second.queued == 0
            assert second.skipped == 1
            assert second.results[0].status == "skipped"
            assert document_count == 1
            assert job_count == 1
            assert len(uploads) == 1
        finally:
            await db.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await db.commit()
