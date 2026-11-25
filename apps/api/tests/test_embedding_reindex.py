import pytest

from app.evaluation.embedding_reindex import (
    parse_vector_dimension,
    validate_embedding_space,
)


def test_parse_vector_dimension() -> None:
    assert parse_vector_dimension("vector(384)") == 384
    assert parse_vector_dimension(" vector(1024) ") == 1024


@pytest.mark.parametrize("data_type", ["vector", "text", "vector(0)", "vector(384, 2)"])
def test_parse_vector_dimension_rejects_non_fixed_vector(data_type: str) -> None:
    with pytest.raises(RuntimeError, match="fixed pgvector dimension"):
        parse_vector_dimension(data_type)


def test_validate_embedding_space_accepts_exact_match() -> None:
    health = {"model": "BAAI/bge-m3", "output_dimension": 1024}

    assert validate_embedding_space(
        health,
        1024,
        expected_model="BAAI/bge-m3",
        expected_dimension=1024,
    ) == ("BAAI/bge-m3", 1024)


def test_validate_embedding_space_rejects_database_mismatch() -> None:
    health = {"model": "BAAI/bge-m3", "output_dimension": 1024}

    with pytest.raises(RuntimeError, match="Refusing to mix embedding spaces"):
        validate_embedding_space(health, 384)


def test_validate_embedding_space_rejects_unexpected_model() -> None:
    health = {"model": "other/model", "output_dimension": 384}

    with pytest.raises(RuntimeError, match="Embedding model mismatch"):
        validate_embedding_space(health, 384, expected_model="expected/model")


@pytest.mark.parametrize(
    "health",
    [
        {"output_dimension": 384},
        {"model": "model"},
        {"model": "model", "output_dimension": "384"},
        {"model": "model", "output_dimension": True},
    ],
)
def test_validate_embedding_space_rejects_invalid_health(health: dict[str, object]) -> None:
    with pytest.raises((RuntimeError, TypeError), match="readiness response"):
        validate_embedding_space(health, 384)
