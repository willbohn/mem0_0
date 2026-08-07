"""Unit tests for HuggingFaceReranker score normalization.

These exercise the pure ``_normalize_scores`` helper directly, so they do not
require ``transformers`` / ``torch`` to be installed.
"""

import math
from unittest.mock import MagicMock

import pytest

from mem0.configs.rerankers.huggingface import HuggingFaceRerankerConfig
from mem0.reranker.huggingface_reranker import TRANSFORMERS_AVAILABLE, HuggingFaceReranker


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


class TestHuggingFaceNormalizeScores:
    def test_logits_mapped_via_sigmoid(self):
        scores = HuggingFaceReranker._normalize_scores([2.0, 8.0, 5.0])
        assert scores == pytest.approx([_sigmoid(2.0), _sigmoid(8.0), _sigmoid(5.0)])

    def test_output_bounded_between_zero_and_one(self):
        for s in HuggingFaceReranker._normalize_scores([-12.0, -1.0, 0.0, 3.0, 15.0]):
            assert 0.0 <= s <= 1.0

    def test_sigmoid_preserves_ranking_order(self):
        raw = [1.0, -4.0, 9.0, 2.5]
        normalized = HuggingFaceReranker._normalize_scores(raw)
        # argsort of raw and normalized must match — sigmoid is monotonic.
        assert sorted(range(len(raw)), key=lambda i: raw[i]) == sorted(
            range(len(normalized)), key=lambda i: normalized[i]
        )

    def test_single_score_not_collapsed_to_zero(self):
        # Regression: a lone document used to normalize to ~0.0 under min-max.
        # A positive logit must now yield a clearly-relevant score (> 0.5).
        (score,) = HuggingFaceReranker._normalize_scores([4.2])
        assert score == pytest.approx(_sigmoid(4.2))
        assert score > 0.5

    def test_tied_scores_not_collapsed_to_zero(self):
        # Regression: tied candidates all collapsed to ~0.0 under min-max.
        scores = HuggingFaceReranker._normalize_scores([3.0, 3.0, 3.0])
        assert scores == pytest.approx([_sigmoid(3.0)] * 3)

    def test_zero_logit_maps_to_half(self):
        assert HuggingFaceReranker._normalize_scores([0.0]) == pytest.approx([0.5])

    def test_empty_scores(self):
        assert HuggingFaceReranker._normalize_scores([]) == []


@pytest.fixture
def mock_huggingface_model(monkeypatch):
    """Fake tokenizer/model classes so HuggingFaceReranker constructs without downloading weights."""
    import mem0.reranker.huggingface_reranker as hf_reranker

    fake_tokenizer = MagicMock()
    fake_model = MagicMock()
    fake_model.to.return_value = fake_model

    fake_tokenizer_cls = MagicMock()
    fake_tokenizer_cls.from_pretrained.return_value = fake_tokenizer
    fake_model_cls = MagicMock()
    fake_model_cls.from_pretrained.return_value = fake_model

    monkeypatch.setattr(hf_reranker, "AutoTokenizer", fake_tokenizer_cls)
    monkeypatch.setattr(hf_reranker, "AutoModelForSequenceClassification", fake_model_cls)

    reranker = hf_reranker.HuggingFaceReranker(HuggingFaceRerankerConfig())
    return reranker, fake_tokenizer


@pytest.mark.skipif(not TRANSFORMERS_AVAILABLE, reason="transformers/torch not installed")
class TestHuggingFaceFallbackNonMutation:
    def test_failed_rerank_does_not_mutate_caller_documents(self, mock_huggingface_model):
        reranker, fake_tokenizer = mock_huggingface_model
        fake_tokenizer.side_effect = RuntimeError("tokenizer error")

        docs = [{"memory": "a"}, {"memory": "b"}]
        original = [d.copy() for d in docs]

        result = reranker.rerank("q", docs, top_k=2)

        assert docs == original
        assert all(item["rerank_score"] == 0.0 for item in result)
