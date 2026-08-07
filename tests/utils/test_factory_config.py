def test_llm_factory_does_not_mutate_caller_dict_config(monkeypatch):
    from mem0.utils import factory

    captured = {}

    class DummyLLM:
        def __init__(self, config):
            captured["config"] = config

    monkeypatch.setattr(factory, "load_class", lambda _path: DummyLLM)
    caller_config = {"model": "m"}
    factory.LlmFactory.create("openai", caller_config, temperature=0.5)
    assert caller_config == {"model": "m"}
