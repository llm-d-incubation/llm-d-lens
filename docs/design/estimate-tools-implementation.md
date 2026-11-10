# Estimate Tools: AISimulate and BLIS Implementation Plan

Status: Step 1 verified as an experimental CPU-only integration baseline;
Steps 2-3 implemented and locally verified (2026-11-10).
Steps 4-14 are not implemented. Prediction accuracy is not validated.
Date: 2026-11-06.
Target branch: `enable_blis`.

## 1. Purpose and scope

Introduce a shared estimate-tool boundary for AISimulate and BLIS. Keep
AISimulate as the default, allow BLIS-only execution, and allow both tools to
run independently with separately attributable results.

This is the implementation and acceptance contract for the estimate-tools
domain. It does not replace:

- [Simulation design](simulation-design.md), which currently owns trace-driven
  benchmarking against real inference endpoints;
- [Capacity planning](capacity-planner-integration-design.md), which owns
  memory and deployment feasibility guardrails;
- [Agentic deployment](agentic-deployment-design.md), which owns proposal
  generation, evidence use, selection, and approval;
- [Storage layout](storage-layout.md), which owns task and artifact locations;
- [Hardware architecture](hardware-plugin-architecture.md), which owns
  hardware profiles and provider-driven mappings.

BLIS is a CPU-only simulator, not a benchmark client against a live server.
The new estimate layer must not change the existing Simulation endpoint
semantics or make prediction support a prerequisite for manual deployment.

### Confirmed behavior

1. An omitted tool selection means AISimulate only.
2. BLIS-only execution must not invoke AISimulate.
3. Selecting both tools runs them independently and retains each result,
   limitation, warning, and error.
4. Predictions are not averaged, silently substituted, or merged into a
   synthetic metric.
5. Tool APIs are not assumed to be equivalent. AISimulate retains its native
   recommendation path; Lens implements BLIS optimization through bounded
   sweeps of exact simulation evaluations.
6. Existing AIC endpoints and their response contracts remain compatible.
7. BLIS integration does not authorize or implement B60 calibration.

### Explicit non-goals

- Implementing `recommend()` inside BLIS or changing BLIS engine internals.
- Reintroducing removed BLIS latency backends or coefficient formats.
- Downloading models, compiling BLIS, or fitting coefficients during a request.
- Claiming global optimality outside a fully evaluated declared finite space.
- Automatically deploying an estimated configuration.
- Porting the complete backend into the new `lens` engine as part of this work.

## 2. Evidence baseline and version discipline

The documentation review used these local source snapshots:

| Checkout | Observed revision | Purpose |
| --- | --- | --- |
| Ordinary `inference-sim` checkout | `762f1605` | Current usage docs and CLI source inspection |
| B60 workspace `inference-sim` checkout | `df31ab9` | B60 integration baseline, not a validated runtime |

These are inspection references, not an approved deployment lock. Do not mix
their binaries, templates, dependency trees, or working-tree modifications.
Step 1 records the full commits and content digests selected for implementation.

Primary upstream references:

