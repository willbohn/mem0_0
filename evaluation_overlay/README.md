# Reproducible memory-quality evaluation overlay

This directory pins the repository, benchmark, dataset, and model revisions used to compare three isolated arms:

- `baseline`: the pre-lifecycle Python revision;
- `candidate`: state-aware recall at the candidate revision;
- `candidate_legacy`: the same candidate revision with `recall.mode=legacy`.

The overlay does not modify the `evaluation/` submodule. Runtime state, source clones, datasets, checkpoints, raw outputs, and logs live below `.evaluation-overlay/`, which is ignored. Only manifest pins, adapters, aggregate metric code, and empty credential templates belong in Git.

## Safety and reproducibility contract

- Each arm has a distinct host, store identity, `MEM0_DIR`, and output tree.
- A run fingerprint covers the complete manifest, arm, and benchmark. Resume fails if any pinned revision, budget, command, model, or gate changes.
- Source verification rejects the wrong Git commit or tracked changes. Dataset verification requires a `.source-receipt.json` whose `kind`, `repository`, and `revision` exactly match the manifest.
- Subprocesses use argument arrays, never a shell. Checkpoints are atomically replaced and fsynced.
- Summaries contain aggregate IDs, rates, sizes, latency, and provider-call counts—not message or memory text.

## Prepare

Initialize the already-pinned benchmark submodule and clone the two external benchmark sources at the exact revisions declared in `manifest.json`:

```bash
git submodule update --init evaluation
git clone https://github.com/icedreamc/STALE.git .evaluation-overlay/sources/stale
git -C .evaluation-overlay/sources/stale checkout ea7d391103a151927cd29d2f01d87597a782bdcb
git clone https://github.com/geniesinc/Memora.git .evaluation-overlay/sources/memora
git -C .evaluation-overlay/sources/memora checkout a6493188efc836d6511ed5e4163fe3ba87da30ff
git worktree add --detach .evaluation-overlay/worktrees/baseline 50bdaaea0c02744720ed374d88584fd01494eeb7
git worktree add --detach .evaluation-overlay/worktrees/candidate 0ea19eb2c4f119248a2a47cec2dbf5b065511aa4
```

Populate each `.evaluation-overlay/datasets/<name>/` from its pinned source, then add `.source-receipt.json` with a SHA-256 entry for every consumed file. For example:

```json
{"kind":"huggingface","repository":"STALEproj/STALE","revision":"617c51dc200b5ab09970834144c7e51c77959af0","files":{"T1_T2_400_FULL.json":"<64 lowercase hex characters>"}}
```

Prefer the receipt writer over hand-authored JSON once the pinned files are in place:

```bash
python -m evaluation_overlay.cli prepare-dataset --dataset locomo --file locomo10.json
python -m evaluation_overlay.cli prepare-dataset --dataset longmemeval --file longmemeval_s_cleaned.json
python -m evaluation_overlay.cli prepare-dataset --dataset stale --file T1_T2_400_FULL.json
python -m evaluation_overlay.cli prepare-dataset --dataset beam
```

Non-derived receipt preparation succeeds only when the supplied paths exactly equal the manifest's `required_files`; a subset or extra file fails closed. BEAM is different: its preparation command verifies the exact DuckDB binary and JSON extension, regenerates every declared `beam_<size>.json` cache atomically from the exact declared source Parquet files, and records an executed-derivation proof. It never trusts a caller-asserted derivation or fetches moving dataset branches.

The expected local files are `locomo/locomo10.json`, `longmemeval/longmemeval_s_cleaned.json`, the pinned and deterministically converted Hugging Face BEAM cache under `beam/`, and `stale/T1_T2_400_FULL.json`. Memora data is read directly from its separately verified, pinned Git checkout, so it does not duplicate those files or require a second receipt.

Model artifacts are pinned more tightly than datasets: each manifest entry names the serving ID and source revision plus the exact runtime artifact repository, revision, relative path, byte size, format, and SHA-256. Do not hand-author or relabel model receipts. Verify and hard-link an existing artifact into the private work tree instead:

```bash
python -m evaluation_overlay.cli prepare-model --model embedder \
  --source "$HOME/.lmstudio/models/gpustack/bge-m3-GGUF/bge-m3-Q8_0.gguf" --copy
python -m evaluation_overlay.cli prepare-model --model llm \
  --source /path/to/pinned/Bonsai-27B-Q1_0.gguf
```

