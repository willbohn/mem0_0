from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evaluation_overlay.harness import (
    REQUIRED_ARMS,
    REQUIRED_BENCHMARKS,
    build_run_spec,
    load_manifest,
    materialize_model_artifact,
    run_benchmark,
    verify_dataset_receipts,
    verify_model_receipts,
    verify_model_endpoints,
    verify_runtime_attestation,
    verify_sources,
)
from evaluation_overlay.metrics import compare_summaries, load_observations, summarize_observations
from evaluation_overlay.normalize import (
    attach_run_telemetry,
    compare_legacy_observations,
    load_unified_result,
    merge_observations,
    normalize_memora_judge,
    normalize_stale_judge,
    normalize_upstream_result,
)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest_and_paths(args: argparse.Namespace) -> tuple[dict[str, Any], Path, Path]:
    manifest_path = args.manifest.resolve()
    repository_root = manifest_path.parent.parent
    return load_manifest(manifest_path), repository_root, args.work_dir.resolve()


def _plan(args: argparse.Namespace) -> int:
    manifest, repository_root, work_dir = _manifest_and_paths(args)
    datasets = verify_dataset_receipts(manifest, work_dir)
    models = verify_model_receipts(manifest, work_dir)
    endpoints = verify_model_endpoints(manifest, models)
    manifest = {
        **manifest,
        "artifact_fingerprints": {"datasets": datasets, "endpoints": endpoints, "models": models},
    }
    sides = args.arm or sorted(manifest["arms"])
    benchmarks = args.benchmark or sorted(manifest["benchmarks"])
    plans = []
    for side in sides:
        for benchmark in benchmarks:
            spec = build_run_spec(
                manifest,
                work_dir,
                side,
                benchmark,
                repository_root=repository_root,
                require_provider_environment=True,
            )
            plans.append(
                {
                    "arm": side,
                    "benchmark": benchmark,
                    "command": list(spec.command),
                    "environment": {
                        key: "<redacted>" if "KEY" in key else value for key, value in spec.environment.items()
                    },
                    "fingerprint": spec.fingerprint,
                    "state_path": str(spec.state_path),
                }
            )
    print(json.dumps({"schema_version": 1, "runs": plans}, indent=2, sort_keys=True))
    return 0


