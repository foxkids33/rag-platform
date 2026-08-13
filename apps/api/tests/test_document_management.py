import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.api.routes import documents
from app.api.routes.documents import DocumentUpdate
from app.db.models import Document


class FakeSession:
    def __init__(self, document: Document | None) -> None:
        self.document = document
        self.committed = False
        self.deleted: Document | None = None

    async def get(self, _model, _document_id):
        return self.document

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _document: Document) -> None:
        return None

    async def delete(self, document: Document) -> None:
        self.deleted = document


def _document(*, status: str = "READY", search_enabled: bool = True) -> Document:
    return Document(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        knowledge_base_version_id=None,
        filename="manual.pdf",
        object_key="workspaces/ws/documents/doc/manual.pdf",
        mime_type="application/pdf",
        sha256="a" * 64,
        status=status,
        search_enabled=search_enabled,
        created_at=datetime.now(UTC),
    )


def test_document_can_be_excluded_and_returned_to_search() -> None:
    document = _document()
    db = FakeSession(document)

    result = asyncio.run(
        documents.update_workspace_document(
            document.workspace_id,
            document.id,
            DocumentUpdate(search_enabled=False),
            db,
        )
    )

    assert result.search_enabled is False
    assert db.committed is True

    db.committed = False
    result = asyncio.run(
        documents.update_workspace_document(
            document.workspace_id,
            document.id,
            DocumentUpdate(search_enabled=True),
            db,
        )
    )

    assert result.search_enabled is True
    assert db.committed is True


def test_non_ready_document_cannot_be_enabled_for_search() -> None:
    document = _document(status="FAILED", search_enabled=False)
    db = FakeSession(document)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            documents.update_workspace_document(
                document.workspace_id,
                document.id,
                DocumentUpdate(search_enabled=True),
                db,
            )
        )

    assert exc_info.value.status_code == 409
    assert db.committed is False


def test_delete_removes_object_before_database_row(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document()
    db = FakeSession(document)
    removed: list[str] = []
    monkeypatch.setattr(documents.storage, "delete", removed.append)

    response = asyncio.run(
        documents.delete_workspace_document(
            document.workspace_id,
            document.id,
            db,
        )
    )

    assert response.status_code == 204
    assert removed == [document.object_key]
    assert db.deleted is document
    assert db.committed is True


def test_processing_document_cannot_be_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document(status="PROCESSING")
    db = FakeSession(document)
    removed: list[str] = []
    monkeypatch.setattr(documents.storage, "delete", removed.append)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            documents.delete_workspace_document(
                document.workspace_id,
                document.id,
                db,
            )
        )

    assert exc_info.value.status_code == 409
    assert removed == []
    assert db.deleted is None
