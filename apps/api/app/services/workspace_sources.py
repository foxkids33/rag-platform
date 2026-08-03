from __future__ import annotations

from enum import StrEnum


class WorkspaceSourceMode(StrEnum):
    USER_DOCUMENTS = "USER_DOCUMENTS"
    KNOWLEDGE_BASE = "KNOWLEDGE_BASE"
    HYBRID = "HYBRID"


VALID_SOURCE_MODES = {mode.value for mode in WorkspaceSourceMode}


def includes_workspace_documents(mode: str | WorkspaceSourceMode) -> bool:
    value = WorkspaceSourceMode(mode)
    return value in {WorkspaceSourceMode.USER_DOCUMENTS, WorkspaceSourceMode.HYBRID}


def includes_knowledge_base(mode: str | WorkspaceSourceMode) -> bool:
    value = WorkspaceSourceMode(mode)
    return value in {WorkspaceSourceMode.KNOWLEDGE_BASE, WorkspaceSourceMode.HYBRID}


def default_source_mode(has_knowledge_base: bool) -> WorkspaceSourceMode:
    if has_knowledge_base:
        return WorkspaceSourceMode.HYBRID
    return WorkspaceSourceMode.USER_DOCUMENTS
