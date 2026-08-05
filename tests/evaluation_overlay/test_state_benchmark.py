import json

from evaluation_overlay.state_benchmark import RestClient, main, memora_cases, run_case, stale_cases


def test_stale_adapter_preserves_old_and_new_session_labels(tmp_path):
    path = tmp_path / "STALE.json"
    path.write_text(
        json.dumps(
            [
                {
                    "uid": "case-1",
                    "haystack_session": [
                        [{"role": "user", "content": "old"}],
                        [{"role": "user", "content": "noise"}],
                        [{"role": "user", "content": "new"}],
                    ],
                    "relevant_session_index": [0, 2],
                    "probing_queries": {"dim1_query": "current?", "dim3_query": "act"},
                }
            ]
        ),
        encoding="utf-8",
    )

    case = next(iter(stale_cases(path)))

    assert case["current_session_ids"] == {2}
    assert case["stale_session_ids"] == {0}
    assert [query["question_id"] for query in case["queries"]] == ["case-1:dim1", "case-1:dim3"]


def test_memora_adapter_maps_presence_and_forgetting_evidence_to_sessions(tmp_path):
    persona = tmp_path / "weekly" / "persona"
    conversations = persona / "conversations"
    conversations.mkdir(parents=True)
    for session_id in (1, 2):
        (conversations / f"session_{session_id:04d}.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "conversation": [
                        {"speaker": "user_agent", "message": f"message {session_id}"},
                        {"speaker": "ai_agent", "message": "ack"},
                    ],
                }
            ),
            encoding="utf-8",
        )
    (persona / "evaluation_questions_persona.json").write_text(
        json.dumps(
            {
                "questions": {
                    "remembering": [
                        {
                            "question_id": "q1",
                            "question": "what is current?",
                            "memory_evidence": {"session_history": [2]},
                            "forgetting_evidence": {"items": [{"session_id": 1}]},
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    case = next(iter(memora_cases(tmp_path)))

    assert case["session_ids"] == [1, 2]
    assert case["queries"][0]["current_session_ids"] == {2}
    assert case["queries"][0]["stale_session_ids"] == {1}


def test_resume_rebuilds_aggregate_only_from_atomic_complete_case_checkpoints(tmp_path, monkeypatch):
    case = {"case_id": "case-1", "queries": [{"question_id": "q1"}, {"question_id": "q2"}]}
    observations = [
        {"case_id": "case-1", "question_id": "q1"},
        {"case_id": "case-1", "question_id": "q2"},
    ]
    calls = []
    monkeypatch.setattr("evaluation_overlay.state_benchmark.stale_cases", lambda path: iter([case]))
    monkeypatch.setattr("evaluation_overlay.state_benchmark.RestClient", lambda *args, **kwargs: object())

    def fake_run_case(*args, **kwargs):
        calls.append(1)
        return observations

    monkeypatch.setattr("evaluation_overlay.state_benchmark.run_case", fake_run_case)
    monkeypatch.setenv("MEM0_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("MEM0_BENCH_STORE_ID", "test")
    output_dir = tmp_path / "output"

    assert main(["--format", "stale", "--dataset-path", str(tmp_path / "unused"), "--output-dir", str(output_dir)]) == 0
    (output_dir / "observations.jsonl").write_text(json.dumps(observations[0]) + "\n", encoding="utf-8")
    assert (
        main(
            [
                "--format",
                "stale",
                "--dataset-path",
                str(tmp_path / "unused"),
                "--output-dir",
                str(output_dir),
                "--resume",
            ]
        )
        == 0
    )

    rebuilt = [json.loads(line) for line in (output_dir / "observations.jsonl").read_text().splitlines()]
    assert rebuilt == observations
    assert calls == [1]


def test_stale_run_reports_false_supersession_from_current_evidence_status():
    class Client:
        def __init__(self):
            self.added = 0

        def clear_user(self, user_id):
            return None

        def add(self, user_id, messages):
            self.added += 1
            return [f"m{self.added}"], 1.0

        def search(self, user_id, query, top_k):
            return [{"id": "m2", "memory": "new"}], 2.0

        def statuses(self, memory_ids):
            return {"m2": "superseded"}

    observations = run_case(
        Client(),
        {
            "case_id": "c1",
            "sessions": [
                [{"role": "user", "content": "old"}],
                [{"role": "user", "content": "new"}],
            ],
            "queries": [{"question_id": "c1:dim1", "query": "current?"}],
            "current_session_ids": {1},
            "stale_session_ids": {0},
        },
        store_id="test",
        benchmark="stale",
        top_k=10,
    )

    assert observations[0]["false_supersession"] is True


def test_rest_client_accepts_legacy_list_add_envelope(monkeypatch):
    client = RestClient("http://127.0.0.1:1")
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: [{"id": "m1"}])

    memory_ids, latency = client.add("u1", [{"role": "user", "content": "value"}])

    assert memory_ids == ["m1"]
    assert latency >= 0
