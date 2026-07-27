from app.api.routes.search import _candidate_limit, _vector_literal


def test_candidate_limit_has_retrieval_headroom() -> None:
    assert _candidate_limit(1) == 40
    assert _candidate_limit(8) == 64
    assert _candidate_limit(20) == 160


def test_vector_literal_is_pgvector_compatible() -> None:
    assert _vector_literal([0.5, -1.25, 0.0]) == "[0.5,-1.25,0]"
