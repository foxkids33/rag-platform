from __future__ import annotations

import json

import pytest

from app.evaluation.dataset import DatasetError, load_case_ids, load_cases, select_cases


def test_load_cases_supports_stable_filenames(tmp_path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "fact-001",
                "question": "Какой срок хранения?",
                "expected_filenames": ["policy.txt"],
                "question_type": "exact_value",
                "required_facts": ["30 дней"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    cases = load_cases(dataset)

    assert len(cases) == 1
    assert cases[0].expected_filenames == ("policy.txt",)
    assert cases[0].required_facts == ("30 дней",)


def test_load_cases_rejects_duplicate_ids(tmp_path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    row = json.dumps({"id": "same", "question": "Вопрос"}, ensure_ascii=False)
    dataset.write_text(f"{row}\n{row}\n", encoding="utf-8")

    with pytest.raises(DatasetError, match="duplicate case id"):
        load_cases(dataset)


def test_case_id_split_preserves_dataset_order(tmp_path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        "\n".join(
            json.dumps({"id": case_id, "question": case_id})
            for case_id in ("case-a", "case-b", "case-c")
        )
        + "\n",
        encoding="utf-8",
    )
    split = tmp_path / "split.txt"
    split.write_text("# acceptance\ncase-c\ncase-a\n", encoding="utf-8")

    selected = select_cases(load_cases(dataset), load_case_ids(split))

    assert [case.id for case in selected] == ["case-a", "case-c"]


def test_case_id_split_rejects_unknown_ids(tmp_path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"id":"case-a","question":"A"}\n', encoding="utf-8")

    with pytest.raises(DatasetError, match="Unknown case ids"):
        select_cases(load_cases(dataset), ("case-b",))
