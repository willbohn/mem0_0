import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

from mem0.utils import spacy_models


def test_missing_spacy_model_falls_back_without_runtime_download(monkeypatch):
    spacy = ModuleType("spacy")
    spacy.util = SimpleNamespace(is_package=lambda _name: False)
    spacy_cli = ModuleType("spacy.cli")
    download = MagicMock()
    spacy_cli.download = download
    monkeypatch.setitem(sys.modules, "spacy", spacy)
    monkeypatch.setitem(sys.modules, "spacy.cli", spacy_cli)
    monkeypatch.setattr(spacy_models, "_nlp_lemma", None)
    monkeypatch.setattr(spacy_models, "_load_failed_lemma", False)

    assert spacy_models.get_nlp_lemma() is None
    download.assert_not_called()
