from __future__ import annotations

import json

import pytest

from app.evaluation.dataset import DatasetError, load_cases


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
