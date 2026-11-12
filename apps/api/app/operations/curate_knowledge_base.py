"""Freeze selected workspace Confluence pages into a separately indexed KB version.

Dry-run is the default. This command never fetches Confluence, reuses vectors,
publishes a version, or shares an object key with the source workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit

from anyio import to_thread
from sqlalchemy import func, select, text

from app.core.config import settings
from app.db.models import (
    Document,
    DocumentChunk,
    IngestionJob,
    KnowledgeBase,
    KnowledgeBaseVersion,
    Workspace,
)
from app.db.session import SessionLocal
from app.services.queue import ingestion_queue
from app.services.storage import ObjectStorage, StorageError, storage

logger = logging.getLogger(__name__)
CURATION_KIND = "confluence_curated_snapshot_v1"
CHUNKER_VERSION = "confluence-storage-parent-child-v1"
BGE_MODEL = "BAAI/bge-m3"


class CurationValidationError(ValueError):
    """The requested source or destination is not a valid curated snapshot."""


@dataclass(frozen=True, slots=True)
class SourceDocument:
    id: uuid.UUID
    workspace_id: uuid.UUID
    page_id: str
    page_version: str
    filename: str
    object_key: str
    mime_type: str | None
    sha256: str
    title: str
    source_url: str
    synced_at: datetime | None


def parse_page_ids(value: str) -> tuple[str, ...]:
    ids = tuple(part.strip() for part in value.split(","))
    if not ids or len(ids) > 100 or any(not re.fullmatch(r"[0-9]+", item) for item in ids):
        raise CurationValidationError("--page-ids must contain 1 to 100 numeric page IDs")
    if len(set(ids)) != len(ids):
        raise CurationValidationError("--page-ids contains duplicate IDs")
    return ids


def validate_embedding_profile(model: str, dimension: int) -> None:
    if model != BGE_MODEL or dimension != 1024:
        raise CurationValidationError("Curation requires the active BAAI/bge-m3 1024 profile")


def validate_source_documents(
    workspace: Workspace,
    documents: list[Document],
    page_ids: tuple[str, ...],
    *,
    tenant_id: str,
    subject: str,
) -> tuple[SourceDocument, ...]:
    if workspace.tenant_id != tenant_id or workspace.user_id != subject:
        raise CurationValidationError("Source workspace tenant or owner does not match the request")
    by_page: dict[str, SourceDocument] = {}
    for document in documents:
        if document.workspace_id != workspace.id or document.knowledge_base_version_id is not None:
            raise CurationValidationError("Source document belongs to a different scope")
        if document.source_type != "CONFLUENCE" or document.status != "READY":
            raise CurationValidationError("Every source document must be a READY Confluence page")
        if not document.search_enabled:
            raise CurationValidationError("Every source document must be enabled for search")
        page_id = document.external_id
        if page_id not in page_ids or page_id in by_page:
            raise CurationValidationError("Selected page IDs are missing or ambiguous")
        if not document.external_version or not document.title or not document.source_url:
            raise CurationValidationError("Source document lacks page provenance")
        source_url = urlsplit(document.source_url)
        if source_url.scheme not in {"http", "https"} or not source_url.hostname:
            raise CurationValidationError("Source document URL is not an absolute HTTP(S) URL")
        if not document.filename.endswith(".confluence.json"):
            raise CurationValidationError("Source document is not a Confluence storage envelope")
        if not re.fullmatch(r"[0-9a-f]{64}", document.sha256):
            raise CurationValidationError("Source document has an invalid SHA-256")
        by_page[page_id] = SourceDocument(
            id=document.id,
            workspace_id=workspace.id,
            page_id=page_id,
            page_version=document.external_version,
            filename=document.filename,
            object_key=document.object_key,
            mime_type=document.mime_type,
            sha256=document.sha256,
            title=document.title,
            source_url=document.source_url,
            synced_at=document.synced_at,
        )
    if set(by_page) != set(page_ids):
        raise CurationValidationError("Some requested Confluence pages are missing from the workspace")
    return tuple(by_page[page_id] for page_id in page_ids)


def validate_chunk_dimensions(
    sources: tuple[SourceDocument, ...],
    dimensions: dict[uuid.UUID, tuple[int, int, int | None, int | None]],
) -> None:
    for source in sources:
        count, embedded, smallest, largest = dimensions.get(source.id, (0, 0, None, None))
        if count < 1 or embedded != count or smallest != 1024 or largest != 1024:
            raise CurationValidationError("Every selected page must have complete 1024-dimensional vectors")


def validate_source_payload(source: SourceDocument, payload: bytes) -> None:
    if not payload or len(payload) > settings.upload_max_bytes:
        raise CurationValidationError("Source object is empty or exceeds the configured upload limit")
    if hashlib.sha256(payload).hexdigest() != source.sha256:
        raise CurationValidationError("Source object SHA-256 differs from the document row")
    try:
        envelope = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise CurationValidationError("Source object is not a valid Confluence JSON envelope") from exc
    if not isinstance(envelope, dict) or envelope.get("format") != "confluence_storage_v1":
        raise CurationValidationError("Source object format is not confluence_storage_v1")
    expected = {
        "id": source.page_id,
        "version": source.page_version,
        "title": source.title,
        "source_url": source.source_url,
    }
    if any(envelope.get(key) != value for key, value in expected.items()):
        raise CurationValidationError("Source envelope page identity or provenance differs from the row")
    if not isinstance(envelope.get("storage_html"), str):
        raise CurationValidationError("Source envelope has no Confluence storage body")


def read_source_payload(source: SourceDocument, backend: ObjectStorage = storage) -> bytes:
    response = None
    try:
        response = backend._client.get_object(backend._bucket, source.object_key)
        payload = response.read(settings.upload_max_bytes + 1)
    except Exception as exc:
        raise StorageError("Failed to read a source snapshot object") from exc
    finally:
        if response is not None:
            response.close()
            response.release_conn()
    validate_source_payload(source, payload)
    return payload


def copied_document(
    source: SourceDocument,
    *,
    knowledge_base_id: uuid.UUID,
    version_id: uuid.UUID,
    document_id: uuid.UUID,
) -> Document:
    object_key = (
        f"knowledge-bases/{knowledge_base_id}/versions/{version_id}/"
        f"documents/{document_id}/{source.filename}"
    )
    if object_key == source.object_key:
        raise CurationValidationError("Curated pages cannot share source object keys")
    return Document(
        id=document_id,
        knowledge_base_version_id=version_id,
        workspace_id=None,
        filename=source.filename,
        object_key=object_key,
        mime_type=source.mime_type,
        sha256=source.sha256,
        status="QUEUED",
        search_enabled=True,
        title=source.title,
        source_url=source.source_url,
        source_type="CONFLUENCE",
        external_id=source.page_id,
        external_version=source.page_version,
        synced_at=source.synced_at,
    )


def _description(workspace_id: uuid.UUID, subject: str, page_ids: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "kind": CURATION_KIND,
            "source_workspace_id": str(workspace_id),
            "curator_subject": subject,
            "page_ids": sorted(page_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def validate_existing_version(
    knowledge_base: KnowledgeBase,
    versions: list[KnowledgeBaseVersion],
    documents: list[Document],
    sources: tuple[SourceDocument, ...],
    *,
    expected_description: str,
) -> KnowledgeBaseVersion:
    if knowledge_base.description != expected_description or len(versions) != 1:
        raise CurationValidationError("An unrelated or changed knowledge base already uses this name")
    version = versions[0]
    if version.version != 1 or version.embedding_model != BGE_MODEL or version.embedding_dimension != 1024:
        raise CurationValidationError("Existing curated version has a different embedding profile")
    by_page = {document.external_id: document for document in documents}
    if len(by_page) != len(documents) or set(by_page) != {source.page_id for source in sources}:
        raise CurationValidationError("Existing curated version has a different page selection")
    for source in sources:
        document = by_page[source.page_id]
        if (
            document.knowledge_base_version_id != version.id
            or document.workspace_id is not None
            or document.source_type != "CONFLUENCE"
            or document.external_version != source.page_version
            or document.sha256 != source.sha256
            or document.source_url != source.source_url
            or document.title != source.title
            or document.filename != source.filename
            or document.object_key == source.object_key
        ):
            raise CurationValidationError("Existing curated pages differ from the requested snapshot")
    return version


def _report(
    *,
    sources: tuple[SourceDocument, ...],
    workspace_id: uuid.UUID,
    name: str,
    apply: bool,
    knowledge_base: KnowledgeBase | None = None,
    version: KnowledgeBaseVersion | None = None,
    existing: bool = False,
    notified_jobs: int = 0,
) -> dict[str, Any]:
    return {
        "dry_run": not apply,
        "existing": existing,
        "source_workspace_id": str(workspace_id),
        "name": name,
        "knowledge_base_id": str(knowledge_base.id) if knowledge_base else None,
        "version_id": str(version.id) if version else None,
        "version": version.version if version else 1,
        "status": version.status if version else "PLANNED",
        "page_count": len(sources),
        "page_ids": [source.page_id for source in sources],
        "page_versions": {source.page_id: source.page_version for source in sources},
        "embedding_model": BGE_MODEL,
        "embedding_dimension": 1024,
        "source_type": "CONFLUENCE",
        "notified_jobs": notified_jobs,
        "published": version is not None and version.status == "ACTIVE",
        "publication_performed": False,
        "requires_ready_then_publish": version is None or version.status != "ACTIVE",
    }


async def curate_knowledge_base(
    *,
    workspace_id: uuid.UUID,
    page_ids: tuple[str, ...],
    name: str,
    tenant_id: str,
    subject: str,
    apply: bool,
) -> dict[str, Any]:
    validate_embedding_profile(settings.embedding_model, settings.embedding_dim)
    if not name.strip() or len(name.strip()) > 255 or not tenant_id.strip() or not subject.strip():
        raise CurationValidationError("Name, tenant ID, and subject must be non-empty")
    name = name.strip()
    parse_page_ids(",".join(page_ids))
    slug = "curated-" + hashlib.sha256(name.encode()).hexdigest()[:24]
    description = _description(workspace_id, subject, page_ids)
    async with SessionLocal() as db:
        if apply:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"curate-kb:{tenant_id}:{slug}"},
            )
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None:
            raise CurationValidationError("Source workspace does not exist")
        statement = select(Document).where(
            Document.workspace_id == workspace_id,
            Document.external_id.in_(page_ids),
        )
        if apply:
            statement = statement.with_for_update()
        documents = list((await db.scalars(statement)).all())
        sources = validate_source_documents(
            workspace, documents, page_ids, tenant_id=tenant_id, subject=subject
        )
        chunk_rows = await db.execute(
            select(
                DocumentChunk.document_id,
                func.count(DocumentChunk.id),
                func.count(DocumentChunk.embedding),
                func.min(func.vector_dims(DocumentChunk.embedding)),
                func.max(func.vector_dims(DocumentChunk.embedding)),
            )
            .where(DocumentChunk.document_id.in_([source.id for source in sources]))
            .group_by(DocumentChunk.document_id)
        )
        validate_chunk_dimensions(
            sources,
            {row[0]: (row[1], row[2], row[3], row[4]) for row in chunk_rows},
        )
        existing_kbs = list(
            (
                await db.scalars(
                    select(KnowledgeBase).where(
                        KnowledgeBase.tenant_id == tenant_id,
                        (KnowledgeBase.name == name) | (KnowledgeBase.slug == slug),
                    )
                )
            ).all()
        )
        if len(existing_kbs) > 1:
            raise CurationValidationError("Curated knowledge base name or slug is ambiguous")
        if existing_kbs:
            knowledge_base = existing_kbs[0]
            versions = list(
                (
                    await db.scalars(
                        select(KnowledgeBaseVersion).where(
                            KnowledgeBaseVersion.knowledge_base_id == knowledge_base.id
                        )
                    )
                ).all()
            )
            existing_documents = list(
                (
                    await db.scalars(
                        select(Document).where(
                            Document.knowledge_base_version_id.in_([item.id for item in versions])
                        )
                    )
                ).all()
            )
            version = validate_existing_version(
                knowledge_base,
                versions,
                existing_documents,
                sources,
                expected_description=description,
            )
            job_ids = list(
                (
                    await db.scalars(
                        select(IngestionJob.id).where(
                            IngestionJob.document_id.in_([
                                document.id
                                for document in existing_documents
                                if document.status == "QUEUED"
                            ]),
                            IngestionJob.status == "QUEUED",
                        )
                    )
                ).all()
            )
            if apply:
                for job_id in job_ids:
                    await ingestion_queue.notify(job_id)
            return _report(
                sources=sources,
                workspace_id=workspace_id,
                name=name,
                apply=apply,
                knowledge_base=knowledge_base,
                version=version,
                existing=True,
                notified_jobs=len(job_ids) if apply else 0,
            )

        # Check every original blob before creating objects or database rows.
        payloads: list[bytes] = []
        total_bytes = 0
        for source in sources:
            payload = await to_thread.run_sync(read_source_payload, source)
            total_bytes += len(payload)
            if total_bytes > settings.upload_max_bytes:
                raise CurationValidationError("Curated snapshot exceeds the bounded in-memory size limit")
            payloads.append(payload)
        if not apply:
            return _report(sources=sources, workspace_id=workspace_id, name=name, apply=False)

        knowledge_base = KnowledgeBase(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=name,
            slug=slug,
            description=description,
            active_version_id=None,
            graph_enabled=False,
        )
        version = KnowledgeBaseVersion(
            id=uuid.uuid4(),
            knowledge_base_id=knowledge_base.id,
            version=1,
            status="INDEXING",
            embedding_model=BGE_MODEL,
            embedding_dimension=1024,
            chunker_version=CHUNKER_VERSION,
        )
        copied_keys: list[str] = []
        job_ids: list[uuid.UUID] = []
        commit_attempted = False
        try:
            db.add(knowledge_base)
            await db.flush()
            db.add(version)
            await db.flush()
            for source, payload in zip(sources, payloads, strict=True):
                document = copied_document(
                    source,
                    knowledge_base_id=knowledge_base.id,
                    version_id=version.id,
                    document_id=uuid.uuid4(),
                )
                copied_keys.append(document.object_key)
                await to_thread.run_sync(
                    lambda document=document, payload=payload: storage.upload(
                        object_key=document.object_key,
                        stream=BytesIO(payload),
                        size=len(payload),
                        content_type=document.mime_type or "application/json; charset=utf-8",
                    )
                )
                db.add(document)
                await db.flush()
                job = IngestionJob(
                    id=uuid.uuid4(), document_id=document.id, status="QUEUED", progress=0, attempts=0
                )
                db.add(job)
                job_ids.append(job.id)
            commit_attempted = True
            await db.commit()
        except Exception:
            await db.rollback()
            # A lost COMMIT acknowledgement is not proof of rollback. Keeping
            # possible orphan blobs is safer than deleting committed documents'
            # data. A rerun resolves committed rows through the same advisory lock.
            if not commit_attempted:
                for object_key in copied_keys:
                    try:
                        await to_thread.run_sync(storage.delete, object_key)
                    except StorageError:
                        logger.error("Failed to clean up an uncommitted curated snapshot object")
            else:
                logger.error("Commit outcome is uncertain; retaining new curated snapshot objects")
            raise
        for job_id in job_ids:
            await ingestion_queue.notify(job_id)
        return _report(
            sources=sources,
            workspace_id=workspace_id,
            name=name,
            apply=True,
            knowledge_base=knowledge_base,
            version=version,
            notified_jobs=len(job_ids),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True, type=uuid.UUID)
    parser.add_argument("--page-ids", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--tenant-id", default="local")
    parser.add_argument("--subject", default="local-user")
    parser.add_argument("--apply", action="store_true", help="Copy objects and queue a new draft version")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        report = asyncio.run(
            curate_knowledge_base(
                workspace_id=args.workspace_id,
                page_ids=parse_page_ids(args.page_ids),
                name=args.name,
                tenant_id=args.tenant_id,
                subject=args.subject,
                apply=args.apply,
            )
        )
    except (CurationValidationError, StorageError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary must not expose SQL payloads or credentials.
        # SQL/network exceptions may include payloads or credentials in their text.
        print(json.dumps({"error": "Curation operation failed", "type": type(exc).__name__}))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
