import numpy as np
import pytest

from app.main import resize_vector


def test_resize_vector_pads_and_normalizes() -> None:
    result = resize_vector(np.array([3.0, 4.0], dtype=np.float32), 4)

    assert len(result) == 4
    assert result[:2] == pytest.approx([0.6, 0.8])
    assert result[2:] == [0.0, 0.0]
