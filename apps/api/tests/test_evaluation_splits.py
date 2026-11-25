from __future__ import annotations

import json

import pytest

from app.evaluation.dataset import DatasetError
from app.evaluation.splits import validate_partition


def _write_dataset(path) -> None:
    path.write_text(
        "\n".join(
            json.dumps(
                {"id": case_id, "question": case_id, "question_type": question_type}
            )
            for case_id, question_type in (
                ("case-a", "direct_fact"),
                ("case-b", "exact_value"),
                ("case-c", "out_of_scope"),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_validate_partition_reports_split_composition(tmp_path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dev = tmp_path / "dev.txt"
    acceptance = tmp_path / "acceptance.txt"
    _write_dataset(dataset)
    dev.write_text("case-a\ncase-b\n", encoding="utf-8")
    acceptance.write_text("case-c\n", encoding="utf-8")

    summary = validate_partition(dataset, dev, acceptance)

    assert summary["dataset_case_count"] == 3
    assert summary["dev"]["case_count"] == 2
    assert summary["acceptance"]["question_type_counts"] == {"out_of_scope": 1}


def test_validate_partition_rejects_overlap_and_missing_cases(tmp_path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dev = tmp_path / "dev.txt"
    acceptance = tmp_path / "acceptance.txt"
    _write_dataset(dataset)
    dev.write_text("case-a\ncase-b\n", encoding="utf-8")
    acceptance.write_text("case-b\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="overlap"):
        validate_partition(dataset, dev, acceptance)
