from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.db.models import Document, KnowledgeBase, KnowledgeBaseVersion, Workspace
from app.operations import curate_knowledge_base as curation
from app.operations.curate_knowledge_base import (
    BGE_MODEL,
    CurationValidationError,
    _description,
    _report,
    copied_document,
    parse_page_ids,
    read_source_payload,
    validate_chunk_dimensions,
    validate_embedding_profile,
    validate_existing_version,
    validate_source_documents,
    validate_source_payload,
)


@pytest.fixture
def page():
    workspace = Workspace(id=uuid.uuid4(), tenant_id="local", user_id="local-user", name="Source")
    envelope = {
        "format": "confluence_storage_v1",
        "id": "123",
        "version": "7",
        "title": "Real page",
        "source_url": "https://confluence.example.test/pages/123",
        "space_key": "KBR",
        "ancestors": ["Root"],
        "labels": ["manual"],
        "storage_html": "<p>Preserved text</p><pre>cmd --exact  value\n</pre>",
    }
    payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode()
    document = Document(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        knowledge_base_version_id=None,
        filename="Real_page.123.confluence.json",
        object_key=f"workspaces/{workspace.id}/confluence/123/original/page.confluence.json",
        mime_type="application/json; charset=utf-8",
        sha256=hashlib.sha256(payload).hexdigest(),
        status="READY",
        search_enabled=True,
        title=envelope["title"],
        source_url=envelope["source_url"],
        source_type="CONFLUENCE",
        external_id="123",
        external_version="7",
        synced_at=datetime(2026, 10, 8, tzinfo=UTC),
    )
    source = validate_source_documents(
        workspace, [document], ("123",), tenant_id="local", subject="local-user"
    )[0]
    return workspace, document, source, payload


@pytest.mark.parametrize("value", ["", "x", "123,", "123,abc", "123,123", "123,-4"])
def test_page_selection_rejects_invalid_or_ambiguous_ids(value):
    with pytest.raises(CurationValidationError):
        parse_page_ids(value)


def test_page_selection_accepts_ordered_numeric_ids():
    assert parse_page_ids(" 123,456 ") == ("123", "456")


def test_page_selection_is_bounded():
    with pytest.raises(CurationValidationError):
        parse_page_ids(",".join(str(value) for value in range(101)))


@pytest.mark.parametrize("model,dimension", [("MiniLM", 1024), (BGE_MODEL, 384), (BGE_MODEL, 0)])
def test_embedding_profile_must_match_bge_native(model, dimension):
    with pytest.raises(CurationValidationError):
        validate_embedding_profile(model, dimension)


def test_native_embedding_profile_is_accepted():
    validate_embedding_profile(BGE_MODEL, 1024)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace_id", uuid.uuid4()),
        ("knowledge_base_version_id", uuid.uuid4()),
        ("source_type", "UPLOAD"),
        ("status", "QUEUED"),
        ("search_enabled", False),
        ("external_version", None),
        ("source_url", "relative/url"),
        ("title", None),
        ("filename", "generic.json"),
        ("sha256", "invalid"),
    ],
)
def test_invalid_source_documents_are_rejected(page, field, value):
    workspace, document, _, _ = page
    setattr(document, field, value)
    with pytest.raises(CurationValidationError):
        validate_source_documents(
            workspace, [document], ("123",), tenant_id="local", subject="local-user"
        )


@pytest.mark.parametrize("tenant,subject", [("other", "local-user"), ("local", "another-user")])
def test_workspace_owner_and_tenant_are_enforced(page, tenant, subject):
    workspace, document, _, _ = page
    with pytest.raises(CurationValidationError, match="tenant or owner"):
        validate_source_documents(
            workspace, [document], ("123",), tenant_id=tenant, subject=subject
        )


def test_missing_and_duplicate_source_pages_are_rejected(page):
    workspace, document, _, _ = page
    with pytest.raises(CurationValidationError, match="missing"):
        validate_source_documents(
            workspace, [document], ("123", "456"), tenant_id="local", subject="local-user"
        )
    with pytest.raises(CurationValidationError, match="ambiguous"):
        validate_source_documents(
            workspace, [document, document], ("123",), tenant_id="local", subject="local-user"
        )


@pytest.mark.parametrize("dimensions", [(0, 0, None, None), (2, 1, 1024, 1024), (2, 2, 384, 384)])
def test_incomplete_or_incompatible_source_vectors_are_rejected(page, dimensions):
    source = page[2]
    with pytest.raises(CurationValidationError, match="complete 1024"):
        validate_chunk_dimensions((source,), {source.id: dimensions})


def test_complete_native_vectors_are_validated_but_not_copied(page):
    source = page[2]
    validate_chunk_dimensions((source,), {source.id: (2, 2, 1024, 1024)})
    copy = copied_document(
        source, knowledge_base_id=uuid.uuid4(), version_id=uuid.uuid4(), document_id=uuid.uuid4()
    )
    assert not hasattr(copy, "embedding")


