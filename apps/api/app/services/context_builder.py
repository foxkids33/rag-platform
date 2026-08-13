from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from app.services.rag_quality import evidence_status, source_quality_score

PAGE_MARKER_RE = re.compile(r"^\s*\d{1,4}\s+из\s+\d{1,4}\s*$", re.IGNORECASE)
STANDALONE_PAGE_RE = re.compile(r"^\s*\d{1,4}\s*$")
DOT_LEADER_RE = re.compile(r"\.{4,}")
URL_OR_EMAIL_RE = re.compile(
    r"(?:https?://|www\.|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:[.\-][0-9A-Za-zА-Яа-яЁё]+)*")
QUERY_STOP_WORDS = {
    "что",
    "такое",
    "какой",
    "какая",
    "какие",
    "как",
    "для",
    "чего",
    "это",
    "есть",
    "или",
    "его",
    "ее",
    "её",
    "про",
    "при",
    "под",
    "над",
    "где",
    "когда",
    "зачем",
}


class SearchResultLike(Protocol):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    text: str
    parent_text: str | None
    heading: str | None
    heading_breadcrumb: str | None
    page_start: int | None
    page_end: int | None
    retrieval_rank: int
    dense_score: float | None
    lexical_score: float | None
    rrf_score: float
    rerank_score: float | None
    rerank_fusion_score: float | None
    source_scope: str
    knowledge_base_name: str | None
    knowledge_base_version: int | None


@dataclass(frozen=True)
class ContextSource:
    index: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    heading: str | None
    page_start: int | None
    page_end: int | None
    excerpt: str
    retrieval_rank: int
    rerank_score: float | None
    rerank_fusion_score: float | None
    source_scope: str
    knowledge_base_name: str | None
    knowledge_base_version: int | None
    quality_score: float

    @property
    def citation(self) -> str:
        return f"[{self.index}]"


@dataclass(frozen=True)
class BuiltContext:
    text: str
    sources: list[ContextSource]
    char_count: int
    candidate_count: int
    evidence_status: str
    evidence_score: float | None
    rejected_low_score: int
    rejected_duplicate: int
    rejected_document_cap: int


def clean_context_text(value: str) -> str:
    cleaned_lines: list[str] = []
    previous_line: str | None = None

    for raw_line in value.replace("\x0c", "\n").splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if PAGE_MARKER_RE.fullmatch(line) or STANDALONE_PAGE_RE.fullmatch(line):
            continue
        if DOT_LEADER_RE.search(line):
            continue
        if URL_OR_EMAIL_RE.search(line):
            continue
        if line.casefold() in {"оглавление", "содержание"}:
            continue
        if line == previous_line:
            continue
        cleaned_lines.append(line)
        previous_line = line

    while cleaned_lines and cleaned_lines[-1] == "":
        cleaned_lines.pop()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()


def _query_terms(query: str) -> set[str]:
    return {
        token.casefold()
        for token in WORD_RE.findall(query)
        if len(token) >= 3 and token.casefold() not in QUERY_STOP_WORDS
    }


def focused_context_excerpt(value: str, query: str, max_chars: int) -> str:
    cleaned = clean_context_text(value)
    if len(cleaned) <= max_chars:
        return cleaned

    terms = _query_terms(query)
    paragraphs = [part.strip() for part in cleaned.split("\n\n") if part.strip()]
    if not paragraphs:
        return cleaned[:max_chars].rstrip()

    def paragraph_score(paragraph: str) -> tuple[float, int]:
        paragraph_terms = {token.casefold() for token in WORD_RE.findall(paragraph)}
        overlap = len(terms & paragraph_terms)
        score = float(overlap)
        lowered = paragraph.casefold()
        if query.casefold().startswith("что такое") and any(
            marker in lowered
            for marker in (" — это ", " это ", "предназначен", "предназначена")
        ):
            score += 1.5
        return score, -len(paragraph)

    best_index = max(range(len(paragraphs)), key=lambda index: paragraph_score(paragraphs[index]))
    selected_indices = {best_index}
    left = best_index - 1
    right = best_index + 1

    while left >= 0 or right < len(paragraphs):
        candidates: list[int] = []
        if right < len(paragraphs):
            candidates.append(right)
        if left >= 0:
            candidates.append(left)

        added = False
        for index in candidates:
            trial_indices = sorted(selected_indices | {index})
            candidate = "\n\n".join(paragraphs[item] for item in trial_indices)
            if len(candidate) <= max_chars:
                selected_indices.add(index)
                added = True
                if index == left:
                    left -= 1
                else:
                    right += 1
                break
        if not added:
            break

    excerpt = "\n\n".join(paragraphs[index] for index in sorted(selected_indices))
    return excerpt[:max_chars].rstrip()