def _verify(args: argparse.Namespace) -> int:
    manifest, repository_root, work_dir = _manifest_and_paths(args)
    sources = verify_sources(manifest, repository_root, work_dir=work_dir)
    datasets = verify_dataset_receipts(manifest, work_dir)
    models = verify_model_receipts(manifest, work_dir)
    endpoints = verify_model_endpoints(manifest, models)
    runtimes = {}
    if not args.artifacts_only:
        runtimes = {arm: verify_runtime_attestation(manifest, arm) for arm in sorted(manifest["arms"])}
    print(
        json.dumps(
            {
                "datasets": datasets,
                "endpoints": endpoints,
                "models": models,
                "runtimes": runtimes,
                "sources": sources,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _run(args: argparse.Namespace) -> int:
    manifest, repository_root, work_dir = _manifest_and_paths(args)
    verify_sources(manifest, repository_root, work_dir=work_dir)
    datasets = verify_dataset_receipts(manifest, work_dir)
    models = verify_model_receipts(manifest, work_dir)
    endpoints = verify_model_endpoints(manifest, models)
    manifest = {
        **manifest,
        "artifact_fingerprints": {"datasets": datasets, "endpoints": endpoints, "models": models},
    }
    spec = build_run_spec(
        manifest,
        work_dir,
        args.arm,
        args.benchmark,
        repository_root=repository_root,
        resume=args.resume,
        require_provider_environment=True,
    )
    result = run_benchmark(spec, resume=args.resume, dry_run=args.dry_run)
    print(json.dumps({"status": result.status, "state_path": str(result.state_path)}, sort_keys=True))
    return 0


def _prepare_model(args: argparse.Namespace) -> int:
    manifest, _, work_dir = _manifest_and_paths(args)
    receipt = materialize_model_artifact(
        manifest,
        work_dir,
        args.model,
        args.source,
        allow_copy=args.copy,
    )
    print(
        json.dumps(
            {
                "artifact": receipt["artifact"]["path"],
                "model": args.model,
                "receipt": str(work_dir / "models" / args.model / ".source-receipt.json"),
                "sha256": receipt["artifact"]["sha256"],
                "status": "verified",
            },
            sort_keys=True,
        )
    )
    return 0


def _summarize(args: argparse.Namespace) -> int:
    summary = summarize_observations(load_observations(args.observations), cutoffs=args.cutoff)
    _write_json(args.output, summary)
    print(json.dumps({"sample_count": summary["sample_count"], "summary": str(args.output)}, sort_keys=True))
    return 0


def _compare(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    comparison = compare_summaries(baseline, candidate, manifest["gates"])
    _write_json(args.output, comparison)
    print(json.dumps({"comparison": str(args.output), "passed": comparison["passed"]}, sort_keys=True))
    return 0 if comparison["passed"] else 1


def _normalize(args: argparse.Namespace) -> int:
    run_state = json.loads(args.run_state.read_text(encoding="utf-8")) if args.run_state else None
    if args.format == "upstream":
        observations = normalize_upstream_result(
            load_unified_result(args.input), cutoff=args.cutoff, run_state=run_state
        )
    elif args.format == "stale":
        observations = normalize_stale_judge(json.loads(args.input.read_text(encoding="utf-8")))
    else:
        observations = normalize_memora_judge(json.loads(args.input.read_text(encoding="utf-8")))
    if args.format in {"stale", "memora"}:
        observations = attach_run_telemetry(observations, run_state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n" for value in observations),
        encoding="utf-8",
    )
    print(json.dumps({"observations": len(observations), "output": str(args.output)}, sort_keys=True))
    return 0


def _merge(args: argparse.Namespace) -> int:
    observations = merge_observations([load_observations(path) for path in args.input])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n" for value in observations),
        encoding="utf-8",
    )
    print(json.dumps({"observations": len(observations), "output": str(args.output)}, sort_keys=True))
    return 0


def _compare_legacy(args: argparse.Namespace) -> int:
    comparison = compare_legacy_observations(load_observations(args.baseline), load_observations(args.candidate_legacy))
    _write_json(args.output, comparison)
    print(json.dumps({"comparison": str(args.output), "passed": comparison["passed"]}, sort_keys=True))
    return 0 if comparison["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    default_manifest = Path(__file__).with_name("manifest.json")
    parser = argparse.ArgumentParser(description="Pinned, isolated Mem0 benchmark overlay")
    parser.set_defaults(func=None)
    subparsers = parser.add_subparsers(dest="command")

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--manifest", type=Path, default=default_manifest)
        subparser.add_argument("--work-dir", type=Path, default=Path(".evaluation-overlay"))

    plan = subparsers.add_parser("plan", help="Print commands and isolated runtime identities without executing")
    add_common(plan)
    plan.add_argument("--arm", action="append")
    plan.add_argument("--benchmark", action="append")
    plan.set_defaults(func=_plan)

    verify = subparsers.add_parser("verify", help="Verify pinned source revisions and dataset receipts")
    add_common(verify)
    verify.add_argument("--artifacts-only", action="store_true")
    verify.set_defaults(func=_verify)

    run = subparsers.add_parser("run", help="Run one benchmark arm")
    add_common(run)
    run.add_argument("--arm", required=True, choices=sorted(REQUIRED_ARMS))
    run.add_argument("--benchmark", required=True, choices=sorted(REQUIRED_BENCHMARKS))
    run.add_argument("--resume", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=_run)

    prepare_model = subparsers.add_parser(
        "prepare-model", help="Verify and hard-link an exact pinned model artifact into the private work tree"
    )
    add_common(prepare_model)
    prepare_model.add_argument("--model", required=True, choices=("llm", "embedder"))
    prepare_model.add_argument("--source", required=True, type=Path)
    prepare_model.add_argument(
        "--copy",
        action="store_true",
        help="Allow a verified byte-for-byte copy when source and work tree are on different filesystems",
    )
    prepare_model.set_defaults(func=_prepare_model)

    summarize = subparsers.add_parser("summarize", help="Create a content-free summary from normalized JSONL")
    summarize.add_argument("observations", type=Path)
    summarize.add_argument("--output", type=Path, required=True)
    summarize.add_argument("--cutoff", type=int, action="append", default=[])
    summarize.set_defaults(func=_summarize)

    normalize = subparsers.add_parser("normalize", help="Remove raw content from an upstream unified result")
    normalize.add_argument("input", type=Path)
    normalize.add_argument("--output", type=Path, required=True)
    normalize.add_argument("--run-state", type=Path)
    normalize.add_argument("--cutoff")
    normalize.add_argument("--format", choices=("upstream", "stale", "memora"), default="upstream")
    normalize.set_defaults(func=_normalize)

    merge = subparsers.add_parser("merge", help="Merge retrieval and official judge observations by question ID")
    merge.add_argument("--input", type=Path, action="append", required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.set_defaults(func=_merge)

    compare_legacy = subparsers.add_parser(
        "compare-legacy", help="Require candidate legacy retrieval ordering and scores to match baseline"
    )
    compare_legacy.add_argument("--baseline", type=Path, required=True)
    compare_legacy.add_argument("--candidate-legacy", type=Path, required=True)
    compare_legacy.add_argument("--output", type=Path, required=True)
    compare_legacy.set_defaults(func=_compare_legacy)

    compare = subparsers.add_parser("compare", help="Apply the manifest acceptance gates")
    compare.add_argument("--manifest", type=Path, default=default_manifest)
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.set_defaults(func=_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        parser.print_help()
        return 2
    if getattr(args, "cutoff", None) == []:
        args.cutoff = [10, 20, 50, 200]
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
