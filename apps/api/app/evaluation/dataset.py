"""Load versionable JSONL evaluation datasets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evaluation.models import EvaluationCase


class DatasetError(ValueError):
    """Raised when an evaluation dataset is malformed."""


def _string_tuple(payload: dict[str, Any], key: str, line_number: int) -> tuple[str, ...]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DatasetError(f"Line {line_number}: {key} must be a list of strings")
    return tuple(item.strip() for item in value if item.strip())


def load_cases(path: str | Path) -> list[EvaluationCase]:
    """Read one JSON object per line and validate stable case identifiers."""

    source = Path(path)
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()

    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetError(f"Cannot read dataset {source}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"Line {line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise DatasetError(f"Line {line_number}: expected a JSON object")

        case_id = payload.get("id")
        question = payload.get("question")
        if not isinstance(case_id, str) or not isinstance(question, str):
            raise DatasetError(f"Line {line_number}: id and question must be strings")
        if case_id in seen_ids:
            raise DatasetError(f"Line {line_number}: duplicate case id {case_id!r}")

        expected_abstention = payload.get("expected_abstention", False)
        if not isinstance(expected_abstention, bool):
            raise DatasetError(f"Line {line_number}: expected_abstention must be a boolean")

        try:
            case = EvaluationCase(
                id=case_id,
                question=question,
                expected_document_ids=_string_tuple(payload, "expected_document_ids", line_number),
                expected_filenames=_string_tuple(payload, "expected_filenames", line_number),
                expected_abstention=expected_abstention,
                question_type=payload.get("question_type"),
                expected_answer=payload.get("expected_answer"),
                required_facts=_string_tuple(payload, "required_facts", line_number),
                forbidden_facts=_string_tuple(payload, "forbidden_facts", line_number),
                difficulty=payload.get("difficulty"),
                notes=payload.get("notes"),
            )
        except ValueError as exc:
            raise DatasetError(f"Line {line_number}: {exc}") from exc

        seen_ids.add(case.id)
        cases.append(case)

    if not cases:
        raise DatasetError("Dataset contains no evaluation cases")
    return cases


def load_case_ids(path: str | Path) -> tuple[str, ...]:
    """Read a versioned list of case ids, ignoring blank lines and comments."""

    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetError(f"Cannot read case-id split {source}: {exc}") from exc

    case_ids: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        case_id = raw_line.strip()
        if not case_id or case_id.startswith("#"):
            continue
        if case_id in seen:
            raise DatasetError(
                f"Line {line_number}: duplicate case id {case_id!r} in {source}"
            )
        seen.add(case_id)
        case_ids.append(case_id)

    if not case_ids:
        raise DatasetError(f"Case-id split {source} contains no case ids")
    return tuple(case_ids)


def select_cases(
    cases: list[EvaluationCase],
    case_ids: tuple[str, ...],
) -> list[EvaluationCase]:
    """Select a split while preserving the canonical dataset order."""

    requested = set(case_ids)
    available = {case.id for case in cases}
    unknown = sorted(requested - available)
    if unknown:
        raise DatasetError("Unknown case ids in split: " + ", ".join(unknown))
    selected = [case for case in cases if case.id in requested]
    if not selected:
        raise DatasetError("Case-id split selected no evaluation cases")
    return selected
