from __future__ import annotations

from collections.abc import Iterable

EDITABLE_VERSION_STATUSES = frozenset({"DRAFT", "INDEXING", "READY", "FAILED"})
IMMUTABLE_VERSION_STATUSES = frozenset({"ACTIVE", "ARCHIVED"})
BUSY_DOCUMENT_STATUSES = frozenset({"QUEUED", "PROCESSING"})


def derive_version_status(current_status: str, document_statuses: Iterable[str]) -> str:
    """Derive a mutable knowledge-base version status from its documents.

    Published versions are immutable and keep ACTIVE/ARCHIVED until an explicit
    publish operation changes which version is active.
    """

    if current_status in IMMUTABLE_VERSION_STATUSES:
        return current_status

    statuses = list(document_statuses)
    if not statuses:
        return "DRAFT"
    if any(status in BUSY_DOCUMENT_STATUSES for status in statuses):
        return "INDEXING"
    if any(status == "FAILED" for status in statuses):
        return "FAILED"
    if all(status == "READY" for status in statuses):
        return "READY"
    return "DRAFT"


def version_is_editable(status: str) -> bool:
    return status in EDITABLE_VERSION_STATUSES


def version_can_publish(status: str) -> bool:
    return status in {"READY", "ARCHIVED"}
