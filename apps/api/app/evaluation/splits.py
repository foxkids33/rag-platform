"""Validate that dev and acceptance case-id files partition a dataset."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from app.evaluation.dataset import DatasetError, load_case_ids, load_cases


def validate_partition(
    dataset: Path,
    dev: Path,
    acceptance: Path,
) -> dict[str, Any]:
    cases = load_cases(dataset)
    dataset_ids = {case.id for case in cases}
    dev_ids = set(load_case_ids(dev))
    acceptance_ids = set(load_case_ids(acceptance))

    overlap = sorted(dev_ids & acceptance_ids)
    unknown = sorted((dev_ids | acceptance_ids) - dataset_ids)
    missing = sorted(dataset_ids - (dev_ids | acceptance_ids))
    problems: list[str] = []
    if overlap:
        problems.append("overlap: " + ", ".join(overlap))
    if unknown:
        problems.append("unknown: " + ", ".join(unknown))
    if missing:
        problems.append("missing: " + ", ".join(missing))
    if problems:
        raise DatasetError("Invalid dataset partition:\n- " + "\n- ".join(problems))

    case_types = {case.id: case.question_type or "unspecified" for case in cases}

    def summary(case_ids: set[str]) -> dict[str, Any]:
        return {
            "case_count": len(case_ids),
            "question_type_counts": dict(
                sorted(Counter(case_types[case_id] for case_id in case_ids).items())
            ),
        }

    return {
        "dataset_case_count": len(cases),
        "dev": summary(dev_ids),
        "acceptance": summary(acceptance_ids),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        summary = validate_partition(args.dataset, args.dev, args.acceptance)
    except DatasetError as exc:
        print(f"Split validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
