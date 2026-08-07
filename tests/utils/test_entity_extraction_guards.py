"""Crash-hardening tests for entity extraction internals.

Unlike test_entity_extraction.py, these tests exercise private helpers with
doubles instead of real spaCy Doc objects, so they do not require spaCy to
be installed.
"""

from mem0.utils.entity_extraction import _add_topic_phrase_candidates


class _RaisingNounChunksDoc:
    ents: list = []

    @property
    def noun_chunks(self):
        raise NotImplementedError("noun_chunks requires the dependency parser, which was not loaded")


def test_add_topic_phrase_candidates_survives_noun_chunks_not_implemented():
    candidates = []

    _add_topic_phrase_candidates(_RaisingNounChunksDoc(), candidates)

    assert candidates == []
