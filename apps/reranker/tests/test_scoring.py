import torch

from app.main import _logits_to_scores


def test_logits_to_scores_supports_binary_classifier() -> None:
    logits = torch.tensor([[1.0, 3.0], [2.5, 1.0]])

    scores = _logits_to_scores(logits)

    assert torch.equal(scores, torch.tensor([2.0, -1.5]))


def test_logits_to_scores_supports_single_logit() -> None:
    logits = torch.tensor([[0.25], [-0.5]])

    assert torch.equal(_logits_to_scores(logits), torch.tensor([0.25, -0.5]))
