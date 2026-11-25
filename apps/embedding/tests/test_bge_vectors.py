from __future__ import annotations

import numpy as np
import pytest

from app import bge_main


class FakeModel:
    def encode(self, inputs: list[str], **_: object) -> dict[str, np.ndarray]:
        vectors = np.zeros((len(inputs), bge_main.settings.embedding_dim), dtype=np.float32)
        vectors[:, 0] = 3
        vectors[:, 1] = 4
        return {"dense_vecs": vectors}


def test_bge_dense_vectors_keep_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bge_main, "get_model", lambda: FakeModel())
    vectors = bge_main._embed(["query", "passage"], normalize=True)
    assert len(vectors) == 2
    assert len(vectors[0]) == 1024
    assert np.linalg.norm(vectors[0]) == pytest.approx(1.0)


def test_bge_rejects_wrong_dimension() -> None:
    with pytest.raises(ValueError, match="does not match"):
        bge_main.prepare_vector(np.ones(3), 1024, normalize=True)