The command first attempts a hard link. Cross-filesystem copies require the explicit `--copy` flag and are verified again before atomic placement, so a large copy never happens silently. The checked-in Bonsai pin is the `prism-ml/Bonsai-27B-gguf` Q1 artifact, not LM Studio's similarly named `lmstudio-community/Bonsai-27B-GGUF` file; the latter is a different byte sequence and is rejected. The BGE-M3 serving ID remains `BAAI/bge-m3`, while the manifest explicitly records that the served Q8 GGUF artifact is the pinned `gpustack/bge-m3-GGUF` derivative. Verified model file hashes are incorporated into every executable run fingerprint, so replacing a local artifact invalidates resume.

No secret values are committed. Render the selected `config/*.json.template` outside Git using:

- `MEM0_BENCH_LOCAL_API_KEY` (an arbitrary non-empty value when the local endpoints do not authenticate);
- `MEM0_BENCH_LLM_BASE_URL` for the OpenAI-compatible Bonsai endpoint;
- `MEM0_BENCH_EMBED_BASE_URL` for the OpenAI-compatible BGE-M3 endpoint.

The manifest pins the public model artifact as `prism-ml/Bonsai-27B-gguf`; `prism-ml/bonsai-27b` is not the canonical public repository ID. All arms must use the same model artifacts, prompts, seeds, token budgets, and answer/judge endpoints.

Qdrant is also immutable input. The manifest pins both the multi-architecture image index and the selected `linux/amd64` platform digest. Use the included launcher; it refuses to replace a pre-existing container, verifies the live container image ID, repository digest, platform digest, platform, port binding, and version, then writes an ignored runtime receipt:

```bash
python -m evaluation_overlay.qdrant_runtime start
python -m evaluation_overlay.qdrant_runtime verify
```

Each benchmark server independently re-inspects the running container and refuses to start if the live runtime no longer matches that receipt. Keep baseline, candidate, and candidate-legacy collection names isolated as declared in the manifest.

## Start, verify, plan, and run

Start one Qdrant service, export the three empty template variables listed above, then start each attested arm in a separate terminal. The launcher refuses the wrong Git revision, a Mem0 import outside the selected worktree, the wrong recall mode/model, a non-loopback bind, or a shared collection identity.

```bash
uv run --with-requirements evaluation_overlay/runtime-requirements.txt \
  python -m evaluation_overlay.server --arm baseline --repository-root .evaluation-overlay/worktrees/baseline
uv run --with-requirements evaluation_overlay/runtime-requirements.txt \
  python -m evaluation_overlay.server --arm candidate --repository-root .evaluation-overlay/worktrees/candidate
uv run --with-requirements evaluation_overlay/runtime-requirements.txt \
  python -m evaluation_overlay.server --arm candidate_legacy --repository-root .evaluation-overlay/worktrees/candidate
```

The three processes listen on ports 18888, 28888, and 38888. The complete benchmark-only FastAPI/Uvicorn dependency closure is pinned in `runtime-requirements.txt`; the manifest binds that file's SHA-256 and exact package-version map, and each process attests every installed distribution in that closure. They share only the receipt-verified Qdrant runtime because their collection names are distinct; the server ignores ad hoc Qdrant host/port environment overrides.

Preflight calls `/models` on both configured OpenAI-compatible endpoints and requires the exact pinned model IDs. Each matching model object must also include `metadata.mem0_0_artifact_fingerprint`, equal to the SHA-256 fingerprint of that model's verified `.source-receipt.json`. Stock `llama-server` does not add this metadata; run it behind the included loopback-only attestation proxy:

```bash
export MEM0_BENCH_LOCAL_API_KEY='use-a-local-random-value'
python -m evaluation_overlay.model_proxy --model llm --upstream-port 18100 --port 18000
python -m evaluation_overlay.model_proxy --model embedder --upstream-port 18101 --port 18001
export MEM0_BENCH_LLM_BASE_URL=http://127.0.0.1:18000/v1
export MEM0_BENCH_EMBED_BASE_URL=http://127.0.0.1:18001/v1
```

The proxy itself launches the SHA-pinned `llama-server` binary with the exact verified artifact path, device/offload settings, pinned model alias, and `enable_thinking=false` for every chat-completion request; arbitrary upstream processes are not accepted by the CLI. Both listener and child server use loopback addresses, and every request requires the local API key. Before listening, the proxy parses a fresh child log and requires the requested device plus complete `offloaded/total` layer placement; requested flags alone are not accepted as evidence. It then returns receipt-bound model, launcher, inference-mode, and resolved-placement metadata. Preflight rejects non-loopback endpoints and partial/fallback placement. The attested endpoint URL and receipt fingerprint become part of the executable run fingerprint, so changing endpoints or served weights invalidates resume. Benchmark subprocesses receive `OPENAI_BASE_URL` and `OPENAI_API_KEY` only from the corresponding `MEM0_BENCH_*` variables; the key is redacted from plans and never written into run state.