- [Installation and catalog compatibility](https://github.com/inference-sim/inference-sim/blob/762f1605/docs/getting-started/installation.md)
- [Configuration](https://github.com/inference-sim/inference-sim/blob/762f1605/docs/reference/configuration.md)
- [Latency models and scenario structure](https://github.com/inference-sim/inference-sim/blob/762f1605/docs/guide/latency-models.md)
- [Cluster and P/D topology](https://github.com/inference-sim/inference-sim/blob/762f1605/docs/guide/cluster.md)
- [Workloads](https://github.com/inference-sim/inference-sim/blob/762f1605/docs/guide/workloads.md)
- [Routing](https://github.com/inference-sim/inference-sim/blob/762f1605/docs/guide/routing.md)
- [Results and measurement definitions](https://github.com/inference-sim/inference-sim/blob/762f1605/docs/guide/results.md)
- [Replay](https://github.com/inference-sim/inference-sim/blob/762f1605/docs/guide/observe-replay-calibrate.md)
- [Capacity tutorial](https://github.com/inference-sim/inference-sim/blob/762f1605/docs/getting-started/tutorial.md)
- [Run CLI implementation](https://github.com/inference-sim/inference-sim/blob/762f1605/cmd/root.go)
- [Workload implementation](https://github.com/inference-sim/inference-sim/blob/762f1605/sim/workload/spec.go)

The inspected module declares Go `1.27.2`, blis-schemas `v0.10.0`,
blis-vllm `v0.32.0-blis.1`, and blis-latency-kernel `v0.5.0`.
Installation docs name catalog `0.3.0` and registry `v0.2.0`.
These declarations are a starting point for compatibility testing, not proof.

Some checked-in scenarios still name engine `0.29.0`, while the current
latency-model guide requires `0.32.0`. Some engine limitations in prose also
lag newer fixtures. Use a fixed build and successful compatibility tests as
the release gate; do not copy an example and assume it works.

### Step 1 verified runtime lock (2026-11-10)

Profile: `blis-762f1605-muse-h100-roce-armB-experimental`. This is a local
integration baseline for the exact fixture below, not a production installation
default or certification of prediction accuracy. The ordinary source checkout
was clean before and after verification; no upstream code was changed.

| Component | Fixed version | Full source commit |
| --- | --- | --- |
| inference-sim | `762f1605` | `762f1605ff27cd825d6b2cb2d9e991cd4b864c91` |
| blis-schemas | `v0.10.0` | `a649e0fa5a41611cac34ef02fbd68cafa342829f` |
| blis-vllm | `v0.32.0-blis.1` | `6f0dcaca760dcf572cb692ffc3af40636001e773` |
| blis-latency-kernel | `v0.5.0` | `560bda63998700b1745c84387008ce3c1ded1b2a` |
| llm-d-router fork | `v1.0.0-blis.2` | `6e4bd8d21c46aabf4bd92baa012bbe2520b9d995` |
| blis-catalog subset | `0.5.0` | `b5550f750cb51571498577fbdc1c68f78cda67db` |
| blis-registry | `v0.3.0` | `96bead697172ab7f2cf8346e04e65b837014d867` |

The installation guide's catalog `0.3.0` / registry `v0.2.0` declarations are
stale for this baseline. The selected data are the source commit's checked-in
`testdata/catalog` and `testdata/registry`; their `.upstream-commit` markers agree
with kernel `v0.5.0`'s `testdata/upstream.lock`. This verifies the actual vendored
contents, not an arbitrary full checkout or another catalog release.

Build toolchain: Go `1.27.2 linux/amd64`. Only the installed compiler and
checksum-verified module/build caches were reused from the B60 toolchain
directory. `GOWORK=off`, `GOTOOLCHAIN=local`, and `-mod=readonly` prevent workspace
or version substitution. The sole module replacement is the pinned router fork
already declared by the ordinary checkout. `go mod verify` returned
`all modules verified`. No B60 source replacement, binary, scenario, or
coefficient belongs to this profile.

#### Content identity and local evidence

Local evidence root:
`/home/wenxin/.local/share/lens/artifacts/estimate-tools/baseline-20261110-762f1605`.
The binary is at
`/home/wenxin/.cache/lens/estimate-tools/baseline-762f1605/blis`.

| Content | SHA-256 |
| --- | --- |
| Binary | `c701b5577e06009157c268e19532c0d598576288c40d9f4a95ce515edc529d16` |
| Catalog, 93 files | `cc2da5d3f131d125fe4100e9e7f671d093ead9791256239b66d05fbc3199f3e7` |
| Registry, 9 files | `4f492df94b6f6ac4bad2759d3d916da4ec0c3aa66ff06c77a4f9981936f64462` |
| Scenario YAML | `358512eb20bcf670678b5b67bc468a0f66f9ce1b65fae11ddbc36fc810a1a70a` |
| Complete input tree | `52d766adb062d42cde4ec376df9a1a709d0675db336b1e8ccfcca6d9a9f7bee6` |
| First raw Result | `4acea46d9d443059dcecb00602f978e9a3e2cd53db68a49e27d5c4770f732b62` |
| Deterministic Result, without `runtime` | `f3d557b2488161073b7d45d6111cc9fa80248bbdff13118701952f20f8b4b969` |

Tree digests hash the concatenation of manifest entries, each encoded as UTF-8
`relative-path + NUL + lowercase-file-sha256 + LF`. Catalog/registry paths are
relative to their individual roots; the complete input tree is relative to
`inputs/`. Symlinks are refused. Per-file digests, source file digests, toolchain
identity, module origins, exact argv/environment, exit codes, support scope, and
assumptions are in `runtime-lock.json` at the evidence root.

Step 3 found that the original manifest uses traversal order rather than global
relative-path sorting. The original hashes above remain unchanged as historical
evidence. The consumer verifies the same files using global lexicographic
relative-path order and the same entry encoding: catalog
`ba72abe7791388114f1b77cfa094c88e5ca6658aed0178bf86bdd87341b9b69b`,
registry unchanged, and complete input tree
`28cc378251abb2bd6be3035bee5ac2c29c1cf42ee2e45afd57a297cfee88ee3a`.
These are ordering changes, not new runtime content or a wider support envelope.

Raw stdout/stderr are retained as `smoke.stdout.json`, `smoke.stderr.log`,
`repeat.stdout.log`, and `repeat.stderr.log`. `deterministic-result.json` stores
the first parsed Result with only its top-level `runtime` removed. Dependency
evidence is in `binary-build-info.stdout.log`, `module-lock.stdout.log`, and
`module-integrity.stdout.log`; negative runs retain separate output and execution
records. These local artifacts are not committed or registered with an API that
does not exist yet. Preserve them with this document when transferring evidence.

Raw Result revisions for BLIS/catalog/registry are `unknown`: the documented
file-list build does not embed a main-module version, and staged data are not
Git checkouts. Do not rewrite or invent those fields. This external lock supplies
the full source commits and actual content hashes; consumer-side provenance
handling remains work for later steps.

#### Reproduction and checked scope

Provision outside any request path:

```bash
cd /home/wenxin/inference-sim
GOWORK=off GOTOOLCHAIN=local \
  GOMODCACHE=/home/wenxin/blis-b60/toolchains/modcache \
  GOCACHE=/home/wenxin/blis-b60/toolchains/buildcache \
  GOPROXY=https://proxy.golang.org,direct \
  /home/wenxin/blis-b60/toolchains/go1.27.2/bin/go build \
  -mod=readonly -trimpath \
  -o /home/wenxin/.cache/lens/estimate-tools/baseline-762f1605/blis main.go
```

An initial `GOPROXY=off` build refused missing exact modules. The provisioning
build above downloaded those declared releases and exited `0`; no model weights
were downloaded. Runtime uses only the staged local inputs and the prebuilt
binary. No request/API code, calibration, or live cluster operation was added.

From the evidence root, rerun with the controlled environment and explicit inputs:

```bash
env -i HOME=/home/wenxin PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC GOMAXPROCS=1 \
  /usr/bin/timeout 120s \
  /home/wenxin/.cache/lens/estimate-tools/baseline-762f1605/blis run \
  --scenarios inputs/scenarios --scenario muse-h100-roce-armB.yaml \
  --catalog inputs/catalog --registry inputs/registry \
  --num-instances 1 --num-requests 8 --seed 42 --horizon 60000000 --rate 2 \
  --prompt-tokens 128 --prompt-tokens-stdev 0 \
  --prompt-tokens-min 128 --prompt-tokens-max 128 \
  --output-tokens 32 --output-tokens-stdev 0 \
  --output-tokens-min 32 --output-tokens-max 32 \
  --timeout 30 --format json --log warn
```

The recorded runs used an equivalent 120-second subprocess wall timeout and
8 MiB stdout/stderr capture bound, not a product execution service. Both exited
`0` and emitted schema-version `1`, `kind: Result`, validated by BLIS before
printing. Full parsed Results were deeply equal after removing **only**
`runtime`; the normalized JSON digest above uses two-space indentation and a
trailing LF. This is reproducibility evidence, not independent sampling.

| Capability / setting | Verified scope |
| --- | --- |
| Operation / model | `run`; `muse-spark-open-816b-a42b-nvfp4`, text decoder only |
| Hardware / placement | Simulated H100; two nodes, eight devices/node; `roce-200g`; one colocated instance |
| Parallelism / engine | TP8, PP2, DP1, EP enabled; scenario engine `0.32.0`; exact checked-in deployment settings |
| Engine settings | Block size 16; max sequences 64; max batched tokens 8192; max model length 262144; prefix caching and async scheduling enabled; GPU memory utilization 0.9; MoE backend `marlin` |
| Workload | CLI distribution mode; rate 2 requests/s; constant 128 input and 32 output tokens; no shared prefix |
| Controls | Seed 42; eight requests; 60,000,000 ticks = 60 simulated seconds; 30-second request deadline |
| Result checks | Exact model/cluster/parallelism/run controls; eight generated/injected/completed; zero failure/in-flight buckets; 1024 input and 256 output tokens; finite positive core metrics |
| Not enabled by this gate | B60, P/D, replay, native concurrency, search, capacity certification, multimodal encoder pricing, or prediction accuracy claims |

Kernel assumptions remain in raw Result and stderr, including unpriced vision/
audio encoding, even expert-routing load, pipeline memory binding, graph defaults,
and the warning that some kernel behavior still models vLLM `0.31.0` despite the
scenario's `0.32.0` rules pack. The exact smoke fixture is the only verified
envelope; changes require another runtime check.

| Negative check | Actual result |
| --- | --- |
| Absent binary | Spawn error `ENOENT`, no process exit code or fallback |
| Selected model removed from an isolated catalog copy | Exit `1`, missing model `graph.yaml` path |
| Isolated registry directory with mode `000` | Exit `1`, coefficient read reports `permission denied`; permissions restored afterwards |
| Isolated scenario with engine `99.0.0` | Exit `1`, explicit unsupported-engine diagnostic |

The initial old-version probe unexpectedly succeeded with `0.29.0`; its raw
outputs remain under `legacy-engine-probe.*`. The runtime explicitly supports a
compatibility engine for `0.29.0`/`0.31.0`, so an old version is not a valid
negative fixture. This observation does not add another supported profile.
The final incompatible fixture changes only the engine version in the isolated
copy of `examplescenarios/gpt-oss-120b-h100-fp4-vllm-tp2.yaml`, not upstream files.

### BLIS facts that constrain implementation

| Fact | Required implementation behavior |
| --- | --- |
| `run` and `replay` need a scenario file, scenario directory, catalog, and registry | Stage a complete, attributable input bundle |
| Model, hardware, parallelism, batch limits, and engine settings belong in Scenario/Deployment | Generate YAML instead of removed flags such as `--tp` |
| Instance counts are run controls, bounded by pool placement capacity | Separate instance shape from replica count |
| `--workload-spec` ignores CLI distribution parameters, including `--rate` | Change `aggregate_rate` in YAML for a YAML rate sweep |
| Closed-loop concurrency is native | Never approximate concurrency by converting it to QPS |
| P/D requires pools, a supported connector, and disaggregating router configuration | Do not implement P/D by changing counts alone |
| Stdout contains a standard `Result`; diagnostics go to stderr | Parse structured output, not human-readable summaries |
| `--metrics-path` is deprecated | Use the standard Result contract |
| `observe` and `calibrate` are deprecated; `replay` is not | Keep measurement collection and calibration out of estimation |
| Replay and run have different default horizons | Set and record horizons explicitly |

## 3. Architecture and ownership

```text
Configuration UI / Agentic / MCP
               |
        Estimate Service
        selection, tasks, persistence, attribution
               |
       +-------+----------------------+
       |                              |
 AISimulate Strategy          BLIS Optimization Strategy
 native search/recommend      enumerate, prune, sweep, assess, rank
       |                              |
 AISimulate Adapter              BLIS Executor
 existing Python API            compile bundle, run CLI, parse Result
```

Public product operations:

- `estimate`: evaluate one explicit configuration under one workload.
- `optimize`: search a bounded space under explicit constraints and objective.
- `support`: report availability and request-specific support, with reasons.

Adapters need not implement identical native methods. Capability discovery
distinguishes native recommendation, native exact estimation, replay, and
Lens-managed sweep support. Model, hardware, engine, topology, and workload
restrictions are request-specific.

### Implementation ownership and target layout

The confirmed target is `apps/backend/src/llm_d_lens/estimate_tools/` with
separate `aic/` and `blis/` packages. AISimulate retains the established `aic`
identifier in code; its product name remains AISimulate. Shared contracts and
orchestration belong to the parent package, not to either tool.

| Proposed file | Responsibility |
| --- | --- |
| `__init__.py` | Deliberate public exports |
| `models.py` | Requests, results, metrics, capabilities, and task contracts |
| `service.py` | Tool dispatch, task lifecycle, and multi-tool aggregation |
| `router.py` | HTTP validation, permissions, status mapping, and MCP metadata |
| `aic/__init__.py` | Deliberate AISimulate exports |
| `aic/models.py` | Existing AIC request/response compatibility contracts |
| `aic/service.py` | Existing AISimulate calls and native default/recommend dispatch |
| `aic/router.py` | Existing `/api/v1/aic/*` routes, preserving their URLs and contracts |
| `aic/adapter.py` | Adapt shared estimate contracts to the existing AIC service |
| `blis/__init__.py` | Deliberate BLIS exports |
| `blis/service.py` | Managed runtime resolution and support discovery; later bundle compilation, CLI execution, and Result parsing |
| `blis/optimization.py` | Later BLIS candidate enumeration, feasibility, and ranking |

Migration moves the existing `llm_d_lens/aic/` implementation into
`estimate_tools/aic/`, updates application registration and internal imports,
and keeps its existing API regression tests. Physical location does not change
the `/api/v1/aic/*` API. A temporary old-import compatibility shim is permitted
only where caller compatibility requires it; the final layout has no parallel
top-level AIC implementation. Shared estimate code dispatches to tool adapters;
the AIC service does not call back into shared orchestration. BLIS-only dispatch
and support discovery must not invoke AIC operations.

The previous flat `aisimulate.py`, `blis.py`, and parent-level
`optimization.py` proposal is superseded. Steps 2-3 introduce only the files and
behavior needed for contracts, AIC compatibility, profiles, and support discovery;
future execution, optimization, and shared HTTP routes are not created as empty
placeholders. Specific file additions and removals still require approval before
the migration starts.

Keep related private helpers together initially. Split compilation and result
parsing only if their implementations become independently substantial.
Do not introduce a plugin framework merely for two adapters.

Inspect existing process, task, storage, and artifact helpers before adding
infrastructure. The existing Simulation process wrapper has useful cancellation
patterns, but its availability probe and unbounded stream-copy behavior are not
sufficient evidence of BLIS availability or output safety. Reuse suitable
lower-level infrastructure without coupling estimate-domain behavior to benchmark
assumptions or weakening existing benchmark behavior.

New source, tests, scripts, migrations, and executable configuration paths require
explicit approval before creation under the repository rules. This plan records
proposed paths; writing this document is not approval to create those code files.

## 4. Contracts to freeze before coding

### 4.1 Requests

Separate these inputs:

| Input | Required information |
| --- | --- |
| Tool selection | Nonempty unique list of known tool IDs; omission defaults to AISimulate |
| Model | Repository identity, catalog mapping, dtype/quantization where applicable |
| Hardware | Profile/model, node layout, device counts, and fabric where required |
| Configuration | Serving mode, per-role parallelism and replicas, effective engine/router settings |
| Workload | Exactly one supported load mode, token shape/distribution or trace reference, prefix semantics |
| Constraints | Resource limit, requested metric/statistic thresholds, completeness/error policy |
| Objective | Explicit optimization goal and deterministic tie-break rules |
| Run controls | Seed, request count, simulated horizon, request deadline, wall timeout |
| Search budget | Evaluation count, parallelism, total wall duration, and optional cross-evaluation allowance |

Empty tool lists, duplicates, unknown IDs, invalid numbers, unsupported fields,
and contradictory load definitions fail validation. Do not silently drop fields.
Concurrency is a positive integer; a rate is a positive finite number.
Unknown hardware remains neutral and must not inherit another vendor's defaults.

A mean token length alone does not determine a distribution. A constant-length
approximation is permitted only as an explicit, recorded workload choice.
Do not silently substitute CLI default token distributions.

### 4.2 Results and identity

Each candidate has a tool-neutral effective-configuration fingerprint,
`generated_by`, and separate per-tool evaluations. A fingerprint includes
model precision, node layout, parallelism, engine settings, and router semantics,
not just TP and replicas. Materially different effective configurations remain
separate even when the user-facing topology looks identical.

Each evaluation records:

- Tool and strategy, runtime profile, and resolved input digest.
- Execution status: `succeeded`, `unsupported`, `unavailable`, `failed`,
  `timed_out`, or `cancelled`.
- Metrics with units, statistic, formula, population, and window.
- Constraint assessment: `satisfied`, `not_satisfied`, or `unknown`.
- Request accounting, warnings, and structured error information.
- Component revisions, coefficient provenance, and artifact IDs.
- Evidence maturity: experimental/calibrated/validated only when supported by
  explicit evidence; absence of evidence must not imply validation.

Execution success and constraint satisfaction are separate. A completed
simulation that misses an SLO is not a process failure.

Batch outcome is `succeeded`, `partial`, or `failed`, while task lifecycle is
`queued`, `running`, `cancelling`, `completed`, `failed`, or `cancelled`.
Unsupported tools do not disappear from the batch. All evaluations failing
must produce an explicit failed outcome, not a successful empty prediction.

### 4.3 Task and API shape

Proposed new API, separate from the unchanged `/api/v1/aic/*` contract:

| Method and path | Purpose |
| --- | --- |
| `GET /api/v1/estimate-tools` | Discover configured tools, runtime profiles, and operations |
| `POST /api/v1/estimate-tools/support` | Check request-specific support |
| `POST /api/v1/estimate-tools/estimate` | Persist and submit an exact-estimate task; return `202` and task ID |
| `POST /api/v1/estimate-tools/optimize` | Persist and submit an optimization task; return `202` and task ID |
| `GET /api/v1/estimate-tools/tasks/{id}` | Return task progress, per-tool results, and termination reason |
| `POST /api/v1/estimate-tools/tasks/{id}/cancel` | Request idempotent cancellation |

Use the existing permission model and owner-scoped artifact access. Cross-user
task IDs do not grant access. API clients and MCP must observe the same
server-authoritative support and selection rules.

## 5. Ordered implementation steps and acceptance gates

Steps 1-3 are locally verified; Steps 4-14 remain **not started**.
Step 1 evidence is recorded in section 2; Steps 2-3 evidence follows below.
A gate passes only with recorded
evidence from the actual inputs under test. Documentation inspection, mocked
execution, real BLIS smoke tests, and hardware calibration are different evidence
levels and must not be reported interchangeably.

### Step 1: Lock a reproducible BLIS runtime

Dependencies: none.

Work:

1. Choose one BLIS baseline and record full component commits/releases.
2. Record binary and catalog/registry content digests, including dirty content.
3. Select a known-supported non-B60 aggregated scenario and workload.
4. Build/provision outside the request path and run a real smoke evaluation.
5. Record an explicit approved engine-setting and feature support matrix.

Acceptance:

- A real CPU-only run exits successfully and emits a valid `kind: Result`.
- The Result resolves the intended model, hardware, engine, and topology.
- Missing binary, missing catalog entry, unreadable registry, and incompatible
  inputs produce distinguishable failures, not automatic downloads or fallback.
- Rerunning the same pinned bundle produces equal deterministic Result content,
  excluding only documented host/time-dependent fields.
- No request triggers compilation, dependency installation, model download,
  coefficient fitting, or live cluster mutation.

Evidence: full version lock, smoke input/output digests, commands, exit codes,
stderr, and determinism comparison.

### Step 2: Freeze public models and AISimulate compatibility behavior

Dependencies: Step 1 for the BLIS compatibility envelope.

Work:

1. Define the contracts in section 4 with explicit validation.
2. Wrap existing AIC operations rather than duplicating their implementation.
3. Preserve native `cli_default` versus `cli_recommend` dispatch behavior.
4. Map errors and metrics without claiming unsupported statistical equivalence.

Acceptance:

- Omitted tools invoke AISimulate exactly once and never invoke BLIS.
- BLIS-only selection invokes AISimulate zero times.
- Invalid tool lists and joint/invalid load targets fail before execution.
- Existing AIC support/search/experiments/estimate contract tests pass unchanged
  unless a separately approved bug fix requires a deliberate update.
- Existing default token lengths, replica scaling, GPU budget enforcement, and
  exact-topology checks remain intact.
- AIC legacy experiment YAML is not accepted as BLIS Scenario YAML.

Evidence: focused existing AIC regression tests and new public-contract tests.

### Step 3: Implement runtime profiles and support discovery

Dependencies: Steps 1-2.

Work:

1. Resolve managed binary, catalog, registry, mappings, and approved settings.
2. Distinguish environment availability, static matching, and execution-confirmed
   support. Unknown support must remain explicit.
3. Use profile/provider-driven hardware mappings, not vendor branches in clients.
4. Expose configured profiles and limitations through capability responses.

Acceptance:

- Requests cannot select arbitrary executable paths, server files, or shell flags.
- Missing AISimulate mapping does not block a BLIS-only request.
- Unknown model/hardware/precision combinations return a specific reason.
- Catalog/registry presence alone never yields a validated capability claim.
- Unsupported optional features remain disabled until their runtime tests pass.
- B60 remains unavailable where kernel construction is refused; changing a
  hardware name or finding a coefficient file cannot bypass that refusal.

Evidence: profile resolution, negative capability, and mapping-isolation tests.

### Steps 2-3 implementation evidence (2026-11-10)

Source base: Lens `2a41de0`; implementation changes are recorded by the commit
containing this section. The four original AIC implementation files were moved
unchanged into `estimate_tools/aic/`; existing callers and test imports now use
that package. The `/api/v1/aic/*` routes, native default/recommend dispatch,
default token lengths, replica scaling, GPU budgets and exact-topology checks
retain their original behavior. The English and Chinese Python API examples
use the new package path.

Shared models reject unknown fields, invalid tool selections, nonfinite values,
contradictory load targets and invalid resource shapes before dispatch. Shared
support and exact-estimate dispatch default to AISimulate and never consult AIC
operations or hardware mappings for BLIS-only requests. Metrics retain native
definitions with unknown constraint assessment where evidence is insufficient.

BLIS provisioning is an explicit server-side Python configuration, not a request
path, environment auto-install, or new HTTP endpoint. `baseline_profile(binary=...,
inputs=...)` selects only the Step 1 experimental lock. Discovery exposes approved
effective inputs, revisions and limitations, never host paths. Profile matching,
environment availability and execution-confirmed evidence are distinct. A matching
verified runtime can report support while every Lens BLIS operation remains
disabled. Missing binaries, changed/unsafe/unreadable inputs, unknown mappings,
unverified settings and kernel construction refusal have specific reasons.
No B60 profile is enabled.

Verification on the implementation inputs:

```bash
.venv/bin/python -m pytest apps/backend/tests/unit/estimate_tools \
  apps/backend/tests/unit/aic -q --disable-warnings
# 88 passed, 2 warnings (3.77 seconds)
.venv/bin/python -m pytest apps/backend/tests/unit/agentic/test_facts.py \
  apps/backend/tests/unit/agentic/test_service.py \
  apps/backend/tests/unit/simulation/test_benchmark_evidence.py \
  apps/backend/tests/contract/test_candidate_search.py -q --disable-warnings
# 238 passed, 10 warnings (1177.77 seconds)
.venv/bin/ruff check apps/backend/src/llm_d_lens/estimate_tools \
  apps/backend/tests/unit/estimate_tools
.venv/bin/ruff format --check apps/backend/src/llm_d_lens/estimate_tools \
  apps/backend/tests/unit/estimate_tools
node hack/generate-mcp-tools.mjs --check
# 201 Python tools; zero unexpected skips
```

Lint also passed for every modified Python caller and existing test. The broad
caller regressions preceded mechanical import sorting; no caller behavior was
changed afterwards. `uv build --wheel --no-build-isolation --python
.venv/bin/python --out-dir <temporary-directory> apps/backend` succeeded; wheel
inspection found all new estimate-tool packages and no old top-level AIC package.
File-scoped `npm run docs:check` passed for the changed design, module README and
both localized API pages; only Python import paths changed in the published MDX.
The real local Step 1 binary/input paths from section 2 were
passed to `baseline_profile` and `support`: available, matched, supported,
execution-confirmed and experimental, with all four operations disabled and the
canonical input digest above. This check hashes existing files; it does not rerun
the simulator. Unit profile fixtures and mapping-isolation tests are mocked/local
filesystem evidence, not new BLIS execution or calibration evidence.

No BLIS bundle compiler, workload compiler, executor, task persistence, shared
HTTP routes, optimization engine or UI is implemented by these steps. Native AIC
search/experiments remain available through their unchanged legacy endpoints and
the compatibility adapter. The shared optimizer remains future work. Local
runtime evidence stays at the section 2 artifact root and is not committed.

### Step 4: Compile aggregated deployment bundles

Dependencies: Steps 2-3.

Work:

1. Generate a two-document Scenario/Deployment bundle.
2. Put model, hardware, fabric, coefficients, parallelism, and engine settings in
   their documented fields.
3. Keep instance count in run controls and validate placement against pool capacity.
4. Copy referenced router files into the bundle with contained relative paths.
5. Preserve effective inputs and digests before execution.

Acceptance:

- TP and batch changes modify YAML; no removed `--tp`, `--model`,
  `--hardware`, or batch-setting flags are generated.
- One node with eight devices and two nodes with four devices produce distinct
  inputs and fingerprints.
- Over-budget and impossible placement candidates are rejected with reasons.
- Uncertain model-memory estimates do not silently prune valid candidates.
- A bundle can be relocated without depending on source-checkout relative paths.
- Fixed-version BLIS successfully executes at least two supported distinct
  aggregated shapes; Result input matches each requested shape.

Evidence: compiler fixture tests, path containment tests, and real shape runs.

### Step 5: Compile QPS and closed-loop workloads

Dependencies: Step 4.

Work:

1. Generate WorkloadSpec v2 for QPS and concurrency requests.
2. Encode rate through `aggregate_rate`, not an ignored `--rate` override.
3. Encode native concurrency through `clients[].concurrency` and
   `think_time_us`, without `rate_fraction` on that client.
4. Define whether input length includes shared prefix and prevent double counting.
5. Distinguish request deadlines, simulated horizons, and wall timeouts.
6. Bound generation explicitly with request count and/or horizon.

Acceptance:

- A YAML QPS sweep changes the effective Result load at each evaluated point.
- A concurrency request preserves the exact integer concurrency, with no QPS
  conversion and no simultaneous rate configuration.
- Real runs cover both load modes, fixed token lengths, and an explicitly chosen
  nonconstant workload where supported by the locked runtime.
- Prefix-inclusive and prefix-exclusive input fixtures produce the intended
  total prompt length exactly once.
- Unit conversions are tested with exact examples: one simulated second is
  `1,000,000` horizon ticks; one second of YAML request timeout is
  `1,000,000` microseconds, while CLI `--timeout 1` is one second.
- Unsupported warmup/measurement requests fail explicitly; no observe-only flag
  is forwarded to `run`.

Evidence: workload compiler tests and real QPS/concurrency Result comparisons.

### Step 6: Execute evaluations safely and persist lifecycle

Dependencies: Steps 3-5.

Work:

1. Use managed argv-based subprocess execution and controlled environment/cwd.
2. Persist tasks before scheduling and keep bounded execution concurrency.
3. Limit wall time, stdout/stderr size, artifact size, and queued work.
4. Implement cancellation and process cleanup, including task-owned descendants.
5. Preserve completed evaluation records and partial artifacts with completeness
   markers.
6. On service restart, reconcile tasks: do not leave a task permanently running,
   duplicate an active process, or mark unverified work successful.

Acceptance:

- Shell metacharacters are treated as data or rejected, never evaluated.
- Cancellation is idempotent and terminates task-owned processes within the
  configured grace period; unrelated processes are untouched.
- Timeout tests verify process cleanup and a `timed_out` evaluation.
- Output-limit tests stop execution with an explicit bounded-output error.
- Concurrent evaluations never exceed the configured limit.
- Restart tests leave each task in a documented recoverable or terminal state
  and preserve previously completed evaluations.
- Task/artifact authorization and path containment tests pass.

Evidence: isolated process fixtures, restart tests, and permission tests.

### Step 7: Parse Result and assess feasibility

Dependencies: Step 6.

Work:

1. Parse standard Result JSON, checking supported schema, kind, and required fields.
2. Verify effective topology, model, workload, and overrides.
3. Preserve `metrics`, `metric_defs`, `window`, `requests`, `revisions`,
   `kernel`, optional `goodput`, and optional `saturation`.
4. Separate execution validity, completeness, constraint assessment, and evidence
   maturity.
5. Keep missing metrics absent; reject nonfinite/invalid values.

Acceptance:

- Fixtures cover malformed output, wrong kind/version, topology mismatch,
  nonzero exit, missing metric, zero completed requests, and partial completion.
- Per-token Result `itl_*` is never replaced with legacy per-step ITL.
- Token throughput and request throughput retain different units.
- Goodput and throughput with different denominators/windows are not combined.
- Per-request ITL goodput thresholds and aggregate ITL percentile constraints
  are assessed independently.
- A process-successful SLO miss yields `succeeded` execution plus
  `not_satisfied` constraints; insufficient evidence yields `unknown`.
- Request-ledger validation detects inconsistency without double-counting
  `length_capped`, which is not an independent terminal bucket.
- The raw Result and coefficient assumptions remain available by artifact ID.

Evidence: pinned Result fixtures, exact normalization assertions, and the real
outputs from Steps 4-5.

### Step 8: Implement bounded BLIS topology optimization

Dependencies: Step 7.

Work:

1. Implement `find_configuration`: fixed workload, bounded topology search.
2. Start with approved aggregated dimensions; keep other engine settings fixed.
3. Prune only demonstrably invalid resource/placement/capability combinations.
4. Evaluate remaining candidates through the same exact executor.
5. Apply explicit feasibility constraints, objective, and stable tie-breaks.
6. Persist the candidate manifest and each evaluation outcome.

Acceptance:

- An arithmetic-only eight-device fixture with TP in `{1,2,4,8}` and all fitting
  positive replica counts generates exactly `8+4+2+1 = 15` candidates before
  model/placement pruning.
- A fully evaluated small fixture returns the same winner/Pareto set as an
  independently specified expected result.
- `max_evaluations` bounds actual executor starts, including failed starts.
- Failed/unsupported/pruned/unvisited candidates remain distinguishable.
- Budget or timeout termination returns best-so-far with `search_complete=false`.
- No-feasible-within-budget is not described as proof that no feasible
  configuration exists.
- Ranking uses requested metrics, not an undisclosed weighted score or the
  BLIS fitness normalization by default.

Evidence: deterministic search fixtures and a small real BLIS sweep.

### Step 9: Implement fixed-topology capacity sweeps

Dependencies: Step 8.

Work:

1. Implement `find_capacity` separately from topology recommendation.
2. Sweep an explicit QPS or concurrency grid at fixed effective configuration.
3. Hold workload shape and comparison controls constant.
4. Assess completion, SLOs, truncation, and saturation/backlog evidence.
5. Share a global budget if capacity search is nested inside topology search.

Acceptance:

- Every rate point changes YAML `aggregate_rate`, not only CLI `--rate`.
- Every concurrency point remains native closed-loop concurrency.
- One candidate with a low completed-request P99 but excessive failures is
  not accepted as feasible.
- Finite-workload drain alone is not claimed to prove sustainable capacity.
- The answer states the highest evaluated feasible grid point and unresolved
  intervals, not a continuous maximum or untested monotonic boundary.
- Nested search never exceeds the total evaluation budget.
- Same-seed reruns are treated as reproducibility checks, not independent samples;
  distinct seeds and per-seed outcomes are recorded when robustness is assessed.

Evidence: capacity-grid fixtures, budget tests, and a real curve with feasible
and infeasible or explicitly inconclusive points.

### Step 10: Add complete P/D execution and search

Dependencies: Steps 7-9 and a runtime-confirmed P/D support envelope.

Work:

1. Compile prefill/decode pools, per-role engine/parallel settings, fabric,
   connector, and EPP disaggregation configuration.
2. Set total/prefill/decode instance counts explicitly.
3. Validate role placement and fixed-runtime cross-pool restrictions.
4. Add P/D candidate enumeration only after exact P/D evaluation passes.

Acceptance:

- A real supported P/D run resolves both intended roles and topology.
- The router configuration actually disaggregates requests; role counts alone
  do not satisfy the gate.
- Missing router files, incompatible pool settings, unsupported connectors,
  and insufficient role capacity produce explicit failures.
- Generated relative references survive bundle relocation.
- Per-role coefficient provenance and P/D breakdowns remain attributable.
- A small P/D sweep respects both role budgets and the global evaluation limit.
- P/D availability is gated independently from aggregated availability.

Evidence: exact P/D bundle, real Result/router evidence, negative fixtures,
and a bounded P/D sweep.

### Step 11: Add trace replay as a separately gated workload mode

Dependencies: Steps 6-7; Step 10 only for P/D replay.

Work:

1. Resolve registered TraceV2 artifacts and invoke `replay`.
2. Preserve fixed-arrival versus response-driven session semantics.
3. Use explicit horizons on run/replay parity checks.
4. Respect recorded router constraints instead of silently overriding them.
5. Keep optional conversion a separate attributable operation; do not claim
   arbitrary input traces are natively supported.

Acceptance:

- A run-export-replay fixture with matching controls produces equal `metrics`,
  `requests`, and `breakdowns` as promised by the locked runtime.
- Replay's automatic horizon cannot silently truncate a task that requested
  matching run/replay semantics.
- Incompatible recorded router inputs are surfaced, not silently replaced.
- Fixed trace replay and supported closed-loop session replay have separate tests.
- Trace paths and referenced files are owner-scoped and contained.
- No deprecated `observe`/`calibrate` invocation is introduced.

Evidence: trace digests, run/replay parity outputs, and negative router tests.

### Step 12: Aggregate dual-tool results and cross-evaluate candidates

Dependencies: Steps 2, 7-9; Steps 10-11 for their respective modes.

Work:

1. Run AISimulate native discovery and BLIS sweep independently.
2. Preserve `generated_by` independently from `estimated_by`.
3. Union candidates by effective configuration and cross-evaluate only where
   both tools can represent the same inputs.
4. Charge cross-evaluation against an explicit budget.
5. Compare only compatible metrics and retain disagreement.

Acceptance:

- Each selected tool executes; one failure does not cancel the other.
- One success and one failure yields `partial` with both records; all failures
  yield an explicit failed outcome.
- Distinct effective configurations never merge merely because TP/replicas match.
- Identical candidates retain two evaluations without overwriting either.
- Unsupported, incompatible-input, and budget-not-evaluated cases have different
  explanations.
- A statistic/window/formula mismatch disables numeric comparison.
- No average, favorable-result selection, or implicit fallback is introduced.

Evidence: dual-tool dispatch, identity, error isolation, budget, and
comparability tests.

### Step 13: Wire API, MCP, configuration, and Agentic

Dependencies: Step 12 for common flows; optional capabilities retain their gates.

Work:

1. Implement the section 4.3 endpoints and existing permission checks.
2. Route candidate search and exact estimates through the estimate service.
3. Replace unconditional AISimulate capability gating with selected-tool support
   checks, without weakening independent deployment validation.
4. Carry tool selections through create/edit/retry/refinement and persistence.
5. Add tool-specific prediction evidence to Agentic facts, candidates, and prompts.
6. Update MCP operation IDs/descriptions and generator tool allowlists.
7. Preserve compatibility reads of old AIC-only records.

Acceptance:

- Old `/api/v1/aic/*` contracts remain compatible.
- New operations enforce the same candidate-search permissions and task ownership.
- BLIS-only requests never hit hidden AIC calls in support, search, refinement,
  or exact-estimate paths.
- Tool choice survives save/load, editing, retries, and Agentic refinement.
- Unsupported estimation alone does not invalidate a deployable manual topology.
- Agentic evidence names tool, configuration, maturity, and relevant metric
  definitions; conflicts cannot silently become an SLO-satisfied claim.
- Live resource validation and deployment approval remain authoritative.
- Old stored candidates load without fabricated BLIS evidence.
- `node hack/generate-mcp-tools.mjs --check` passes after route changes.

Evidence: affected candidate/configuration/Agentic contract tests, API permission
tests, persistence compatibility fixtures, and MCP catalog validation.

### Step 14: Complete UI, packaging, documentation, and release validation

Dependencies: Step 13.

Work:

1. Add AISimulate/BLIS multi-selection, defaulting to AISimulate.
2. Display request-specific availability, progress, per-tool results/errors,
   metric definitions, experimental status, and partial/search-incomplete state.
3. Preserve topology application and record which estimate justified selection.
4. Package a supported BLIS runtime or document an explicit opt-in installation;
   the default AISimulate path must work without BLIS installed.
5. Update English and Simplified Chinese user/API/environment documentation.
6. Update affected canonical Agentic design sections when their implementation
   actually changes; do not document planned behavior as already deployed.

Acceptance:

- Browser tests cover default, BLIS-only, both-tools, partial failure, unavailable
  tool, empty result, timeout, cancellation, and saved-selection reload.
- Changing selection or workload invalidates stale in-flight results; late
  responses cannot overwrite a newer request's state.
- Tool results and errors remain independently visible and keyboard-accessible
  in both supported themes.
- A selected topology still produces the intended deployment configuration;
  prediction does not bypass approval or resource checks.
- A clean runtime without BLIS retains the default AISimulate flow.
- All affected Python tests/lint, frontend tests/lint/type checks, MCP catalog,
  and documentation checks pass; build checks run where module wiring or
  packaging requires them.
- Public documentation matches behavior in both languages.

Evidence: focused validation commands and outputs, browser scenarios, packaging
smoke test, and updated documentation.

## 6. Remaining policy decisions and feature gates

The following numerical or product policies are not approved merely by this
plan. Resolve them before implementing behavior that depends on them, record
the chosen values here, and test exact boundary values.

| Decision | Required before |
| --- | --- |
| Approved runtime profiles and supported model/hardware/precision matrix | Steps 1 and 3 |
| Numerical queue, output, disk, wall-time, and evaluation limits | Steps 6 and 8 |
| Minimum sample size/window, allowed failure/truncation rates, and saturation evidence | Steps 7 and 9 |
| Default optimization objective and deterministic tie-breaks | Step 8 |
| Warmup and steady-state measurement policy | Steps 5, 7, and 9 |
| Cross-evaluation allowance and search-space distribution between tools | Step 12 |
| Agentic selection behavior when tools disagree or only experimental evidence exists | Step 13 |
| Retention, cleanup, and operational installation defaults | Steps 6 and 14 |

Until an automatic conflict policy is approved, preserve per-tool conclusions,
mark disagreement/incomparability explicitly, and do not manufacture a combined
positive SLO conclusion.

## 7. B60 acceptance boundary

The separately maintained B60 enablement work reported unresolved kernel
construction, formal coefficient, measurement-contract, and independent
validation gates at the time of this plan. Its working notes and diagnostic
archives are not a supported runtime profile.

There are three separate gates:

1. **Integration:** Lens compiles inputs and executes a compatible BLIS runtime.
2. **Hardware execution:** that runtime can construct and successfully simulate
   the specific B60 model/topology.
3. **Prediction validation:** frozen coefficients and independent measurements
   establish the stated accuracy and applicability scope.

Passing gate 1 does not pass gates 2 or 3. Do not release experimental fit
candidates, remove kernel refusal checks, substitute another accelerator, or
claim capacity/SLO trustworthiness to make integration tests pass.
Use a supported non-B60 reference scenario for the first end-to-end gate.

## 8. Completion and evidence checklist

Record each completed step with: source revision, runtime profile/digests,
test inputs, command, actual outcome, artifact references, and limitations.
Reuse results only while their source, inputs, and dependencies are unchanged.
Do not put credentials, raw private prompts, or unrelated cluster data in
committed test evidence.

- [x] Step 1: pinned real runtime and deterministic aggregated smoke test (2026-11-10; exact experimental fixture and local evidence in section 2).
- [x] Step 2: public contracts and unchanged AISimulate default behavior (2026-11-10; evidence in section 5).
- [x] Step 3: managed profiles and request-specific support (2026-11-10; exact Step 1 envelope only).
- [ ] Step 4: correct aggregated bundles and placement.
- [ ] Step 5: explicit QPS/concurrency workloads and units.
- [ ] Step 6: bounded, authorized, durable execution and cancellation.
- [ ] Step 7: Result parsing, accounting, and feasibility.
- [ ] Step 8: bounded topology optimization.
- [ ] Step 9: fixed-topology capacity curves.
- [ ] Step 10: complete P/D execution and search.
- [ ] Step 11: trace replay and parity.
- [ ] Step 12: independent dual-tool results and budgeted cross-evaluation.
- [ ] Step 13: API/MCP/configuration/Agentic integration and compatibility.
- [ ] Step 14: UI, installation, documentation, and release validation.

An intermediate release must name which gates passed and keep the remaining
operations explicitly unavailable. It must not be described as completion of
this full plan.
