from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from evaluation_overlay.normalize import candidate_score, memory_fingerprint


def _load_records(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        for key in ("data", "records"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{path} must contain a JSON list of objects")
    return value


def stale_cases(dataset_path: Path) -> Iterable[dict[str, Any]]:
    for record in _load_records(dataset_path):
        indices = record.get("relevant_session_index") or []
        if len(indices) != 2:
            raise ValueError(f"STALE record {record.get('uid')} has no old/new session indices")
        yield {
            "case_id": str(record["uid"]),
            "sessions": record["haystack_session"],
            "queries": [
                {"question_id": f"{record['uid']}:{dimension.removesuffix('_query')}", "query": query}
                for dimension, query in sorted(record["probing_queries"].items())
            ],
            "current_session_ids": {int(indices[1])},
            "stale_session_ids": {int(indices[0])},
        }


def _collect_session_ids(value: Any) -> set[int]:
    found: set[int] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "session_id" and isinstance(child, int):
                found.add(child)
            elif key in {"session_history", "session_ids"} and isinstance(child, list):
                found.update(item for item in child if isinstance(item, int))
            else:
                found.update(_collect_session_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_session_ids(child))
    return found


def memora_cases(dataset_dir: Path) -> Iterable[dict[str, Any]]:
    question_files = sorted(dataset_dir.glob("**/evaluation_questions_*.json"))
    if not question_files:
        raise ValueError(f"no Memora evaluation question files found below {dataset_dir}")
    for question_path in question_files:
        persona_dir = question_path.parent
        sessions = []
        session_ids = []
        for session_path in sorted((persona_dir / "conversations").glob("session_*.json")):
            session = json.loads(session_path.read_text(encoding="utf-8"))
            messages = []
            for turn in session.get("conversation", []):
                role = "user" if turn.get("speaker") == "user_agent" else "assistant"
                messages.append({"role": role, "content": str(turn.get("message", ""))})
            sessions.append(messages)
            session_ids.append(int(session["session_id"]))

        value = json.loads(question_path.read_text(encoding="utf-8"))
        queries = []
        for category, questions in sorted(value.get("questions", {}).items()):
            for question in questions:
                queries.append(
                    {
                        "question_id": str(question["question_id"]),
                        "query": str(question["question"]),
                        "category": category,
                        "current_session_ids": _collect_session_ids(question.get("memory_evidence")),
                        "stale_session_ids": _collect_session_ids(question.get("forgetting_evidence")),
                    }
                )
        relative = persona_dir.relative_to(dataset_dir)
        yield {
            "case_id": str(relative).replace(os.sep, ":"),
            "sessions": sessions,
            "session_ids": session_ids,
            "queries": queries,
        }


class RestClient:
    def __init__(self, host: str, api_key: str | None = None):
        self.host = host.rstrip("/")
        self.api_key = api_key

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        request = urllib.request.Request(f"{self.host}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Mem0 HTTP {exc.code} for {method} {path}: {detail[:500]}") from exc

    def clear_user(self, user_id: str) -> None:
        self.request("DELETE", f"/memories?{urllib.parse.urlencode({'user_id': user_id})}")

    def add(self, user_id: str, messages: list[dict[str, str]]) -> tuple[list[str], float]:
        started = time.perf_counter()
        response = self.request("POST", "/memories", {"messages": messages, "user_id": user_id})
        elapsed = (time.perf_counter() - started) * 1000
        results = response.get("results", []) if isinstance(response, dict) else response
        if not isinstance(results, list):
            raise RuntimeError("Mem0 add returned an unexpected response")
        return [str(item["id"]) for item in results if item.get("id")], elapsed

    def search(self, user_id: str, query: str, top_k: int) -> tuple[list[dict[str, Any]], float]:
        started = time.perf_counter()
        response = self.request("POST", "/search", {"query": query, "user_id": user_id, "limit": top_k})
        elapsed = (time.perf_counter() - started) * 1000
        results = response.get("results", response) if isinstance(response, dict) else response
        if not isinstance(results, list):
            raise RuntimeError("Mem0 search returned an unexpected response")
        return results, elapsed

    def statuses(self, memory_ids: set[str]) -> dict[str, str]:
        response = self.request("POST", "/benchmark-status", {"ids": sorted(memory_ids)})
        if not isinstance(response, dict):
            raise RuntimeError("Mem0 benchmark status returned an unexpected response")
        return {str(key): str(value) for key, value in response.items()}


def _normalized_messages(session: Any) -> list[dict[str, str]]:
    messages = []
    for message in session if isinstance(session, list) else []:
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            messages.append({"role": str(message.get("role", "user")), "content": content})
    return messages


def run_case(
    client: RestClient, case: dict[str, Any], *, store_id: str, benchmark: str, top_k: int
) -> list[dict[str, Any]]:
    digest = hashlib.sha256(f"{store_id}:{benchmark}:{case['case_id']}".encode()).hexdigest()[:20]
    user_id = f"mem0_0_bench_{digest}"
    client.clear_user(user_id)
    ids_by_session: dict[int, set[str]] = {}
    write_latencies = []
    for offset, session in enumerate(case["sessions"]):
        messages = _normalized_messages(session)
        if not messages:
            continue
        memory_ids, elapsed = client.add(user_id, messages)
        write_latencies.append(elapsed)
        session_id = int(case.get("session_ids", [])[offset]) if case.get("session_ids") else offset
        ids_by_session[session_id] = set(memory_ids)

    observations = []
    for query_index, query in enumerate(case["queries"]):
        current_sessions = query.get("current_session_ids", case.get("current_session_ids", set()))
        stale_sessions = query.get("stale_session_ids", case.get("stale_session_ids", set()))
        current_ids = set().union(*(ids_by_session.get(int(value), set()) for value in current_sessions))
        stale_ids = set().union(*(ids_by_session.get(int(value), set()) for value in stale_sessions))
        results, search_latency = client.search(user_id, query["query"], top_k)
        observation = {
            "case_id": str(case["case_id"]),
            "question_id": query["question_id"],
            "candidate_ids": [str(item["id"]) for item in results if item.get("id")],
            "candidate_fingerprints": [memory_fingerprint(item.get("memory", "")) for item in results],
            "candidate_scores": [candidate_score(item) for item in results],
            "relevant_ids": sorted(current_ids),
            "current_relevant_ids": sorted(current_ids),
            "stale_ids": sorted(stale_ids),
            "retrieved_chars": sum(len(str(item.get("memory", ""))) for item in results),
            "search_latency_ms": search_latency,
        }
        if benchmark == "stale" and current_ids:
            statuses = client.statuses(current_ids)
            observation["false_supersession"] = any(
                statuses.get(memory_id, "missing") in {"superseded", "archived", "invalid"} for memory_id in current_ids
            )
        if query_index == 0:
            observation["write_latency_samples_ms"] = write_latencies
        observations.append(observation)
    return observations


def _atomic_write_observations(path: Path, observations: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for observation in observations:
            handle.write(json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _load_observation_lines(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        values = [json.loads(line) for line in handle if line.strip()]
    if not values or not all(isinstance(value, dict) for value in values):
        raise ValueError(f"invalid observation checkpoint {path}")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run state-aware retrieval on pinned STALE or Memora data")
    parser.add_argument("--format", required=True, choices=("stale", "memora"))
    parser.add_argument("--dataset-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=200)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    if args.format == "stale":
        cases = stale_cases(args.dataset_path)
    else:
        cases = memora_cases(args.dataset_path)
    cases = list(cases)
    if args.limit is not None:
        cases = cases[: args.limit]

    output_path = args.output_dir / "observations.jsonl"
    cases_dir = args.output_dir / "cases"
    existing_case_paths = sorted(cases_dir.glob("*.jsonl")) if cases_dir.exists() else []
    if existing_case_paths and not args.resume:
        raise FileExistsError(
            f"case checkpoints exist below {cases_dir}; pass --resume or use a fresh output directory"
        )
    completed: set[str] = set()
    if args.resume:
        for case_path in existing_case_paths:
            values = _load_observation_lines(case_path)
            case_ids = {str(value.get("case_id")) for value in values}
            if len(case_ids) != 1:
                raise ValueError(f"observation checkpoint {case_path} contains multiple cases")
            completed.update(case_ids)

    client = RestClient(os.environ["MEM0_HOST"], os.environ.get("MEM0_API_KEY"))
    store_id = os.environ["MEM0_BENCH_STORE_ID"]
    for case in cases:
        if str(case["case_id"]) in completed:
            continue
        case_path = cases_dir / f"{hashlib.sha256(str(case['case_id']).encode()).hexdigest()}.jsonl"
        _atomic_write_observations(
            case_path,
            run_case(client, case, store_id=store_id, benchmark=args.format, top_k=args.top_k),
        )
    observations = []
    for case_path in sorted(cases_dir.glob("*.jsonl")):
        observations.extend(_load_observation_lines(case_path))
    _atomic_write_observations(output_path, observations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
