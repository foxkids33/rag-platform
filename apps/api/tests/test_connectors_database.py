"""Connector lifecycle against an explicitly selected disposable PostgreSQL."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.api.routes import confluence
from app.core.security import local_principal
from app.db.models import Connector, ConnectorJob, Document, IngestionJob, Workspace
from app.db.session import SessionLocal, engine
from app.main import app
from app.services.connector_queue import connector_queue
from app.services.storage import StorageError

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DATABASE_TESTS") != "1",
    reason="requires a migrated disposable PostgreSQL",
)


@pytest.fixture
async def client(monkeypatch):
    async def notify(*_):
        return True

    monkeypatch.setattr(connector_queue, "notify", notify)
    monkeypatch.setattr(confluence.ingestion_queue, "notify", notify)
    monkeypatch.setattr(confluence.storage, "upload", lambda **_: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    async with SessionLocal() as db:
        await db.execute(delete(Workspace).where(Workspace.name == "connector-lifecycle-test"))
        await db.execute(delete(Connector).where(Connector.name == "connector-lifecycle-test"))
        await db.commit()
    await engine.dispose()


async def setup_connector(client):
    created = await client.post(
        "/api/v1/integrations/confluence/connectors/enrollments",
        json={
            "name": "connector-lifecycle-test",
            "allowed_spaces": ["KBR"],
        },
    )
    assert created.status_code == 201, created.text
    payload = {
        "enrollment_token": created.json()["enrollment_token"],
        "capabilities": ["LIST_SPACES", "LIST_CHILDREN", "IMPORT_PAGES"],
    }
    enrolled = await client.post("/api/v1/connectors/enroll", json=payload)
    assert enrolled.status_code == 200, enrolled.text
    principal = local_principal()
    async with SessionLocal() as db:
        workspace = Workspace(
            id=uuid.uuid4(),
            name="connector-lifecycle-test",
            tenant_id=principal.tenant_id,
            user_id=principal.subject,
            source_mode="USER_DOCUMENTS",
        )
        db.add(workspace)
        await db.commit()
        workspace_id = str(workspace.id)
    return (
        enrolled.json()["connector_id"],
        {"Authorization": "Bearer " + enrolled.json()["credential"]},
        workspace_id,
    )


async def claim_import(client, connector_id, headers, workspace_id):
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/integrations/confluence/imports",
        json={"page_ids": ["42"], "page_spaces": {"42": "KBR"}},
    )
    assert created.status_code == 202, created.text
    claimed = await client.get(f"/api/v1/connectors/{connector_id}/jobs/next", headers=headers)
    assert claimed.status_code == 200, claimed.text
    return claimed.json()


@pytest.mark.asyncio
async def test_concurrent_enrollment_consumes_code_once(client):
    created = await client.post(
        "/api/v1/integrations/confluence/connectors/enrollments",
        json={
            "name": "connector-lifecycle-test",
            "allowed_spaces": ["KBR"],
        },
    )
    payload = {
        "enrollment_token": created.json()["enrollment_token"],
        "capabilities": ["LIST_SPACES"],
    }
    results = await asyncio.gather(
        *[client.post("/api/v1/connectors/enroll", json=payload) for _ in range(2)]
    )
    assert sorted(result.status_code for result in results) == [200, 401]


@pytest.mark.asyncio
async def test_import_result_redelivery_creates_only_one_document_and_ingestion_job(client):
    connector_id, headers, workspace_id = await setup_connector(client)
    job = await claim_import(client, connector_id, headers, workspace_id)
    result = {
        "lease_id": job["lease_id"],
        "result": {
            "pages": [
                {
                    "id": "42",
                    "title": "Test page",
                    "space_key": "KBR",
                    "version": "1",
                    "storage_html": "<p>Connector delivery</p>",
                }
            ],
            "errors": [],
        },
    }
    url = f"/api/v1/connectors/{connector_id}/jobs/{job['job_id']}/result"
    for _ in range(2):
        response = await client.post(url, headers=headers, json=result)
        assert response.status_code == 200, response.text
    async with SessionLocal() as db:
        documents = list(
            await db.scalars(
                select(Document).where(Document.workspace_id == uuid.UUID(workspace_id))
            )
        )
        assert len(documents) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(IngestionJob)
                .where(
                    IngestionJob.document_id == documents[0].id,
                )
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["CANCELLED", "expired"])
async def test_late_result_cannot_import_after_cancel_or_expiry(client, state):
    connector_id, headers, workspace_id = await setup_connector(client)
    job = await claim_import(client, connector_id, headers, workspace_id)
    async with SessionLocal() as db:
        row = await db.get(ConnectorJob, uuid.UUID(job["job_id"]))
        if state == "expired":
            row.leased_until = datetime.now(UTC) - timedelta(seconds=1)
        else:
            row.status = state
        await db.commit()
    response = await client.post(
        f"/api/v1/connectors/{connector_id}/jobs/{job['job_id']}/result",
        headers=headers,
        json={
            "lease_id": job["lease_id"],
            "result": {
                "pages": [
                    {
                        "id": "42",
                        "title": "Late page",
                        "space_key": "KBR",
                        "storage_html": "<p>late</p>",
                    }
                ]
            },
        },
    )
    assert response.status_code == 409
    async with SessionLocal() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Document)
                .where(
                    Document.workspace_id == uuid.UUID(workspace_id),
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_storage_failure_is_reported_as_failed_page_not_server_error(client, monkeypatch):
    connector_id, headers, workspace_id = await setup_connector(client)
    job = await claim_import(client, connector_id, headers, workspace_id)

    def unavailable(**_):
        raise StorageError("unavailable")

    monkeypatch.setattr(confluence.storage, "upload", unavailable)
    response = await client.post(
        f"/api/v1/connectors/{connector_id}/jobs/{job['job_id']}/result",
        headers=headers,
        json={
            "lease_id": job["lease_id"],
            "result": {
                "pages": [
                    {
                        "id": "42",
                        "title": "Page",
                        "space_key": "KBR",
                        "storage_html": "<p>content</p>",
                    }
                ]
            },
        },
    )
    assert response.status_code == 200, response.text
    operation = await client.get(
        f"/api/v1/workspaces/{workspace_id}/integrations/confluence/operations/{job['job_id']}",
    )
    assert operation.json()["result"]["failed"] == 1
