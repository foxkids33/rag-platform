import numpy as np
import pytest

from app.main import prepare_vector


def test_prepare_vector_normalizes_without_resizing() -> None:
    result = prepare_vector(np.array([3.0, 4.0], dtype=np.float32), 2)

    assert result == pytest.approx([0.6, 0.8])


def test_prepare_vector_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        prepare_vector(np.array([3.0, 4.0], dtype=np.float32), 4)
