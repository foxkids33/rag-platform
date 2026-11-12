"""Deterministic provenance for fenced code, not execution safety or entailment.

Only the excerpts actually supplied to the model are witnesses. A contiguous
extract, adapted command, or combination of different blocks is not a full-block
match, even when it might be useful or correct. No whitespace, parameters, or
configuration values are repaired by this audit.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_EXACT_TRANSCRIPTION_MARKERS = (
    "дословн",
    "точное содержим",
    "точный блок",
    "точную команд",
    "точные команды",
    "вербатим",
    "verbatim",
    "exact command",
    "exact configuration",
)


class CodeSourceLike(Protocol):
    index: int
    excerpt: str
    source_page_id: str | None
    source_version: str | None
    source_url: str | None


class CodeBlockWitness(BaseModel):
    source_index: int
    source_page_id: str | None = None
    source_version: str | None = None
    source_url: str | None = None


class CodeBlockIntegrity(BaseModel):
    index: int
    status: Literal["exact_match", "unverified"]
    sha256: str
    witnesses: list[CodeBlockWitness] = Field(default_factory=list)


class CodeIntegrityAudit(BaseModel):
    status: Literal["no_code", "exact_match", "unverified"] = "no_code"
    total_block_count: int = 0
    verified_block_count: int = 0
    blocks: list[CodeBlockIntegrity] = Field(default_factory=list)


@dataclass(frozen=True)
class _FencedBlock:
    content: str
    closed: bool


def _fenced_blocks(value: str) -> list[_FencedBlock]:
    """Keep code bytes after CRLF and one fence-separator newline normalization.

    An unfinished answer fence is deliberately returned as unverified; an
    unfinished source fence can never act as a witness. Fence length/language
    do not form part of the code, and longer matching closing fences are valid.
    """
    blocks: list[_FencedBlock] = []
    opening: str | None = None
    content: list[str] = []
    # Match the frontend's LF-only fence boundaries. str.splitlines also
    # splits Unicode separators that may legitimately belong to code text.
    for raw_line in re.findall(r"[^\n]*(?:\n|$)", value.replace("\r\n", "\n")):
        line = raw_line.removesuffix("\n")
        marker = _FENCE_RE.fullmatch(line)
        if opening is None:
            if marker:
                opening = marker[1]
                content = []
            continue
        if (
            marker
            and marker[1][0] == opening[0]
            and len(marker[1]) >= len(opening)
            and not marker[2].strip()
        ):
            # The newline immediately separating code from the closing fence
            # is syntax. A second trailing newline remains significant.
            blocks.append(_FencedBlock("".join(content).removesuffix("\n"), True))
            opening = None
            content = []
        else:
            content.append(raw_line)
    if opening is not None:
        blocks.append(_FencedBlock("".join(content), False))
    return blocks


def requires_exact_code_integrity(question: str) -> bool:
    """Enforce full-block fidelity only for explicit literal transcription.

    General questions asking which command to use can legitimately extract a
    line from a longer block; they still receive an unverified audit, not an
    unsupported claim that this is a full original block.
    """
    normalized = " ".join(question.casefold().split())
    return any(marker in normalized for marker in _EXACT_TRANSCRIPTION_MARKERS)


def audit_code_integrity(answer: str, sources: Iterable[CodeSourceLike]) -> CodeIntegrityAudit:
    witnesses_by_content: dict[str, list[CodeBlockWitness]] = {}
    for source in sources:
        witness = CodeBlockWitness(
            source_index=source.index,
            source_page_id=getattr(source, "source_page_id", None),
            source_version=getattr(source, "source_version", None),
            source_url=getattr(source, "source_url", None),
        )
        for block in _fenced_blocks(source.excerpt):
            if not block.closed:
                continue
            witnesses = witnesses_by_content.setdefault(block.content, [])
            if witness not in witnesses:
                witnesses.append(witness)

    audited: list[CodeBlockIntegrity] = []
    for index, block in enumerate(_fenced_blocks(answer), start=1):
        witnesses = witnesses_by_content.get(block.content, []) if block.closed else []
        audited.append(CodeBlockIntegrity(
            index=index,
            status="exact_match" if witnesses else "unverified",
            sha256=hashlib.sha256(block.content.encode("utf-8")).hexdigest(),
            witnesses=witnesses,
        ))
    verified = sum(block.status == "exact_match" for block in audited)
    return CodeIntegrityAudit(
        status=("no_code" if not audited else "exact_match" if verified == len(audited) else "unverified"),
        total_block_count=len(audited),
        verified_block_count=verified,
        blocks=audited,
    )