def _content_terms(value: str) -> set[str]:
    return {
        token.casefold()
        for token in WORD_RE.findall(value)
        if len(token) >= 4 and token.casefold() not in QUERY_STOP_WORDS
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _page_label(page_start: int | None, page_end: int | None) -> str | None:
    if page_start is None:
        return None
    if page_end is None or page_end == page_start:
        return f"стр. {page_start}"
    return f"стр. {page_start}–{page_end}"


def _source_header(source: ContextSource) -> str:
    if source.source_scope == "knowledge_base":
        knowledge_base = source.knowledge_base_name or "База знаний"
        if source.knowledge_base_version is not None:
            knowledge_base = f"{knowledge_base}, версия {source.knowledge_base_version}"
        details = [f"база знаний: {knowledge_base}", source.filename]
    else:
        details = [source.filename]
    page_label = _page_label(source.page_start, source.page_end)
    if page_label:
        details.append(page_label)
    if source.heading:
        details.append(source.heading)
    return f"{source.citation} Источник: " + ", ".join(details)


def build_context(
    question: str,
    results: Iterable[SearchResultLike],
    *,
    max_context_chars: int,
    max_source_chars: int,
    max_sources: int,
    min_source_score: float = 0.05,
    relative_source_score: float = 0.25,
    source_similarity_threshold: float = 0.78,
    max_sources_per_document: int = 3,
    strong_evidence_score: float = 0.50,
    limited_evidence_score: float = 0.15,
) -> BuiltContext:
    candidates = list(results)
    candidate_scores = [source_quality_score(result) for result in candidates]
    best_score = max(candidate_scores, default=None)
    status = evidence_status(
        best_score,
        strong_threshold=strong_evidence_score,
        limited_threshold=limited_evidence_score,
    )

    if status == "insufficient":
        return BuiltContext(
            text="",
            sources=[],
            char_count=0,
            candidate_count=len(candidates),
            evidence_status=status,
            evidence_score=best_score,
            rejected_low_score=len(candidates),
            rejected_duplicate=0,
            rejected_document_cap=0,
        )

    sources: list[ContextSource] = []
    blocks: list[str] = []
    selected_terms: list[set[str]] = []
    document_counts: dict[uuid.UUID, int] = {}
    used_chars = 0
    rejected_low_score = 0
    rejected_duplicate = 0
    rejected_document_cap = 0
    score_floor = max(min_source_score, (best_score or 0.0) * relative_source_score)

    for result, quality_score in zip(candidates, candidate_scores, strict=True):
        if len(sources) >= max_sources:
            break
        if quality_score < score_floor:
            rejected_low_score += 1
            continue
        if document_counts.get(result.document_id, 0) >= max_sources_per_document:
            rejected_document_cap += 1
            continue

        source_text = result.text
        if len(source_text.strip()) < 240 and result.parent_text:
            source_text = result.parent_text

        excerpt = focused_context_excerpt(source_text, question, max_source_chars)
        if len(excerpt) < 40:
            rejected_low_score += 1
            continue

        terms = _content_terms(excerpt)
        if any(_jaccard(terms, existing) >= source_similarity_threshold for existing in selected_terms):
            rejected_duplicate += 1
            continue

        heading = result.heading_breadcrumb or result.heading
        source = ContextSource(
            index=len(sources) + 1,
            chunk_id=result.chunk_id,
            document_id=result.document_id,
            filename=result.filename,
            chunk_index=result.chunk_index,
            heading=heading,
            page_start=result.page_start,
            page_end=result.page_end,
            excerpt=excerpt,
            retrieval_rank=result.retrieval_rank,
            rerank_score=result.rerank_score,
            rerank_fusion_score=result.rerank_fusion_score,
            quality_score=quality_score,
            source_scope=getattr(result, "source_scope", "workspace"),
            knowledge_base_name=getattr(result, "knowledge_base_name", None),
            knowledge_base_version=getattr(result, "knowledge_base_version", None),
        )
        block = f"{_source_header(source)}\n{source.excerpt}"
        separator_chars = 2 if blocks else 0
        remaining = max_context_chars - used_chars - separator_chars
        if remaining < 120:
            break
        if len(block) > remaining:
            shortened_excerpt_chars = max(40, remaining - len(_source_header(source)) - 1)
            shortened_excerpt = source.excerpt[:shortened_excerpt_chars].rstrip()
            source = replace(source, excerpt=shortened_excerpt)
            block = f"{_source_header(source)}\n{source.excerpt}"

        sources.append(source)
        blocks.append(block)
        selected_terms.append(_content_terms(source.excerpt))
        document_counts[result.document_id] = document_counts.get(result.document_id, 0) + 1
        used_chars += len(block) + separator_chars

    if not sources:
        status = "insufficient"

    context_text = "\n\n".join(blocks)
    return BuiltContext(
        text=context_text,
        sources=sources,
        char_count=len(context_text),
        candidate_count=len(candidates),
        evidence_status=status,
        evidence_score=best_score,
        rejected_low_score=rejected_low_score,
        rejected_duplicate=rejected_duplicate,
        rejected_document_cap=rejected_document_cap,
    )