```bash
python -m evaluation_overlay.cli verify --artifacts-only
# After all three servers are running:
python -m evaluation_overlay.cli verify
python -m evaluation_overlay.cli plan
python -m evaluation_overlay.cli run --arm baseline --benchmark locomo
python -m evaluation_overlay.cli run --arm candidate --benchmark locomo
python -m evaluation_overlay.cli run --arm candidate_legacy --benchmark locomo
```

Use `--resume` only with the exact same manifest. A changed fingerprint requires a new work directory, preserving the old run as immutable evidence.

STALE and Memora use `state_benchmark.py` to measure candidate recall, current-state precision, stale-memory leakage, context size, and latency directly against the local REST service. Their official answer/judge stages remain the source of state-resolution and FAMA answer scores; normalize those scores into the same observation JSONL before applying the answer-quality gates. Missing metrics fail closed.

Normalize the ignored upstream LoCoMo, LongMemEval, or BEAM unified result before summarizing. The normalizer removes questions, memories, answers, and judge text while retaining generic scores, IDs, sizes, latency, and attested provider-call telemetry:

```bash
python -m evaluation_overlay.cli normalize .evaluation-overlay/runs/candidate/locomo/output/locomo_results_<timestamp>.json \
  --run-state .evaluation-overlay/runs/candidate/locomo/run-state.json \
  --output .evaluation-overlay/runs/candidate/locomo/observations.jsonl
```

The same command supports official judged STALE and Memora outputs with `--format stale` or `--format memora`. Pass their retrieval run state too; the adapter adds attested extraction/embedding call counts and write-latency samples. Those adapters provide state-resolution accuracy and FAMA respectively, with no question-ID-specific answer handling.

Normalized observations include ordered SHA-256 fingerprints of normalized memory text and every returned `score`/`rerank_score`, but never the text itself. Enforce the legacy escape-hatch gate for each benchmark after normalizing the baseline and `candidate_legacy` arms:

```bash
python -m evaluation_overlay.cli compare-legacy \
  --baseline .evaluation-overlay/runs/baseline/locomo/observations.jsonl \
  --candidate-legacy .evaluation-overlay/runs/candidate_legacy/locomo/observations.jsonl \
  --output .evaluation-overlay/runs/locomo-legacy-parity.json
```

The command fails on a missing question, changed content order, or any changed dense/rerank score. Random provider memory IDs are deliberately excluded from this comparison.

LoCoMo, LongMemEval, and BEAM do not expose source-memory relevance labels, so the overlay does not mislabel an LLM sufficiency opinion as candidate recall. True candidate recall@K is computed only where the adapter has explicit relevant memory IDs (STALE and Memora). For the other suites, produce a separately reported conservative extractive-evidence-coverage@K diagnostic with the pinned local model, then merge it with the upstream answer observations:

```bash
python -m evaluation_overlay.retrieval_judge \
  .evaluation-overlay/runs/candidate/locomo/output/<unified-result.json> \
  --run-state .evaluation-overlay/runs/candidate/locomo/run-state.json \
  --output retrieval-evidence-coverage.jsonl
python -m evaluation_overlay.cli merge --input observations.jsonl --input retrieval-evidence-coverage.jsonl --output complete.jsonl
```

This diagnostic sees only the question, reference answer, and top-K retrieved candidates; it has no benchmark- or question-ID-specific branches. It decomposes the reference into atomic claims, identifies the earliest supporting candidate for each claim, and must return exact reference and candidate substrings. The adapter requires complete reference-token coverage, requires the full normalized reference excerpt inside the candidate evidence, and validates the earliest index. This intentionally undercounts paraphrases rather than allowing model knowledge or unsupported inference to become a false retrieval hit. Its model receipt, attested endpoint, prompt, source run fingerprint, and content-bearing unified-result hash are bound into every content-free output observation. The diagnostic is reported separately as `candidate_evidence_coverage_at_k` and never populates `candidate_recall_at_k`.

For STALE or Memora, merge the retrieval and official-judge observation files before summarizing:

```bash
python -m evaluation_overlay.cli merge --input retrieval.jsonl --input judged.jsonl --output observations.jsonl
```

Create and compare aggregate summaries:

```bash
python -m evaluation_overlay.cli summarize observations.jsonl --output summary.json
python -m evaluation_overlay.cli compare --baseline baseline-summary.json --candidate candidate-summary.json --output comparison.json
```

The comparison exits nonzero if any declared metric is absent or fails its gate. Run gates separately for LoCoMo, LongMemEval, and BEAM so the one-percentage-point answer-accuracy limit is enforced per benchmark, not hidden by a pooled average.
