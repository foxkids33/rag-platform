from app.services.knowledge_base_lifecycle import (
    derive_version_status,
    version_can_publish,
    version_is_editable,
)


def test_empty_version_is_draft() -> None:
    assert derive_version_status("DRAFT", []) == "DRAFT"


def test_queued_or_processing_document_makes_version_indexing() -> None:
    assert derive_version_status("DRAFT", ["READY", "QUEUED"]) == "INDEXING"
    assert derive_version_status("INDEXING", ["PROCESSING"]) == "INDEXING"


def test_failed_document_blocks_ready_status() -> None:
    assert derive_version_status("INDEXING", ["READY", "FAILED"]) == "FAILED"


def test_all_documents_ready_makes_version_ready() -> None:
    assert derive_version_status("INDEXING", ["READY", "READY"]) == "READY"


def test_published_versions_are_immutable() -> None:
    assert derive_version_status("ACTIVE", ["FAILED"]) == "ACTIVE"
    assert derive_version_status("ARCHIVED", ["PROCESSING"]) == "ARCHIVED"
    assert version_is_editable("ACTIVE") is False
    assert version_is_editable("ARCHIVED") is False


def test_ready_and_archived_versions_can_be_published() -> None:
    assert version_can_publish("READY") is True
    assert version_can_publish("ARCHIVED") is True
    assert version_can_publish("FAILED") is False
