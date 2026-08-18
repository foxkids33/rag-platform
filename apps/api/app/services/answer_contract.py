from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

ANSWER_MARKER = "[[ANSWER]]"
ABSTAIN_MARKER = "[[ABSTAIN]]"
CONTROL_MARKERS = (ANSWER_MARKER, ABSTAIN_MARKER)
INSUFFICIENT_EVIDENCE_ANSWER = (
    "В доступных документах недостаточно подтверждённой информации, "
    "чтобы надёжно ответить на этот вопрос."
)

AbstentionReason = Literal[
    "insufficient_retrieval_evidence",
    "model_reported_insufficient_context",
]

_LEGACY_REFUSAL_PREFIXES = (
    "в контексте нет ",
    "в предоставленном контексте нет ",
    "в актуальном контексте нет ",
    "в доступном контексте нет ",
    "в контексте предоставленных источников нет ",
    "в контексте отсутств",
    "в предоставленных документах нет ",
    "в доступных документах недостаточно ",
    "предоставленный контекст не содержит ",
    "актуальный контекст не содержит ",
    "данные отсутствуют в предоставленном контексте",
    "the provided context does not ",
    "the available context does not ",
    "the available documents do not ",
    "there is insufficient information in the provided context",
)
_CITATION_RE = re.compile(r"\[\d+\]")


@dataclass(frozen=True)
class ResolvedAnswer:
    text: str
    abstained: bool
    abstention_reason: AbstentionReason | None
    marker_used: bool = False

    @property
    def has_citations(self) -> bool:
        return _CITATION_RE.search(self.text) is not None


def _first_control_marker(text: str) -> str | None:
    positions = [
        (position, marker)
        for marker in CONTROL_MARKERS
        if (position := text.find(marker)) >= 0
    ]
    if not positions:
        return None
    return min(positions, key=lambda item: item[0])[1]


def _without_control_markers(text: str) -> str:
    for marker in CONTROL_MARKERS:
        text = text.replace(marker, "")
    return text.strip()


def resolve_generated_answer(text: str) -> ResolvedAnswer:
    """Convert the model's explicit refusal contract into API metadata.

    An explicit marker is authoritative even when an imperfect model places it
    after the user-facing text. Prefix matching keeps older prompts and model
    responses machine-readable when no control marker is present.
    """

    stripped = text.strip()
    control_marker = _first_control_marker(stripped)
    cleaned = _without_control_markers(stripped)
    if control_marker == ABSTAIN_MARKER:
        return ResolvedAnswer(
            text=cleaned or INSUFFICIENT_EVIDENCE_ANSWER,
            abstained=True,
            abstention_reason="model_reported_insufficient_context",
            marker_used=True,
        )
    if control_marker == ANSWER_MARKER:
        if cleaned:
            return ResolvedAnswer(
                text=cleaned,
                abstained=False,
                abstention_reason=None,
                marker_used=True,
            )
        return ResolvedAnswer(
            text=INSUFFICIENT_EVIDENCE_ANSWER,
            abstained=True,
            abstention_reason="model_reported_insufficient_context",
            marker_used=True,
        )

    normalized = " ".join(stripped.casefold().split())
    if normalized.startswith(_LEGACY_REFUSAL_PREFIXES):
        return ResolvedAnswer(
            text=stripped,
            abstained=True,
            abstention_reason="model_reported_insufficient_context",
        )

    return ResolvedAnswer(
        text=stripped,
        abstained=False,
        abstention_reason=None,
    )


@dataclass
class AbstentionStreamFilter:
    """Remove control markers even when they are split across stream tokens."""

    _raw_parts: list[str] = field(default_factory=list)
    _pending: str = ""
    marker_detected: bool = False
    control_marker: str | None = None

    def feed(self, token: str) -> str:
        self._raw_parts.append(token)
        self._pending += token
        visible: list[str] = []
        while self._pending:
            matches = [
                (position, marker)
                for marker in CONTROL_MARKERS
                if (position := self._pending.find(marker)) >= 0
            ]
            if matches:
                position, marker = min(matches, key=lambda item: item[0])
                visible.append(self._pending[:position])
                self._pending = self._pending[position + len(marker) :]
                self.marker_detected = True
                if self.control_marker is None:
                    self.control_marker = marker
                continue

            held_suffix = max(
                (
                    size
                    for marker in CONTROL_MARKERS
                    for size in range(1, min(len(marker), len(self._pending) + 1))
                    if marker.startswith(self._pending[-size:])
                ),
                default=0,
            )
            if held_suffix:
                visible.append(self._pending[:-held_suffix])
                self._pending = self._pending[-held_suffix:]
            else:
                visible.append(self._pending)
                self._pending = ""
            break
        return "".join(visible)

    def finish(self) -> str:
        visible = self._pending
        self._pending = ""
        return visible

    @property
    def raw_text(self) -> str:
        return "".join(self._raw_parts)
