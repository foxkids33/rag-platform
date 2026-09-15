import numpy as np
import pytest

from app.main import prepare_inputs, prepare_vector


def test_prepare_vector_normalizes_without_resizing() -> None:
    result = prepare_vector(np.array([3.0, 4.0], dtype=np.float32), 2)

    assert result == pytest.approx([0.6, 0.8])


def test_prepare_vector_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        prepare_vector(np.array([3.0, 4.0], dtype=np.float32), 4)


def test_prepare_inputs_adds_e5_retrieval_prefixes() -> None:
    assert prepare_inputs(
        ["как настроить кластер"],
        "query",
        "intfloat/multilingual-e5-small",
    ) == ["query: как настроить кластер"]
    assert prepare_inputs(
        ["инструкция по настройке"],
        "passage",
        "intfloat/multilingual-e5-small",
    ) == ["passage: инструкция по настройке"]


def test_prepare_inputs_does_not_duplicate_existing_prefix() -> None:
    assert prepare_inputs(
        ["query: как настроить кластер"],
        "query",
        "intfloat/multilingual-e5-small",
    ) == ["query: как настроить кластер"]


def test_prepare_inputs_keeps_minilm_text_unchanged() -> None:
    inputs = ["Текст технической документации"]

    assert prepare_inputs(
        inputs,
        "passage",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    ) == inputs
