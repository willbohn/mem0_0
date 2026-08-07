"""Regression test: SentenceTransformerReranker fallback must not mutate caller documents.

Fakes the ``CrossEncoder`` class (mirroring the ``mock_cohere`` / ``mock_zero_entropy``
pattern in ``test_reranker_fallback_topk.py``) so the reranker constructs without
downloading real weights, then forces the predict call to fail.
"""

from unittest.mock import MagicMock

import pytest

from mem0.configs.rerankers.sentence_transformer import SentenceTransformerRerankerConfig
from mem0.reranker.sentence_transformer_reranker import SENTENCE_TRANSFORMERS_AVAILABLE


@pytest.fixture
def mock_sentence_transformer_model(monkeypatch):
    """Fake the CrossEncoder class so SentenceTransformerReranker constructs without downloading weights."""
    import mem0.reranker.sentence_transformer_reranker as st_reranker

    fake_model = MagicMock()
    fake_cross_encoder_cls = MagicMock(return_value=fake_model)

    monkeypatch.setattr(st_reranker, "CrossEncoder", fake_cross_encoder_cls)

    reranker = st_reranker.SentenceTransformerReranker(SentenceTransformerRerankerConfig())
    return reranker, fake_model


@pytest.mark.skipif(not SENTENCE_TRANSFORMERS_AVAILABLE, reason="sentence-transformers not installed")
class TestSentenceTransformerFallbackNonMutation:
    def test_failed_rerank_does_not_mutate_caller_documents(self, mock_sentence_transformer_model):
        reranker, fake_model = mock_sentence_transformer_model
        fake_model.predict.side_effect = RuntimeError("predict error")

        docs = [{"memory": "a"}, {"memory": "b"}]
        original = [d.copy() for d in docs]

        result = reranker.rerank("q", docs, top_k=2)

        assert docs == original
        assert all(item["rerank_score"] == 0.0 for item in result)