def test_copy_preserves_provenance_in_an_independent_scope_and_object(page):
    _, original, source, _ = page
    kb_id, version_id, document_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    copy = copied_document(
        source, knowledge_base_id=kb_id, version_id=version_id, document_id=document_id
    )
    assert copy.id != original.id
    assert copy.workspace_id is None
    assert copy.knowledge_base_version_id == version_id
    assert copy.object_key != original.object_key
    assert copy.object_key.startswith(f"knowledge-bases/{kb_id}/versions/{version_id}/")
    assert str(document_id) in copy.object_key
    for field in (
        "filename", "mime_type", "sha256", "title", "source_url", "source_type",
        "external_id", "external_version", "synced_at",
    ):
        assert getattr(copy, field) == getattr(original, field)
    assert copy.status == "QUEUED"
    assert copy.search_enabled is True


def test_source_payload_bytes_and_provenance_are_verified(page):
    source, payload = page[2:]
    validate_source_payload(source, payload)
    with pytest.raises(CurationValidationError, match="SHA-256"):
        validate_source_payload(source, payload + b" ")
    envelope = json.loads(payload)
    envelope["version"] = "8"
    changed = json.dumps(envelope).encode()
    with pytest.raises(CurationValidationError, match="identity or provenance"):
        validate_source_payload(replace(source, sha256=hashlib.sha256(changed).hexdigest()), changed)


@pytest.mark.parametrize("content", [b"[]", b"not-json", b'{"format":"other"}'])
def test_non_envelope_objects_cannot_be_promoted(page, content):
    source = replace(page[2], sha256=hashlib.sha256(content).hexdigest())
    with pytest.raises(CurationValidationError):
        validate_source_payload(source, content)


def test_minio_stream_is_closed_and_released_without_altering_bytes(page):
    source, payload = page[2:]
    response = SimpleNamespace(
        read=lambda limit: payload,
        close=lambda: events.append("closed"),
        release_conn=lambda: events.append("released"),
    )
    events = []
    backend = SimpleNamespace(
        _bucket="rag-documents",
        _client=SimpleNamespace(get_object=lambda bucket, key: response),
    )
    assert read_source_payload(source, backend) == payload
    assert events == ["closed", "released"]


def existing_snapshot(page):
    workspace, _, source, _ = page
    kb = KnowledgeBase(
        id=uuid.uuid4(), tenant_id="local", name="Curated",
        description=_description(workspace.id, "local-user", ("123",)),
    )
    version = KnowledgeBaseVersion(
        id=uuid.uuid4(), knowledge_base_id=kb.id, version=1, status="INDEXING",
        embedding_model=BGE_MODEL, embedding_dimension=1024,
    )
    document = copied_document(
        source, knowledge_base_id=kb.id, version_id=version.id, document_id=uuid.uuid4()
    )
    return kb, version, document


def test_existing_exact_snapshot_is_idempotent_even_after_publication(page):
    kb, version, document = existing_snapshot(page)
    version.status = "ACTIVE"
    assert validate_existing_version(
        kb, [version], [document], (page[2],), expected_description=kb.description
    ) is version


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("external_version", "99"), ("source_type", "UPLOAD")])
def test_existing_changed_snapshot_is_not_silently_overwritten(page, field, value):
    kb, version, document = existing_snapshot(page)
    setattr(document, field, value)
    with pytest.raises(CurationValidationError, match="differ"):
        validate_existing_version(
            kb, [version], [document], (page[2],), expected_description=kb.description
        )


def test_existing_shared_object_key_is_rejected(page):
    kb, version, document = existing_snapshot(page)
    document.object_key = page[2].object_key
    with pytest.raises(CurationValidationError, match="differ"):
        validate_existing_version(
            kb, [version], [document], (page[2],), expected_description=kb.description
        )


def test_unrelated_existing_knowledge_base_is_rejected(page):
    kb, version, document = existing_snapshot(page)
    with pytest.raises(CurationValidationError, match="unrelated"):
        validate_existing_version(
            kb, [version], [document], (page[2],), expected_description="not this operation"
        )


def test_report_exposes_ids_and_counts_but_not_article_payloads_or_storage_keys(page):
    report = _report(
        sources=(page[2],), workspace_id=page[0].id, name="Curated", apply=False
    )
    serialized = json.dumps(report)
    assert report["page_count"] == 1
    assert report["page_versions"] == {"123": "7"}
    assert report["published"] is False
    assert report["requires_ready_then_publish"] is True
    assert page[2].object_key not in serialized
    assert "storage_html" not in serialized


