from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.services.context_builder import build_context
from app.services.workspace_sources import (
    WorkspaceSourceMode,
    default_source_mode,
    includes_knowledge_base,
    includes_workspace_documents,
)


@dataclass
class Candidate:
    text: str
    source_scope: str
    knowledge_base_name: str | None
    knowledge_base_version: int | None
    document_id: uuid.UUID = uuid.uuid4()
    chunk_id: uuid.UUID = uuid.uuid4()
    filename: str = "manual.pdf"
    chunk_index: int = 1
    parent_text: str | None = None
    heading: str | None = "Введение"
    heading_breadcrumb: str | None = "1. Введение"
    page_start: int | None = 2
    page_end: int | None = 2
    retrieval_rank: int = 1
    dense_score: float | None = 0.8
    lexical_score: float | None = 0.2
    rrf_score: float = 0.03
    rerank_score: float | None = 0.95
    rerank_fusion_score: float | None = 0.02


def test_user_documents_mode_only_includes_workspace_layer() -> None:
    mode = WorkspaceSourceMode.USER_DOCUMENTS
    assert includes_workspace_documents(mode) is True
    assert includes_knowledge_base(mode) is False


def test_knowledge_base_mode_only_includes_active_kb_layer() -> None:
    mode = WorkspaceSourceMode.KNOWLEDGE_BASE
    assert includes_workspace_documents(mode) is False
    assert includes_knowledge_base(mode) is True


def test_hybrid_mode_includes_both_layers() -> None:
    mode = WorkspaceSourceMode.HYBRID
    assert includes_workspace_documents(mode) is True
    assert includes_knowledge_base(mode) is True


def test_default_mode_preserves_previous_workspace_behaviour() -> None:
    assert default_source_mode(False) is WorkspaceSourceMode.USER_DOCUMENTS
    assert default_source_mode(True) is WorkspaceSourceMode.HYBRID


def test_context_marks_knowledge_base_provenance() -> None:
    candidate = Candidate(
        text="МБД.Х — это программно-аппаратный комплекс для хранения больших данных.",
        source_scope="knowledge_base",
        knowledge_base_name="Скала МБД.Х",
        knowledge_base_version=3,
    )
    context = build_context(
        "Что такое МБД.Х?",
        [candidate],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=3,
    )

    assert context.sources[0].source_scope == "knowledge_base"
    assert context.sources[0].knowledge_base_version == 3
    assert "база знаний: Скала МБД.Х, версия 3" in context.text


def test_context_keeps_workspace_source_compatible() -> None:
    candidate = Candidate(
        text="Документ workspace содержит подтверждённое описание продукта.",
        source_scope="workspace",
        knowledge_base_name=None,
        knowledge_base_version=None,
    )
    context = build_context(
        "Что содержит документ?",
        [candidate],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=3,
    )

    assert context.text.startswith("[1] Источник: manual.pdf")