class FakeSession:
    def __init__(self, page, results, *, fail_commit=False):
        self.workspace = page[0]
        self.chunk_rows = [(page[2].id, 2, 2, 1024, 1024)]
        self.results = list(results)
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_commit = fail_commit

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, model, identifier):
        assert model is Workspace and identifier == self.workspace.id
        return self.workspace

    async def scalars(self, statement):
        result = self.results.pop(0)
        return SimpleNamespace(all=lambda: result)

    async def execute(self, statement, *args):
        return self.chunk_rows

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        return None

    async def commit(self):
        if self.fail_commit:
            raise RuntimeError("simulated commit failure")
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def fake_runtime(monkeypatch, page, db):
    monkeypatch.setattr(curation, "SessionLocal", lambda: db)
    monkeypatch.setattr(curation.settings, "embedding_model", BGE_MODEL)
    monkeypatch.setattr(curation.settings, "embedding_dim", 1024)
    monkeypatch.setattr(curation, "read_source_payload", lambda source: page[3])
    copied, deleted, notified = [], [], []

    def upload(**kwargs):
        copied.append((kwargs["object_key"], kwargs["stream"].read()))

    async def notify(job_id):
        notified.append(job_id)
        return True

    async def run_sync(function, *args):
        return function(*args)

    monkeypatch.setattr(curation.storage, "upload", upload)
    monkeypatch.setattr(curation.storage, "delete", deleted.append)
    monkeypatch.setattr(curation.ingestion_queue, "notify", notify)
    monkeypatch.setattr(curation.to_thread, "run_sync", run_sync)
    return copied, deleted, notified


async def run_curation(page, *, apply):
    return await curation.curate_knowledge_base(
        workspace_id=page[0].id,
        page_ids=("123",),
        name="Curated",
        tenant_id="local",
        subject="local-user",
        apply=apply,
    )


@pytest.mark.asyncio
async def test_dry_run_reads_and_validates_without_rows_objects_or_jobs(page, monkeypatch):
    db = FakeSession(page, [[page[1]], []])
    copied, deleted, notified = fake_runtime(monkeypatch, page, db)
    report = await run_curation(page, apply=False)
    assert report["dry_run"] is True and report["status"] == "PLANNED"
    assert not db.added and db.commits == 0
    assert not copied and not deleted and not notified


@pytest.mark.asyncio
async def test_apply_copies_bytes_and_creates_fresh_durable_job_without_publishing(page, monkeypatch):
    db = FakeSession(page, [[page[1]], []])
    copied, deleted, notified = fake_runtime(monkeypatch, page, db)
    report = await run_curation(page, apply=True)
    assert len(copied) == 1 and copied[0][1] == page[3]
    assert copied[0][0] != page[2].object_key
    assert db.commits == 1 and not deleted
    assert len(notified) == 1
    assert report["status"] == "INDEXING" and not report["published"]
    assert report["version_id"] and report["knowledge_base_id"]
    copied_docs = [item for item in db.added if isinstance(item, Document)]
    assert len(copied_docs) == 1 and copied_docs[0].source_type == "CONFLUENCE"
    assert copied_docs[0].external_version == "7"
    assert page[1].status == "READY"


@pytest.mark.asyncio
async def test_uncertain_commit_retains_objects_and_does_not_notify(page, monkeypatch):
    db = FakeSession(page, [[page[1]], []], fail_commit=True)
    copied, deleted, notified = fake_runtime(monkeypatch, page, db)
    with pytest.raises(RuntimeError, match="commit failure"):
        await run_curation(page, apply=True)
    assert db.rollbacks == 1
    assert copied and not deleted
    assert not notified


@pytest.mark.asyncio
async def test_failure_before_commit_cleans_up_only_new_objects(page, monkeypatch):
    db = FakeSession(page, [[page[1]], []])
    copied, deleted, notified = fake_runtime(monkeypatch, page, db)

    def failed_upload(**kwargs):
        copied.append((kwargs["object_key"], b"partial"))
        raise RuntimeError("simulated upload failure")

    monkeypatch.setattr(curation.storage, "upload", failed_upload)
    with pytest.raises(RuntimeError, match="upload failure"):
        await run_curation(page, apply=True)
    assert db.rollbacks == 1
    assert deleted == [key for key, _ in copied]
    assert page[2].object_key not in deleted
    assert not notified


@pytest.mark.asyncio
async def test_idempotent_apply_renotifies_existing_queued_job_without_new_copies(page, monkeypatch):
    kb, version, document = existing_snapshot(page)
    job_id = uuid.uuid4()
    db = FakeSession(page, [[page[1]], [kb], [version], [document], [job_id]])
    copied, deleted, notified = fake_runtime(monkeypatch, page, db)
    report = await run_curation(page, apply=True)
    assert report["existing"] is True
    assert report["knowledge_base_id"] == str(kb.id)
    assert report["version_id"] == str(version.id)
    assert notified == [job_id]
    assert not copied and not deleted and not db.added and db.commits == 0
