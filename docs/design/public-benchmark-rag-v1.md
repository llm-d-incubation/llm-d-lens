# Public Benchmark RAG v1

## P0 / P2 delivery order

Implement input normalization (P0) before bounded multi-source recommendation
(P2); full online acquisition and scheduling (P1) is the final stage.
Agentic creation/refinement share `planning-cases.v1` evidence packets from
authorized internal SQL, approved public SQL and separately gated OpenSearch
documents. No schema migration, automatic acquisition, new external model or
standalone RAG frontend is part of this increment.

Each SQL branch selects up to 50 ranked experiments, not 50 measurement rows;
model context contains at most ten
complete experiment/series cases and twenty passages under a conservative
16,000-byte serialized evidence ceiling (a conservative token upper bound, not
tokenizer output). Model context does not duplicate case measurements as legacy
observations; audit records retain their full selected payloads and exclusions.
Method-diverse configuration passages receive a reserved share after the best
complete measurement case, before final packet accounting.
Budget exclusions and branch failures remain
visible. Cases retain raw/normalized units, statistic, provenance, applicability,
unknowns and validation requirements. Cross-source run identifiers do not alone
prove independent corroboration. Configuration-intent queries cover settings,
applicability, prerequisites and limitations alongside workload/SLO context.
After validated generation, one query per distinct deployable provider technique
(at most five) supplements the initial search under a separate shared 10-second
budget; generation does not consume that budget. The initial retrieval stage
retains its 60-second deadline. Each query reranks up to twenty candidates before
retaining five complete passages; the merged bundle still caps at twenty.
Passage-body configuration/constraint cues and marginal technique coverage drive
an explicit heuristic ranking, with source/title hints only assisting bodies
that contain such cues. Generic rollout/API passages are not configuration
support. Both model projections label relevance as heuristic and require actual
textual support or abstention. Request-time trimming removes low-ranked documents
without changing SQL case order or truncating paragraphs. Branch diagnostics
preserve relevance cues, ranked/selected IDs, query outcomes and timeout budgets.
Target-model examples and registered hardware-profile cues refine relevance;
unknown hardware is neutral, and profile agreement does not prove exact-device
compatibility. After the first relevant passage, uncovered methods precede
repeated contexts, so source-specific guidance can remain as a qualified reference.
Authority-member reads use request-local `_mget` batches of at most 256 records
with complete validation and fresh-head checks. Hybrid honors the requested
candidate limit instead of capping it at ten; cold model readiness has its own
eight-second bound, separate from the five-second query/vector-page deadline.
Existing candidate validators,
approval requirements and S3/S4 rollout gates remain authoritative.

### Latest experiment-ranking contract (2026-10-28)

The Agentic-only planning service ranks all eligible, authorized, published
exact-model/hardware experiment/configuration-and-series cases **before**
applying its internal `experiment_limit=50`. It must not rank only the first
50 measurement rows. Only measurements from the same experiment/configuration
and series may form one case; different runs, configurations or series cannot
be joined to manufacture complete coverage.

The public SQL implementation scopes publication membership once and materializes
the small publication-scoped experiment/file dimensions. It ranks compact, fully
authorized experiment/series metadata, applies Top-K, then fetches complete rows
by selected primary-key IDs instead of repeating wide eligible-row/manifest joins.
Publication and authorization predicates remain intact; this avoids repeated
scans without raising the 15-second branch budget or truncating measurements
before whole-experiment ranking.

Explicitly supplied workload similarity and coverage are the first ranking criteria; lower
observed valid latency is second. Do not rank by distance to an SLO, minimum
target violation or SLO status. Default zeros are not explicit workload input.

| Explicit workload | Explicit latency targets | Ranking |
| --- | --- | --- |
| Yes | Yes | Workload match, then lower observed latency for the requested exact metric/statistic pairs. |
| No | Yes | Lower observed latency for the requested exact metric/statistic pairs, without a workload penalty. |
| Yes | No | Workload match, then available latency grouped by exact metric/statistic. |
| No | No | Available latency grouped by exact metric/statistic, without a workload penalty. |

Targets restrict comparable metric/statistic pairs; their numeric thresholds
do not define a closeness score. As of the 2026-10-28 operator policy update,
a null statistic defaults to `mean` for query filtering, planning comparisons
and model context; explicit percentiles remain distinct. Stored measurements and
query evidence preserve the original null. Numeric answer text labels the default;
model measurements and comparison annotations carry `statistic_original=null`
and `statistic_basis="default_mean"`. Multiple latency metrics
use Pareto dominance only on equal available metric/statistic sets: no worse
on every comparable metric and better on at least one. Preserve nondominated
tradeoffs; never sum TTFT, TPOT and end-to-end latency or mix mean with p99.
Different coverage sets remain distinct, with coverage and unknowns explicit.
Ranking metadata carries `queue="ranked"|"fallback"` and `fallback_reason`.
Fallback applies if and only if no measured workload fields were compared
(`workload_similarity=null`, including no requested workload) and no valid latency
exists on requested/comparable axes. Its reason is
`no_comparable_workload_or_valid_latency`; ranked cases use null. Explicit measured
zero similarity is a real comparison, not fallback when latency is absent.
SQL unknown workload scores remain null consistently with Python. Case model
context, audit and UI explicitly label fallback references and never describe
them as low latency. Query summaries use neutral experiment-reference language.
No observation is not zero latency. Valid latency must be positive, finite
and use metric-specific compatible units; documented compatible conversions
are allowed, but incompatible dimensions are not. Zero error counters remain
valid context, not latency. Low historical latency does not prove that the
current deployment meets its SLO.

Configuration source clues (`run_id`, `source_path` and
`source_info.file_identifier`, including scorer and prefix-cache path names)
remain in generator/planner context. They are untrusted configuration hints,
not verified serving parameters or candidate-specific performance guarantees.
Bulky import metadata and raw URLs remain audit-only. This projection participates
in the existing evidence budgets on both creation and refinement.

The model-facing `configuration.technique_hints` explicitly extracts recognized
labels from those three source fields. Each hint carries `provider_ref`,
`technique`, `source_field`, `source_value` and
`verification="unverified_source_hint"`. The provider is a possible current
technique association, not a verified historical provider or guide. No hint is
inferred from generic prefix/cache labels, import metadata or raw URLs. Both AI
stages receive this projection, without mutating historical configurations.
The selector prompt requires adoption/exclusion reasons for related supplied
candidate providers, even outside the returned top three, and case/measurement
citations for those comparisons. Technique-specific measurements must not be
relabeled as generic baseline performance. This is a prompt requirement, not a
deterministic guarantee of model compliance or a new ranking rule.

Storage admission is not configuration completeness validation:
`public_benchmark_records` stores source/publication links and measurements as well
as experiments. The experiment contract requires `source_pointer`, but permits
null configuration. The Prism importer retains nonmetric record metadata in the
configuration object, so a nonempty object may still lack serving settings.
Such records remain historical references with explicit unknowns; they must not
be presented as reproducible, fully specified deployments.

Candidate bundles and subsequent budget selection preserve workload/latency
ranking before quality or freshness tie-breaks; neither may reintroduce
SLO-status ordering. Selected experiment evidence retains complete relevant
measurements, subject to an explicit safety cap with visible exclusions, so
planning responses may contain more than 50 measurement envelopes. Compact
model-context budgets remain separate: at most 10 complete cases, 20 passages
and a 16,000-byte serialized evidence ceiling. Experiment reference ordering is
separate from deterministic candidate scoring and server SLO assessment.

The generic public REST/MCP query is unchanged: `QueryRequest.limit` still
limits returned measurement/evidence envelopes to 1–50 after verified merges,
not experiments. `experiment_limit` is an internal Agentic service argument,
not a new public request field. Proposed optional planning-response diagnostics
are `matched_experiments`, `returned_experiments` and `experiment_limit`;
measurement counts retain their existing meanings. These diagnostics do not
redefine generic endpoint behavior.

Scoped 2026-10-28 validation passed: the seven-file regression had 476 passing
tests before the final SQL/service-test edits; subsequent SQL regression passed
99 tests, and updated isolated create/refine variants passed two tests.
JavaScript presentation tests passed 11 tests; affected lint/formatting passed.
The SQL suite includes a 9,304-measurement fixture checking SQL under five seconds
and total query under the unchanged 15-second budget.

Authenticated live creation and refinement passed without an external AI provider
or approval/deployment/experiment-ingestion calls. Creation selected 50 experiment-series/
400 complete measurements from 63/504, retaining five ranked cases/40 measurements
in 15,343 bytes. Refinement to unavailable TTFT P90 retained five fallback cases/
40 measurements in 15,542 bytes, with null workload scores, zero latency coverage,
unknown latency/SLO status and explicit UI fallback/not-low-latency labels.
Authenticated GET and append-only `0600` snapshots verified both queues; generic
`limit=3` still returned three envelopes. The existing H100-over-A100 simulation
was labelled and deployment disabled; the plan remained awaiting approval.
This is not physical H100, current SLO, deployment or S3/S4 certification.
The initial SQL timeout remains in audit history; the final optimized path passed
without raising timeouts. A pre-existing duplicate React key warning remains.
See [scoped ranking validation](agentic-deployment-design.md#scoped-ranking-validation-2026-10-28)
for test timing and scope. Historical validation below remains a separate checkpoint.

## Approved final storage architecture (2026-10-27)

This is the current contract and supersedes all historical checkpoints below.
Public documents are OpenSearch-only, with no PostgreSQL authority gate or SQL
fallback. `public-rag-management-v1` owns document sources, approvals,
publication manifests, build history and rebuild requests. Retrieval aliases
are the sole active authority: each request pins one physical build and model
contract. Immutable source bytes remain in the approved raw archive; neither
OpenSearch nor the embedding cache replaces it.

SQL `public_benchmark_records` is reserved only for Prism experiments and their
required provenance/publication associations, never for OpenSearch RAG.
The approved cleanup creates this table empty: no legacy records are copied
and no Prism snapshot is imported. Internal experiments are unchanged.
Mixed document releases can preserve original manifest hashes,
membership identities and citation IDs; OpenSearch carries only the external
identity/dependency descriptors needed for document retrieval, not structured
experiment, measurement, figure or link fact bodies. Internal experiment storage
is unchanged.

The old SQL document definition/full-text retrieval and SQL search coordinator
paths are removed. The approved SQL-only cleanup drops `rag_items`,
`rag_search_builds` and `rag_search_pointers`, then creates empty Prism storage.
Existing Prism rows or a preexisting target table block destructive cleanup.
The old tables are not runtime fallback stores.
`LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY` is removed;
legacy `postgres` configuration is rejected, not a way to restore SQL documents.

The SQL-only `benchmark_rag/cutover.py` module uses existing database settings.
Operators must freeze writers and retain verified backups in a persistent directory.
The command never connects to OpenSearch, changes aliases, rebuilds indexes or
removes original source snapshots. This replaces the earlier migrate-and-rebuild plan.
Run the implemented cutover from the configured backend environment:

```bash
python -m llm_d_lens.benchmark_rag.cutover --execute --writers-quiesced \
  --backup-dir <persistent-backup-dir>
```

The caller must stop or quiesce **all legacy and document writers** before
execution and keep them frozen throughout. `--writers-quiesced` asserts this
precondition; the command does not stop the service itself. It preserves
verified `legacy-sql.zip` with old table rows and checksums, then drops the old
tables and initializes empty Prism storage under exclusive SQL transaction locks.
Managed installations use revision `8bf2a6c1d903`; unversioned installations run
the same guarded transaction without inventing Alembic history.
A final `committed.json` confirms SQL cleanup only, not document migration,
retrieval readiness or S3 quality acceptance. Existing OpenSearch indexes remain
unchanged and are not certified usable by this cleanup.
On failure, keep the service stopped and inspect the preserved backups and
state before recovery; there is no flag-only PostgreSQL rollback.

Administrative `backup`, `verify` and `restore` default to OpenSearch document
bundles; structured SQL transfers require explicit `--structured`. Document
verification is offline. Restore is not an approval grant or alias activation:
it must preserve revocations, and serving restored documents requires current
source approval, publication and verified retrieval rebuilding. `refresh`,
`status` and source `rollback` route Prism to SQL and other sources to the
document repository. Existing `rebuild` remains the Prism SQL import command,
not an OpenSearch retrieval rebuild; use `PublicRagIndexBuilder` or the explicit
`search-rebuild` administration command for the latter.

`DocumentLifecycle` coordinates document metadata/alias writes with persistent
single-host `flock` at `LENS_DATA_DIR/public-rag/publication.lock`. All writers
must share that path; only `LENS_PUBLIC_RAG_WRITER_MODE=single-host` is supported.
Unknown alias-mutation outcomes fail closed. Operators must drain in-flight
requests before explicit reconciliation; neither queries nor restore silently
repair aliases. Source-head rollback is not retrieval-alias recovery. There is
no flag-only PostgreSQL rollback; recovery must preserve post-cutover
publications and revocations in the approved OpenSearch architecture.

The SQL-only cutover was subsequently committed, and Prism was imported separately,
as recorded in the user-maintained execution plan. The historical checkpoints
below do not certify current corpus quality, complete D1 acceptance or production readiness.

### S4 backend controls and D2 integration boundary

The current delivery target is a backend that Agentic can integrate, observe,
enable for approved users and roll back, not a standalone RAG product launch.
S4 engineering and D2 integration may proceed in parallel; S3 performance
acceptance is deferred until the end. Failed quality gates are not waived.
Document consumption defaults disabled, then moves through shadow validation
before explicitly approved enabled use. No standalone RAG frontend is maintained.

`benchmark_rag/runtime.py` owns a local, single-host operational policy at
`LENS_DATA_DIR/public-rag/runtime.json`. It does not become document publication
authority. The policy has strict settings, generation CAS, cross-process locking,
private atomic durable writes, and explicit failures for corrupt/unreadable state.
Absent policy preserves already shipped direct search and disables Agentic use.
Every Agentic rollout requires explicit stable user IDs plus the caller's separate
GLOBAL query grant; admin status does not bypass the rollout allowlist.
Agentic defaults lexical; hybrid is an explicit choice, with no reranker enabled.

`GET /api/v1/benchmark-rag/runtime` exposes sanitized configuration presence,
pinned embedding contract, active/latest builds, task counts/state, source
staleness, rebuild/recovery state and bounded process-local search telemetry.
Model-file presence is not verified readiness. Credentials, service URLs and local
paths are not returned. Dedicated GLOBAL runtime-read/configure permissions are
admin-only by default; `PUT /runtime` additionally requires normal browser CSRF.
The read operation is an MCP tool; the rollout mutation is intentionally not.
Operational connections/models remain operator-configured; this work does not
add a resource-configuration frontend or a new credential store.

`search_enabled=false` blocks new document searches, including the legacy
definition adapter, but not SQL experimental queries. Disabling advanced search
blocks vector/hybrid and preserves lexical. In-flight work must be drained before
emergency repair. Search outcomes and timings are logged without questions/text;
status metrics are per worker since startup, not persistent fleet-wide metrics.

The existing administrative module now supports `search-status`,
`search-rebuild --mode lexical|vector --origin ...`, and
`search-recover --mode lexical|vector --requests-quiesced`, each with the explicit
target `--data-dir`. Rebuild origins describe a complete replacement corpus, not
an incremental addition; repeat the flag for every desired approved published
scope. Source enable/disable uses explicit expected-generation checks. Source
rollback uses the existing manifest/generation operation followed by a fresh
retrieval build, never an old-index alias shortcut.

Portable document bundles exclude rollout policies, credentials, aliases and
model caches. A clean restore therefore cannot enable users or revive withdrawn
sources: explicit source approval/publication and fresh retrieval rebuilding
are still required. Restoring to an existing target preserves its local controls;
operators must review them before recovery, not assume a bundle resets policy.

On 2026-10-27, the focused D1 lifecycle integration test passed against the local
OpenSearch 3.3.2 service (1 passed). It used a generated source and a temporary
raw archive, and exercised publication, index build and search; unique alias
resolution; stale-worker fencing and concurrent activation; explicit recovery
after the alias mutation succeeded but its response was lost; citation hash,
path and source-text verification; and withdrawal filtering. The recovery case
injects a lost response after a real alias mutation; it does not kill a worker
process. The test used unique aliases and a unique management index, cleaned up
its temporary indexes, and left the pre-existing active aliases unchanged.
The previously run focused lifecycle, caller and compatibility suite passed
317 tests.

This does **not** verify restoration or rebuilding of the current corpus archive:
no reviewed eight-scope backup bundle was available. The original v1 fixed quality
set remains a two-GitHub-repository historical baseline. A new 100-case v2 draft
now covers all eight active source scopes and reports per-source metrics, but has
not been run against a verified full-corpus bundle or independently reviewed.
S3 relevance, quality and per-source acceptance remain incomplete. **D1 is not
complete.** Successful SQL cleanup alone is not D1 or S3 acceptance.

The v2 draft contains ten source-targeted positive queries per scope (two in each
of five categories, balanced across development and holdout), ten cross-source
disambiguation queries, and ten all-scope no-evidence queries. The v1 fixture is
preserved unchanged for historical comparisons. Sparse labels are diagnostic;
source coverage alone is not a quality pass.

The quality runner searches all eight pinned scopes for every query; target
origins are attribution, not implicit retrieval filters. It scores deduplicated
global Top-10 parent evidence without promoting source-local ranks. Per-source
query averages retain those global scores, with target-source seed hits reported
separately. Reports include category/split intersections, source macro averages
per split, and micro recall from total labeled hits divided by total relevant
labels. Stale or degraded responses fail evaluation. The integration quality
checks apply the unchanged relevance thresholds to holdout as well as overall
results; these numerical checks do not replace independent annotation review.

The 2026-10-27 live, read-only 300-request static run finished without changing
aliases or labels. Hybrid overall Recall@10/nDCG@10 were 0.8000/0.6571 and holdout
were 0.8333/0.6917: both fail the 0.90/0.80 thresholds. Human sign-off remains
0/100, 1,989 pooled question/parent pairs remain unreviewed, and the 17 freeze
blockers remain. This is sparse-label diagnosis, not S3 acceptance; details are
in the [implementation plan](../rag-implementation-plan.md).

## Historical D1 gated proposal (2026-10-26; superseded)

The following gated workflow records an earlier implementation checkpoint,
not current configuration or operator instructions. The approved architecture
above removes its SQL gate, SQL coordinator and continuing SQL document baseline.

This section supersedes earlier PostgreSQL-only public-document authority and
recovery descriptions for the gated mode only. It does not certify deployment.
`LENS_PUBLIC_RAG_DOCUMENT_AUTHORITY` accepts `postgres` (default) or `opensearch`;
keep the default until a verified shadow migration permits an explicit switch.
SQL exact measurements, internal experiments and shared manifests remain unchanged.
The `/benchmark-rag/query` definition branch uses the configured document repository;
numeric, comparison and figure experiment retrieval remain SQL-backed.

In `opensearch` mode, `public-rag-management-v1` owns public documents, source
approvals, publication manifests, build history and rebuild requests. The selected
public alias is the sole retrieval authority, with one physical build and model
contract fixed for each request. Immutable source bytes stay in the approved
local archive layout; neither OpenSearch nor embedding cache replaces that archive.
`DocumentRepository.from_environment()` validates the authority flag and explicitly
constructs the synchronous OpenSearch repository, requiring its configured URL.
`configured_document_repository(client=None)` selects `RagQueryDao` only for
default/explicit `postgres` mode and the document repository for `opensearch`.
Invalid flags and OpenSearch failures never fall back to SQL.

Only source and passage fact bodies may be written to OpenSearch; experiment,
measurement, figure and link bodies are rejected. Shadow migration validates the
full caller-supplied SQL snapshot in memory, then writes inert source/passage
shadows and external identity/dependency descriptors with hashes. Descriptors
contain no model, configuration, workload, values or source-pointer fact bodies.
Shared manifest bytes, hashes and membership IDs are preserved exactly, not
rewritten as a document-only release. Complete verified descriptors support
mixed-release document retrieval without SQL joins or manifest splitting.
SQL experiment facts and their manifests remain authoritative for experiments;
new document staging still rejects mixed experimental fact publication.
Prism remains entirely SQL-owned. Shadow copy/promotion validates the full
supplied SQL baseline but omits all `PRISM_ORIGIN` records and external references
from the OpenSearch projection, reporting `sql_only_origins=(PRISM_ORIGIN,)`.
The repository rejects Prism registration, immutable records and external references.
Other mixed document-source manifests retain exact hashes and membership descriptors.
Direct document staging rejects raw structured fact bodies, but future SQL mixed
updates use the verified baseline workflow without source/hash splitting.

`promote_documents(repo, snapshot, *, verified_snapshot_sha256, expected_heads, archive, legacy_writers_quiesced=False, fresh_snapshot=None)`
refuses by default. Administrators must quiesce legacy publication/approval
writers before obtaining a fresh full authoritative SQL export and keep them
frozen through promotion. Shadow-copy/verify the baseline, then pass
`legacy_writers_quiesced=True`, `fresh_snapshot=fresh_export`, the verified digest,
source archive and expected target heads for every origin. The full fresh export
is validated in memory and its digest must match the verified shadow; a newer SQL
write/revocation fails before target mutation. No writer stopping or SQL connection
is performed internally, and target checks are not a cross-store transaction.
Missing baseline registries
are installed with original approval/history/generation; existing target
registries are never overwritten, so newer revocations win. State drift fails
before writes, identical replay is idempotent, and neither aliases nor the
authority environment setting are switched automatically.

Future mixed-release updates are verified through this sequence: unchanged
SQL-owned importer publishes a newer mixed release; frozen fresh-baseline shadow
and promotion import immutable documents/external descriptors but preserve the
existing target head; explicit
`RagPublicationService(dao=repo).activate(origin, new_original_manifest_id, expected_generation=current_target_generation)`
approves document-head advancement, followed by `PublicRagIndexBuilder` rebuild.
Disabled/revoked targets reject activation until explicit current source
reenable. Original source/manifest hashes and citation IDs are retained;
SQL facts are not rewritten. Baseline replay never automatically reenables a source.

Document bundles preserve exact shared manifests, carrying external descriptors
separately in checksummed `external_records.jsonl`, without structured fact bodies.
Mixed bundles need complete valid descriptors; legacy mixed bundles lacking
proofs fail closed. Restore imports immutable documents/descriptors but skips all
source registries, aliases and active-build state. SQL experiment transfer is unchanged.
Source staging/publication is separate from a D1 retrieval rebuild using
`PublicRagIndexBuilder` with the document repository and `DocumentLifecycle`.
Builder defaults select D1 when the authority gate is enabled. The existing
`admin.py` rebuild remains Prism SQL import, not OpenSearch retrieval rebuilding;
no new CLI is specified here.
Existing admin `backup`, `verify` and `restore` select `DocumentBackupService`
under `opensearch`; `postgres` preserves SQL transfer. Document backup/restore
needs no database URL and opens no SQL; verification is offline with no endpoint.
Document restore `--initialize` initializes the management index.
Administrative source `refresh`, `status` and `rollback` also select the document
repository for non-Prism sources in `opensearch` mode without SQL or a database URL.
Source rollback does not recover a retrieval alias. Explicit Prism refresh/status/rollback
(origin detected from the refresh plan or `--origin`) and Prism rebuild remain SQL
regardless of the gate. Document backup validates the full SQL baseline in memory
before excluding all Prism records, references and blobs; Prism-only baselines
fail because they contain no OpenSearch-owned documents. Restore rejects any
bundle containing Prism before writes; normal document snapshots contain none.
Promotion may install missing non-Prism baseline registries; restore never creates them.
D1 retrieval rebuilding uses the Python builder.

Lifecycle coordination uses persistent `flock` at
`LENS_DATA_DIR/public-rag/publication.lock`, not in the runtime directory.
All writer processes on the host must share this persistent data path and keep
the full metadata/alias operation in the same synchronous thread/context.
There is no expiring lease. `LENS_PUBLIC_RAG_WRITER_MODE` accepts only
`single-host` (default); repository construction explicitly rejects `multi-host`
and every other unsupported value. This is not multi-host HA.
Unknown alias-mutation outcomes fail closed. Operator recovery requires first
draining outstanding requests using the old OpenSearch alias; normal requests do not guess whether an uncertain
mutation succeeded, and restore never automatically restores aliases.
`DocumentLifecycle.reconcile(writer, vector=False, requests_quiesced=False)`
requires explicit `requests_quiesced=True` after that drain; its default fails
closed. This assertion does not itself drain requests. No new operator command
is specified here.

Rollout requires shadow comparisons of source approval state, manifest
membership, passage identities/counts, provenance, builds and retrieval results,
including new publication, revocation and failure cases. Rollback must account
for state written after cutover: replay new publications and revocations into
PostgreSQL and verify parity before selecting `postgres` again. A flag-only
rollback can serve obsolete or revoked evidence and is not supported.
Restore must preserve revocations and deny approval resurrection. An empty target
requires explicit current source reapproval before serving restored documents;
restoring a backup is not an approval grant or alias activation.

Architecture tests are not S3 relevance/quality acceptance. Live acceptance for
D1 has not been claimed; earlier S1 measurements below do not establish it.
Shadow, promotion and portable transfer paths are implemented, but no test, acceptance or public OpenSearch URL
is configured in the current validation environment. Live migration and quality
threshold completion therefore remain unverified.

## Historical S1 public lexical search backend (2026-10-26)

This update supersedes the diagnostic OpenSearch adapter ownership described in
older planning notes. `OpenSearchEvidenceSearchRepository` and its projection
writer now live under `benchmark_rag/` and serve only public document passages.
Diagnostic artifact checksum verification remains in
`agentic/diagnostic_projection.py`; other diagnostic projection contracts remain
separate and are not imported into the public index.

`POST /api/v1/benchmark-rag/search` is a separate read-only REST/MCP operation
with the same GLOBAL public-query permission. It searches title, headings,
standard/English/CJK text fields and exact technical identifiers. Requests pin a
physical index, filter public source manifests, then recheck PostgreSQL
membership and source bytes before returning citations. Section passages use exact
character offsets; table, narrative and custom-JSON context passages must match
the original reviewed parser output, including their provenance pointers. No experiment
join is required for a documentation passage. Existing numeric query semantics
and the internal benchmark branch are unchanged.

`PublicRagIndexBuilder` builds from enabled published manifests, verifies every
source passage, writes bounded bulk batches, checks counts and source generation,
and atomically updates the dedicated public alias. S1 retains full section text
and does not implement vectors, hybrid ranking, distributed activation
coordination, a build-job table, or resource-management UI. A source generation
mismatch is explicitly stale and excluded until rebuild. OpenSearch outages and
invalid/incomplete responses fail explicitly; there is no automatic PostgreSQL
fallback. Operational builds must be serialized until S3 coordination exists.

The two reviewed repository manifests back a local development index with
1,860 passages. The initial 164-test checkpoint was followed by fixed-corpus,
authentication and evaluation acceptance (138 passing tests), existing numeric,
adapter, build and diagnostic regression (74 passing tests), and one final
10k-document performance test. These selections are not a full workspace run.

The 60-query seed suite has six categories, 36 development and 24 holdout cases,
and ten original-source-verified anchors. Judgments are sparse and agent-curated,
not independent human review. The user accepted this initial S0 set and deferred
independent human review and expanded judgments to the S3 pre-release gate.
Lexical positive-query Recall@10 is 0.78 and nDCG@10 is 0.5831; cross-language
Recall@10 is only 0.20. Ten unrelated-domain cases correctly return no results.
These are baseline measurements, not hybrid release acceptance.

Bounded primary-key authority resolution, application-owned HTTP connection
pooling and lazy authority batches preserve current manifest/source verification
while meeting the load threshold. A disposable PostgreSQL and dedicated
OpenSearch 3.3.2 on a shared host exercised 10,000 synthetic passages, concurrency
10, 20 warmups and 500 timed requests per boundary. Final service P95 was 1.3997 s;
ASGI HTTP with real session authentication, CSRF, routing and serialization was
1.6225 s. ASGI excludes network/TLS; synthetic data measures load, not relevance.
OpenSearch was limited to two CPUs, 2 GiB memory and 512 MiB heap.

Real HTTPS login/REST and MCP initialize/list/call returned verified citations;
anonymous search remained 401 and missing CSRF returned 403. The temporary
least-privilege user, role and sessions, load-test container and preview process
were removed; the development index remains active. The initial 5,146-row backup
was also restored and replayed idempotently in disposable PostgreSQL.
The latest user scope explicitly defers frontend work and browser acceptance:
S0-S1 backend completion does not certify a UI or production release.

S2 selects `intfloat/multilingual-e5-small` revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3`: 384 dimensions, 512-token total budget,
`query: ` / `passage: ` prefixes, attention-mask mean pooling, L2 normalization
and cosine similarity. The model card declares MIT; the standalone LICENSE URL
returned 404, so full license material still needs checking before download.
No weights or new embedding dependencies were downloaded and no vectors exist.
Next steps are CPU-bounded encoding, tokenizer-aware provenance-preserving
subchunks, versioned cache and an independent vector shadow index before S3
hybrid integration. Resource-management UI and activation recovery remain later
gates.

The previous implementation checkpoint and historical target/migration states
below do not certify a current deployment. The current preparation and target
operations are recorded in the separately maintained Chinese execution plan.

## Historical implementation status (2026-10-20, after `9bb27e5`)

**Step 3a tooling development and local verification are complete. Real-target
deployment acceptance is not complete.** Steps 1-8 and the 3a tools are committed;
`agentic_deploy_v2` was pushed through `c32dec1`, then through `9bb27e5`.
Step 6a remains deferred; step 3b remains unimplemented. Internal retrieval
still requires explicit opt-in and the caller's owning-context permissions.

This section supersedes older present-tense status statements below, including
"not pushed" and "not started". Those statements describe their historical
checkpoints, not the current branch. Both EN/ZH design plans remain Git-ignored,
local-only files: this documentation update is neither committed nor pushed.
The shared read-only attachments are unchanged.

### Latest code and verification

- `9ae2183` checkpointed review remediation; `c32dec1` completed the query
  parsing corrections and validation documented in the
  [review execution record](../benchmark-rag-review-2026-10-20.md).
- The subsequent portability review reran **83 migration/schema tests** and
  **four public-baseline/repository-portability tests**, all passing with no
  skips. The latter include the real three-source fixed public baseline,
  **20/20 cases**, and pinned guide/document backup/restore.
- Current code verified the existing real three-source bundle:
  **14,961 retained rows and 190 source blobs**. A fresh disposable PostgreSQL
  instance and separate evidence directory restored **14,961 rows and three
  source states**; replay inserted **zero rows** and updated **zero sources**.
  Counts include history and manifests, not independent experiments.
- `9bb27e5` fixes malformed evidence manifests at the archive-read boundary.
  Invalid encoding, ownership, entry shapes and source resolution now produce
  `PublicationError`, which the query service logs and converts to
  `EvidenceUnavailableError`. REST returns **503**, code
  `rag_evidence_unavailable`, without an evidence response or partial answer.
- The **13 new corruption cases failed before the fix**. Final query, REST,
  transfer and publication regression passed **159 tests, no skips**. Each
  corruption case checks archive/query errors, actual HTTP 503 and HTTP 200
  recovery with the original answer after restoring the unchanged manifest.
  Ruff lint/format, Pylance diagnostics on the changed archive reader, IDE
  problems, whitespace, English and docs-link checks passed. Both locale
  builds passed with existing deprecation, OWNERS-link and missing-blog
  warnings; no complete workspace typecheck is claimed.
- The code, regressions and public EN/ZH API documentation are committed and
  pushed in `9bb27e5`. No target deployment or business-database operation was
  performed during this review, fix or local verification.

### Remaining step-3a acceptance

The last recorded target checkpoint is still
[awaiting schema review](#real-target-migration-checkpoint-2026-10-20-awaiting-schema-review):
target revision `d9e3f7b1c5a8`, no `rag_items`, and existing
`audit_logs.cluster_id` drift (`VARCHAR(8)` in the database versus
`VARCHAR(32)` in the model). This target state was **not remotely rechecked**
in the latest review. No decision to alter that business column or bypass
managed migrations is implied.

Before calling 3a complete: recheck the target, approve and apply a compatible
RAG-only native migration, integrate current backend/Node code, align runtime
database/search path and persistent evidence directory, and verify actual
caller grants. Restore the full bundle, compare current/history counts,
numeric samples and hashes, verify repeat restore, REST/MCP, restart
persistence and independent target operation without the source. CLI `rebuild`
remains initial-Prism-only; use `backup -> verify -> restore` for the full
three-source corpus. These local results do not replace real-target acceptance.

## Purpose and boundary

Build a reproducible question-answering index for three public evidence sets: a downloaded Prism snapshot, `llm-d` guides and their published benchmark results, and `llm-d-benchmark` documentation. The first release also searches authorized internal Agentic benchmark records through a separate read-only retrieval path. It answers factual questions about reported results, their experimental conditions, and benchmark terminology. It does not estimate unreported results or recommend a deployment configuration.

This historical design describes a question-answering index in the existing Lens PostgreSQL database. The current OpenSearch architecture and controlled Agentic integration above supersede its storage and integration boundary. Importing public sources must not write to `agentic_benchmark_records` or change measured evidence or SLO decisions. The second retrieval path reads that existing table under the caller's authorization; it does not copy its rows into the public index. The distinct authority and access rules for planning are documented in [Agentic Deploy](agentic-deployment-design.md).

## Source inventory and authority

| Source | Ingest | Answer authority |
| --- | --- | --- |
| Local Prism snapshot under `private/prism-public-2026-10-13/` | `historical-merged-deduplicated.json` contains 249 unique-ID archive-shaped rows: 177 historical records and 72 normalized stage reports. Keep the eight full public run payloads and their original reports for provenance; four run-level summaries are not extra experiments. | Structured numeric evidence after source, schema, units, and workload have been checked. |
| `llm-d/guides/` | Guide Markdown, 20 Markdown result documents, and five custom JSON result files in `benchmark-results/`. Preserve configuration, individual table cells, narrative, and links to experiments. | JSON measurements and published table cells support exact numbers; prose supports only its stated claims. |
| `llm-d/guides/**/benchmark-results/*.png` | 37 images, linked to the relevant report, caption, and scenario. Record whether the published data cover the entire plot, some series, or only a summary. | Trend and visual context only; image pixels are not exact measured values. |
| `llm-d-benchmark/` | Markdown documentation, especially `docs/` and `benchmark-report/README.md`; index experiment and configuration documentation only when needed to interpret results. | Definitions, method, schema, and reproduction steps, never measured results by themselves. |

The Prism snapshot is local and Git-ignored. Its [snapshot README](../../private/prism-public-2026-10-13/README.md) describes deduplication, exclusions, and units. Snapshot stage timestamps are submission times, not experiment times; unknown concurrency remains null. The snapshot does not cover independently configured GCS or GIQ sources. `llm-d-benchmark` report v0.2.x has `version`, `run`, `scenario`, and `results`; the five guide JSON files are custom formats and must not be parsed as v0.2 reports. Keep a report's declared version and metric units rather than assuming every source uses the same schema.

## V1 evidence model

Keep source bytes immutable. Add one PostgreSQL table, `rag_items`, with typed rows for six item kinds:

1. `source`: repository, relative path or Prism source ID, revision or snapshot date, SHA-256, format, access classification, and source URL when verified.
2. `experiment`: stable source-scoped ID, run/stage or guide/scenario, model, hardware, runtime, workload, configuration, timestamp plus its kind, and links to original sources. Missing values stay null.
3. `measurement`: experiment ID, metric name, statistic (`mean`, `p50`, `p90`, `p99`, etc.), numeric value, unit, series/load coordinate if published, source pointer (JSON path or document section/table/row/column), and evidence type (`raw_json`, `published_table`). Store the original value/unit alongside any documented normalized value/unit.
4. `passage`: section-scoped Markdown text or table interpretation with heading path and location within the pinned version.
5. `figure`: path, caption, related scenario, and coverage (`complete`, `partial`, `summary_only`, `unknown`) with a list of known missing series when reviewed.
6. `figure_link`: a typed relation from one figure item to an experiment item, allowing a chart with multiple runs without assuming they are identical.

Every row has an item ID, kind, source identity, pinned revision/snapshot, content hash, access classification, and optional self-referencing `parent_id`. `figure_link` rows also carry a referenced item ID. Keep frequently filtered fields typed (experiment ID, model, hardware, metric, statistic, numeric value, unit, and passage text); use JSONB for source-specific workload/configuration, source pointers, and coverage details, not as a substitute for numeric filtering. Use a stable source-scoped key and unique constraint per item/version for idempotent imports. Index lookup keys with B-tree and passage text with PostgreSQL `tsvector`/GIN; no SQLite or FTS5 index is involved. Table constraints and importer validation must limit which fields and links are valid for each kind. Original documents and benchmark reports remain outside this index, referenced by pinned path and hash.

IDs are scoped to the source repository and pinned revision; the same filename or similar model is not evidence of the same run. Preserve links between stages and run-level summaries without counting both as independent measurements. A matching guide experiment, Prism record, and Agentic benchmark may be linked only when run identifiers or verifiable source metadata match, never on model/hardware similarity alone. Keep original reports available for audit. Do not infer missing hardware, concurrency, quantization, request shape, or experiment time. Document locations (heading, character offsets, and JSON paths) are valid only for their pinned source version; reindex changed files instead of applying stale offsets to new revisions.

For published Markdown tables, extract actual row/column cells with units and series labels; do not index a whole table as a single free-text numeric claim. Carry notes such as sample count, measurement window, single-run caveats, failed requests, and SLO definition with the associated experiment. Do not compute an apparent improvement from metrics with different units, statistics, denominators, or experimental conditions.

## Retrieval and answer contract

Use `rag_items` in the existing Lens PostgreSQL database for the public corpus, with PostgreSQL full-text search for passages. Rebuild these rows from pinned sources without modifying the separately owned `agentic_benchmark_records` table. Vector search is optional only if evaluation shows full-text search misses terminology/paraphrase queries. This answer-only retrieval does not feed the planner's existing OpenSearch or candidate-ranking evidence paths.

1. Authenticate the caller and resolve their permissions before either query. Treat the public corpus as readable only where the source is verified public and the caller is allowed to use the RAG feature. Restrict the Agentic branch to records for which the service can verify the caller's right to see the owning benchmark/evaluation context; a missing or unverifiable owner means omit the record. Apply authorization in the query or before any candidate leaves that branch, including counts and snippets; never fetch all records then rely on the language model to filter them.
2. Classify the question as a numeric lookup/comparison, method or definition, or chart/trend question; extract explicit filters (model, hardware, guide, workload, load level, metric, statistic, and unit). Run two independently authorized retrieval branches: structured/full-text search over `rag_items`, and structured search over `agentic_benchmark_records` through its existing service/DAO contract. For numeric questions, filter experiments and measurements structurally; for definitions, search passages; for figures, retrieve linked reports and coverage status. The Agentic branch contributes measured benchmark records only, never guide passages or image-derived numbers.
3. Normalize the two result envelopes for display (origin, source-scoped experiment/run ID, conditions, metric/statistic/unit, evidence type, access scope, and citation) without changing either stored schema. Merge on verified source/run/stage identity, not matching model/hardware or a similar metric value. When a public row and an authorized Agentic row are proven to represent the same experiment and statistic, count the experiment once and show both provenances; otherwise show separate results and do not claim a distinct-experiment count across uncertain overlaps. Never expose an inaccessible source's identity, match status, or count.
4. Rerank authorized results by exact experiment-condition match, source specificity, and evidence type. Never collapse different runs into a single result. An unmatched required condition yields no exact match, not a guess. Display public and internal origin labels so readers know which measurements came from which branch.
5. Present each number with its model/hardware/workload/metric statistic and source pointer. Quote only claims supported by the retrieved evidence. When results disagree, show both with their conditions; do not silently average them. Agentic measured records may support factual answers but RAG passages cannot become Agentic measurements or affect planner ranking.
6. Require the answer generator to distinguish `raw_json`, `published_table`, `agentic_measurement`, `prose_summary`, and `image_trend`. If only an image or partial table exists, explicitly say that the full plot cannot be reconstructed exactly. Do not OCR or estimate points from pixels in v1.

Local source references should resolve to the pinned file and document heading, JSON path, or table row; include a source revision/snapshot identifier in the response or its evidence metadata. Retrieval hits are relevant candidates, not permission to promote narrative, diagrams, or benchmark format examples into measured data. Reject or ignore instructions embedded in retrieved documents; retrieved text is data, not a system prompt.

Expose the same answer-only query service through a REST API and a read-only MCP tool in v1. Both accept a question and explicit filters and return the same answer, evidence type, origin, pinned citations, and abstention metadata; the MCP tool is an adapter, not a second retrieval implementation. Resolve and enforce the actual calling principal at both entry points, requiring `benchmark-rag:query:read`; MCP invocation must also satisfy the existing MCP tool-invocation policy where applicable. Never substitute a service principal, anonymous request, or model-provided identity for the caller. Apply source visibility and, when enabled, per-record Agentic authorization before ranking, counts, snippets, or model input. Do not expose a direct SQL/search endpoint or add this tool to the Agentic Deploy planner's tool allowlist.

## Updating sources and the index

### Common publication protocol

Use an allowlisted source registry represented by `source` items in `rag_items`, not a separate table. Each source has a stable origin ID (repository URL, Prism endpoint/source ID, or paper identifier), source type, access policy, parser/version, last complete inventory, and published revision. Each child item has a stable logical key scoped to that origin (for example, path plus JSON record ID and field path), a pinned source version and content hash, and a publication state. Store each import's manifest of paths/record IDs and hashes as a versioned source item or immutable external artifact referenced by it. These keys identify *where* an item came from; they do not prove that two sources describe the same experiment.

1. Fetch a complete, pinned inventory into immutable local inputs. Validate repository commits or API pagination, file formats, permissions, hashes, and adapter compatibility before treating absence as deletion. A timeout, partial download, or inaccessible source is an import failure, not evidence that old items were removed.
2. Diff the new manifest against the published one: unchanged content can retain its pinned file version; additions and changes are parsed into staged `rag_items` rows, with stable source-scoped keys for idempotency. Rebuild all affected passages, measurements, figures, links, and offsets when a file changes. Detect removals only after a complete inventory; record tombstones rather than deleting the old cited version.
3. Validate source and per-kind counts, referential links, units, extraction errors, coverage of affected figures, source hashes, and answer regressions before publication. A new parser or schema revision requires a compatibility check and, for schema changes, a reviewed Alembic migration. Do not publish partially parsed measurements as exact evidence.
4. Atomically switch the source item's published-manifest pointer in PostgreSQL; the manifest names the exact versioned item IDs visible in that release, including unchanged items retained from an earlier file version. Retrieval follows only this manifest's membership and valid links, not an independent per-row `active` flag that could expose mixed releases. For relationships across sources, revalidate the affected links on either side and publish both affected releases together, or leave the link unresolved until verified. A failed validation or transaction leaves the previous release searchable; rollback restores its pinned manifest, not a mixture of old and new rows. Serialize concurrent refreshes for the same origin when switching its pointer.
5. Retain superseded items and source bytes under a defined retention policy so prior citations remain verifiable. Delete them only after that policy and dependency checks; do not overwrite pinned content or reuse a source-scoped key for an unrelated experiment. The separately authorized Agentic branch remains a live read of `agentic_benchmark_records`; no public-source refresh copies or deletes its rows.

Run a manual refresh for new Prism downloads in v1 and a commit-triggered or periodic refresh for the two allowlisted repositories once the importer exists. Record the trigger, import status, source revision, previous and new counts, failures, and activation time without logging credentials. The initial 249 Prism rows and current guide file counts are baseline checks, not permanent expected totals. A no-change refresh must be idempotent.

### Update scenarios

| Change | Import and publication rule |
| --- | --- |
| `llm-d` or `llm-d-benchmark` adds files or benchmark results | Compare the pinned commit's complete allowlisted file inventory with the published manifest. Parse only supported new files and their dependencies; add guide measurements only with their model, scenario, units, and table/JSON pointers. Stage affected figure links and publish after validation. New experiment IDs remain distinct until source metadata proves otherwise. |
| Either repository removes, moves, or renames files | A complete inventory establishes removal; retire the old path and its dependent items from default search, while retaining the pinned prior version for citations. Treat a move as a new path linked to the old source only when Git history or content identity supports it; update or invalidate affected figure and cross-document links. Never infer deletion from a failed fetch or a partial checkout. |
| Markdown documentation or result tables change | Reparse the changed file, even if only headings or layout moved; replace its passages, text-search vectors, offsets, table-cell measurements, and outgoing links as one staged unit. Preserve old citations at the old commit/hash. Check changed values and notes against the corresponding experiment; do not let a stale passage or table row cite the new revision. |
| A new repository or dataset is proposed | Require explicit source approval (owner, public visibility/license, allowed paths/formats, retrieval access, retention, and update trigger), a pinned initial release, a source-specific adapter when needed, and representative parsing/answer tests. Register it as a new origin ID; do not silently crawl arbitrary repositories or treat overlapping experiments as identical. A dataset with restricted rights gets no public index until its access path and filtering are designed. |
| A new Prism source is enabled, including GCS/GIQ | Verify the endpoint/bucket authorization and public status, enumerate every accessible page/object and its stable ID/version, and fetch only permitted raw reports. Validate report schema, timestamp kind, units, and overlap with existing archive/Results Store IDs. Publish a separate source release with source-scoped records; link proven duplicates without counting them twice. Partial pagination or authorization failure preserves the previously published release. GCS/GIQ ingestion is a post-v1 expansion, not part of the initial 249-row import. |
| Papers or other research literature are added | First verify publication identity (DOI/arXiv ID plus version), rights to index text, retraction/withdrawal state, and retrieval scope. Keep metadata, abstract or permitted full-text passages, page/section locators, and citation provenance at a pinned version; re-extract if the PDF or HTML changes. Paper claims are explanatory by default, not Prism/guide measurements or Agentic records. Promote a published numeric table only after a dedicated adapter verifies its experimental conditions, statistic, units, and license; never derive exact values from plots. On withdrawal, retire the active source and retain an audit tombstone. This is also a post-v1 expansion. |

Add scenario tests for additions, removals, renames, heading/table changes, partial listings, parser failures, duplicate imports, cross-source link invalidation, source rollback, permission changes, and withdrawn papers. A change to access rights must take effect before the next answer, even if a content refresh has not run; fail closed when current authorization cannot be verified. Published source-version filters and permission checks must be applied before full-text ranking, snippet generation, counts, and cross-branch merging.

## Deployment scenarios and current implementation status (2026-10-19)

Cross-cluster migration and sharing within one cluster are separate deployment
scenarios. PostgreSQL concurrency support is not proof that either deployment
has been delivered. RAG consists of both `rag_items` and the immutable original
evidence archive; sharing or copying the table alone is insufficient.

| Scenario | Current status | Remaining work |
| --- | --- | --- |
| Migrate RAG from cluster A to an independent cluster B | Target-local `rebuild`, `backup`, `verify`, and `restore` commands are implemented and tested in isolated PostgreSQL schemas with separate evidence roots; the user approved the tools review on 2026-10-19. | Cluster operator runbook, target configuration, secure file transfer, and acceptance on two real clusters. No SSH orchestration or continuous replication is implemented. |
| Migrate the entire Lens PostgreSQL database | Not implemented by the RAG tools. | A separately approved database migration plan covering other tables, ownership, roles, schema versions, and associated storage. This is outside the RAG-only scope. |
| Multiple instances share RAG within one cluster | PostgreSQL transaction/locking support and filesystem-backed evidence access exist, but the shared deployment is not delivered. | Common database/schema configuration, shared evidence storage, credentials, deployment wiring, connection budgeting, and multi-instance acceptance. |
| Shared user-facing RAG retrieval | Authenticated REST/MCP retrieval is implemented and locally verified; shared deployment remains unaccepted. | Verify the shared runtime and evidence storage with actual callers. Internal retrieval remains opt-in and owning-context authorized. Shared database access does not replace caller authorization or tenant isolation. |

### Scenario A: migration between independent clusters

Run `backup` in cluster A against its explicitly configured PostgreSQL database
and evidence directory. Transfer the trusted logical bundle through an
operator-approved channel, run `verify`, then run `restore` in cluster B against
its own database/schema and persistent evidence directory. Alternatively, use
`rebuild` with the complete pinned Prism snapshot and explicit source approval.
See the [administration guide](../../apps/backend/src/llm_d_lens/db/README.md#target-local-rebuild-backup-and-incremental-migration).

The bundle includes all RAG rows, source/publication history, tombstones, and
referenced original bytes, not other Lens tables, credentials, database roles,
or global configuration. Restore can merge into a nonempty target: preserve
target history and approvals, reject immutable conflicts transactionally,
adopt the backup's published version when present, and never automatically
re-enable a source disabled on either side. Reapplying the same backup does not
switch publication again. Verified blobs copied before a failed SQL transaction
may remain unreferenced.

After migration the clusters have independent copies; later updates do not
synchronize automatically. The operator must provision the destination and
install compatible code/dependencies. `--initialize` is only for standalone RAG
schemas; a managed Lens database must use its reviewed migration lifecycle.
Real-cluster acceptance is still pending, not implied by the 20 portability
tests or the 173-test database regression.

### Scenario B: sharing within one cluster

Plan for application replicas to use one PostgreSQL database and schema, plus
one accessible immutable evidence archive. Do not restore a separate database
for each Pod. The current `SourceArchive` uses filesystem paths; replicas with
independent local directories do not share evidence merely because they share
PostgreSQL.

Before implementation, review the storage choice: a suitable shared filesystem
is the initial option; an object store or evidence service would require a new
adapter and separate approval. Validate hard-link publication, atomicity,
durability, permissions, and visibility against the selected backend rather
than assuming any RWX volume is compatible. Prefer centralized import,
restore, publication, and schema-migration jobs; give serving replicas
least-privilege read access where their runtime responsibilities permit it.
Configure secrets and total connection-pool budgets explicitly.

Replicas using the same registry share publication, rollback, and source-disable
effects. Independent releases or tenants need a separately reviewed isolation
boundary; PostgreSQL schemas alone do not establish application authorization.
Missing evidence or unavailable storage must produce an explicit failure, not
an unsupported answer. Database service HA, automatic failover, and cross-cluster
replication are not provided by the current RAG implementation.

## Implementation sequence

Keep the original step numbers. Supplemental steps 3a and 3b track deployment
work separately from corpus ingestion. The user subsequently approved the 3a
tools and explicitly authorized step 4 on 2026-10-19. The agreed execution order
is step 4, review, step 5, review, then real-cluster acceptance for 3a and shared
deployment for 3b. Every implementation stage still requires user review before
proceeding; tools approval is not deployment acceptance.

Step 4 subsequently passed isolated acceptance and user review on 2026-10-19.
The user first paused step 5, then explicitly requested a commit of the reviewed
work followed by implementation of step 5. Steps 3/3a/4 are now committed locally
as `1c43ec9`; step 5 subsequently passed user review and was committed locally
as `86ef3f6`. Nothing was pushed. The user then explicitly authorized step 6
and selected opt-in `include_internal: true`, preserving public-only defaults.
Step 6 is implemented, isolated-tested, and passed user review. The user
subsequently requested a local commit; it is now `3ab782d`, without a push.
The same request added supplemental step 6a below as planning only for
`agentic_benchmark_records` and simulation-task persistence improvements.
No business database, real cluster or planner was changed.
On 2026-10-20 the user explicitly deferred step 6a and requested step 7 onward.
Both commit and periodic repository triggers were selected, sharing one
reviewed target-local entry point. Step 7 is implemented with 309 passing
affected regression cases and no skips. The user approved step 7 and explicitly
requested the step-8 fixed baseline. It now passed 20 public and 12 internal
fixed cases plus 70 final affected regressions with no skips. The user approved
step 8 and initially paused further work. The user subsequently authorized the
recommended sequence: steps 7/8 were committed locally as `c5ee9a1`, without a
push. A dedicated persistent acceptance instance on the current machine was
selected, with explicit permission to import only the three pinned sources for
local acceptance, not external distribution or production licensing approval.
Live local-instance acceptance subsequently passed user review. At the user's
explicit request, its evidence-whitespace correction was committed locally as
`c6f4796`, without a push; no existing business
database, running Lens service, scheduler or cluster Job is changed.

### 0. Pin inputs and decide access boundaries

Record the Prism snapshot date and file hashes, the exact `llm-d` and `llm-d-benchmark` commits, the allowlisted paths/formats, and retention and citation requirements. Confirm the RAG feature permission and whether the existing ownership model can safely expose any Agentic records; benchmark IDs without a verifiable owner do not qualify. Assemble fixed questions for exact metrics, guide tables, definitions, chart gaps, changed/removed files, cross-source overlap, and authorized/unauthorized internal records. **Gate:** public source inventory and access decisions are documented, Git-ignored inputs stay out of Git, and the questions have known evidence and expected abstentions.

#### Step 0 acceptance record (2026-10-14)

The initial pinned inputs were the local Prism snapshot dated 2026-10-13, `llm-d` at commit `080c14d957755da4cc363745638619fc748558f1` (historical checkout origin `https://github.com/VincyZhang/llm-d`), and `llm-d-benchmark` at commit `6e081de16d2debc7b29436ad53f2c62e8d475f2e` (checkout origin `https://github.com/llm-d/llm-d-benchmark.git`). Both repository checkouts were clean when pinned. These are checkout identities, not a claim that every document or external URL linked from them is public; verify source publication rights before an importer exposes any item.

The current approved llm-d origin is `https://github.com/llm-d/llm-d`.
Repository refreshes without `--source` automatically fetch the reviewed full commit
from the official GitHub repository into the shared managed repository cache.
Explicit offline checkouts remain supported with origin and commit verification;
Prism snapshots still require a local source. The initial llm-d commit also exists
in the official repository. Its 139-file content inventory is unchanged, but the
origin-dependent inventory digest is now
`5cc884dcde2bf7986f86c7ed200a8293fd60623d1f7c2049b10daa6b24c46aee`.
Historical fork-origin records, citations and audit snapshots are not rewritten.
They require a new approved import under the official source identity.

The v1 allowlist is `llm-d/guides/**/*.md` (guide text, including the 20 result Markdown files), `llm-d/guides/**/benchmark-results/**/*.json` (five custom result files), and `llm-d/guides/**/benchmark-results/**/*.png` (37 figures, context only). In `llm-d-benchmark`, allow `docs/**/*.md` (27 files) and `benchmark-report/README.md` for definitions and report-format context, not measured data; consult `benchmark-report/llmd_benchmark_report/br_v0_2_json_schema.json` only to validate the v0.2 report format. No other JSON, images, guide configuration assets, or repository paths are implicitly approved. Inventory is relative to the commits above, not a permanent expected count. The five guide JSON files are under `guides/p2p-kv-cache-sharing/benchmark-results/data/glm-5.2-c64/`; their format is not the benchmark-report schema. Symlinks, submodules, and external links are not additional ingestion roots.

The local input root is `private/prism-public-2026-10-13/` in this repository, excluded by `.gitignore`'s `private/` rule. Only its two archive JSON files, two Results Store index JSON files, eight full-run JSON payloads, eight original ZIP exports, the normalization rule, and the merged JSON are eligible for this import. Its README records the source endpoints and exclusions. SHA-256 below is over the stored bytes, with paths relative to that root; the README is descriptive, not an import input.

```text
6b41f1175789429620f51e3be6c7545d94c16c4b6b25c2ddd286406159ff4fee  archives/archived_drive_data.json
1ae3cd3945059dfd7a4eaf15d99f680242a2e124eaaad2238fa1ff4abb731bd6  archives/llm-d-benchmarks.json
6ff00bf899c77f1b656160082dc406538d3781fb842bb6f6e2caf63426ef9e0e  historical-merged-deduplicated.json
aec36a726fc140924589271859985ad92d06cf623eade8305b72223589c860e6  normalize-and-merge.jq
36e96467c27235c84591d36f0011373736ec85395fdfc926759317683133f74e  results/promoted-index.json
b03f1b6351e8e270d44fdaf793fef36f2b99f97bec093bc1bd308832f2bdbea2  results/public-index.json
a9c6b0b610514e44670a9eb9287a4371675a262528902697e1f02921f850b07f  results/json/18865f26-7574-4828-9c44-b08b20d34be9.json
f76dd9055619b076859445bbcf5de304dd1d9dbd0d8600f2caf60f25b2d8c126  results/json/5450510e-7af5-484b-a06a-89af5eab5265.json
f31add5affb5c40547d29756bc34c7b7abc9dca8d735b0d35ad53149c6f14615  results/json/63a6adee-542c-4817-9c83-32c3fc007a63.json
edfd4e20db8e4ea383741e2191719dccf70cb7fe6b44dc14eaab181760048f52  results/json/a8f1b438-49ca-482b-bd3a-5303defd33a1.json
f7fa15f21aae70e8e96a66763915d91067965066fbfc914bfc1d62fb38d87d13  results/json/c2602342-6fc1-4b6b-ab1e-8e9a12364697.json
55ebe0791ea7d11e9b93ca1b9e2198b66ff037c6b5f5b3e4a11d7b5a74c56a65  results/json/cdf3e25a-0265-4414-98e8-dbb7dc594d55.json
f96ea3cd73662999979b2404375206b73d35e259e3f8a3f61f2871617397bbf6  results/json/ced8634e-33ff-463a-b539-425594891f2d.json
144f310c2c2cf1d48e5c07e0364c0e6cde8e3ab222f2026d27faab7838a894f6  results/json/e354540c-ed82-4442-8405-7e5b0327c581.json
ee8a97b5e0290849c681fbddbfd3066f8455483bb17e1d2785a9db59bc576b5d  results/zip/18865f26-7574-4828-9c44-b08b20d34be9.zip
175f72dca6542baa6434638fe34851a70be76bf956ec491fbb2aeff4d4386948  results/zip/5450510e-7af5-484b-a06a-89af5eab5265.zip
f1cf77adc94a3431db279c1c6f80c0b74004131a2e8378a9a8c8d4738f7da886  results/zip/63a6adee-542c-4817-9c83-32c3fc007a63.zip
eff0af959b4d3ebe17aeb32e344ff5d6f11bc445162c83c75c587910c49e13dd  results/zip/a8f1b438-49ca-482b-bd3a-5303defd33a1.zip
3091c4d0e9381fe587e14e5559f42e35edf78398baaf2e26797880723aeb174f  results/zip/c2602342-6fc1-4b6b-ab1e-8e9a12364697.zip
1c78aebde0dc5a41c659578a4d0739e789fd10e930d70bfdcbf49045576b89b0  results/zip/cdf3e25a-0265-4414-98e8-dbb7dc594d55.zip
2de148c8fff89fee1f23be4f5ec3cd3184ec1a83a5525e378b91f3c533c5d072  results/zip/ced8634e-33ff-463a-b539-425594891f2d.zip
e3468f6bfa6ed565e7ea4538a7fee0439986d8e0b5029399cfd89f0977959903  results/zip/e354540c-ed82-4442-8405-7e5b0327c581.zip
```

Access decision for v1: introduce a dedicated `benchmark-rag:query:read` permission before enabling any query endpoint; require an authenticated principal with that permission for the public branch, and do not grant it implicitly through `evaluate:run:read`, `candidate:candidate:search`, or anonymous access. Its role assignments and endpoint enforcement belong to step 5. Restrict public index items to verified public sources; permission revocation must take effect at query time. The current permission catalog has no RAG permission. The Agentic branch remains disabled until step 6: `BenchmarkRecordDao.list()` is unscoped, its `evaluate_run_id` is a nullable soft reference, and `evaluate_runs` ownership can be null or its row removed. Step 6 must check a live, owner-bearing evaluation against the authenticated principal and `evaluate:run:read` before any hit, count, or snippet is returned. A missing principal, owner, or live context fails closed for this branch; the existing `resource_readable(None, ...)` shortcut is not suitable for it. No agentic record is copied into the public index.

Citation policy: carry repository identity, commit, relative path, file hash, and heading/table cell or JSON path for every answer; carry the snapshot date and original run/report path for Prism. Verify a pinned file's bytes before dereferencing it. Keep retired items and their pinned source bytes for at least 365 days after removal and for as long as any retained citation or dependent item needs them, whichever is longer. Any later deletion needs a dependency check and a reviewed retention decision; never overwrite historical source bytes with a refreshed version. Local snapshot ZIPs and JSON stay Git-ignored and are not published by this document.

Fixed acceptance questions (answers below are expected evidence, not generated outputs):

| Question | Expected evidence or abstention |
| --- | --- |
| What is the recorded throughput for Prism ID `02c8f038-1cad-4ee8-a72e-724affa923f4`? | `historical-merged-deduplicated.json`, row with that ID, `metrics.throughput`: `6131.06177725188` output tokens/s; also give model `Qwen3-0.6B` and source snapshot. Do not treat `metrics.output_tput` as a second observation. |
| What was the P2P weight-load time versus the default loader for the H200 gpt-oss-120b guide observation? | `guides/modelexpress-p2p/benchmark-results/h200-ib-gpt-oss-120b.md`, Weight loading table: `0.93 s` versus `30.3 s`, with the report's cross-node/warm-NFS conditions and caveats. |
| What is a benchmark report intended to capture? | `llm-d-benchmark/docs/benchmark_report.md`: aggregate performance statistics for a workload/environment, not individual request traces; no example schema value is a measurement. |
| Can the exact unreported points in the Qwen3 Coder TPU latency-versus-throughput PNG be listed? | `llm-d/guides/agentic-serving/qwen3-coder-480b-tpu.md` links `benchmark-results/latency_vs_throughput.png`; abstain from exact numeric points without published underlying data, cite the guide and figure, and state the figure's reviewed coverage. |
| Does a newly added or removed guide report appear before a complete, pinned refresh? | No; search retains the old published manifest until a validated new inventory is atomically published. A partial listing cannot prove deletion. |
| Are similar model/hardware rows from Prism and guides the same experiment? | Do not merge without verified run/stage or source identity; no distinct-experiment count across uncertain overlaps. |
| Can an authenticated user with `benchmark-rag:query:read` see a benchmark belonging to an accessible evaluation? | Only after step 6 proves the live evaluation has an owner, grants that caller `evaluate:run:read`, and links the benchmark ID to it; until then abstain from internal records. |
| Can another user, an unauthenticated caller, or a record with missing/deleted/unverifiable owner appear in results or counts? | No; omit the record, its identity, any overlap signal, and its count. Without RAG feature permission, deny the entire query. |
| Do REST and MCP calls from the same principal and filters return the same evidence? | Yes; compare citations, origin labels, abstentions, and authorized counts. Deny both without `benchmark-rag:query:read`; MCP invocation must also meet its own tool policy. Neither transport may expose the Agentic branch before step 6 passes. |

### 1. Create the single-table contract and migration

Add the `rag_items` SQLAlchemy model and DAO with a reviewed Alembic migration against PostgreSQL. Specify the six item kinds, stable source-scoped keys, pinned revision/hash, typed numeric fields, self-links, publication state, source manifest membership, constraints, B-tree filters, and `tsvector`/GIN passage search. Define unit/statistic mappings and a public-source-only access classification; retain original files separately. **Gate:** migration and schema/DAO tests pass on PostgreSQL, a same-version import is idempotent, unknown fields/units are not fabricated, and neither model nor importer writes to `agentic_benchmark_records`.

### 2. Implement publication before the first import

Build the allowlisted source registry, complete-inventory validation, staging, manifest of versioned item IDs, source-pointer switch, tombstones, and rollback. Serialize competing imports for one origin. Filter queries through the published manifest before full-text ranking or counts; unchanged items may be referenced from their old pinned file version by the new manifest. A missing file in an incomplete listing must not retire it. **Gate:** failure before or during activation preserves the old searchable release, concurrent refreshes do not mix releases, deleted items leave current search but retain verifiable historical citations, and unchanged refreshes produce no duplicate items.

### 3. Import the initial Prism snapshot

Use the existing normalized archive adapter for 249 rows (177 historical and 72 stage reports), with original JSON/report paths and hashes. Expand known metric leaves without duplicating aliases; link the four run-level summaries as context, not extra experiments. Preserve submission timestamp kind, null concurrency, measured versus target rate, and documented time/throughput units. **Gate:** the initial published manifest resolves exactly 249 source-scoped experiment rows, a second import changes no counts, and sampled values trace to their original JSON paths; future refreshes do not hard-code 249 as a permanent total.

### 3a. Complete cross-cluster RAG portability and acceptance

**Tooling checkpoint:** target-local commands and isolated acceptance were
implemented in `1c43ec9` and passed user review on 2026-10-19; the branch is now
pushed through `9bb27e5`. Latest verification and remaining deployment gates
are recorded in the current implementation status above. The initial CLI round trip
imports 249 experiments, 9,060 measurements, four context passages, and 22 source
files; the initial full-table bundle contains 9,337 rows including registry and
manifest rows. Repeat restore inserts no rows and changes no source state.

**Real-target stage: started, paused before schema migration.** Steps 4/5 and
local acceptance are complete. The last recorded target attempt stopped on
non-RAG schema drift; it has not been resumed or remotely rechecked in this
update. After explicit schema review, complete source/target prerequisites,
code/schema compatibility,
secrets, persistent directory ownership, secure bundle transfer, verification,
and recovery/cutover procedures. Exercise both source-snapshot rebuild and
database-bundle restoration on two real clusters, including a nonempty target.
For a migration intended as a final cutover, coordinate source writes or take
a final consistent backup; an earlier snapshot is not continuous replication.
Preserve the original deployment for an explicitly approved rollback window.

**Gate:** compare per-kind counts, exact numeric samples, publication/history,
source enablement, original-byte hashes, and historical citation resolution
between source and target; verify repeat restore, conflict/fault rollback, and
target operation after the source is unavailable. Do not count any other Lens
table as migrated. Step 5 now exists and the local step-8 baseline passes;
repeat user-facing REST/MCP answer comparison on the actual target.

### 3b. Implement single-cluster shared RAG deployment

**Current checkpoint:** planned only; no RAG-specific shared deployment or real
multi-Pod acceptance has been delivered.

1. Review the database/schema, credential roles, connection limits, storage
   backend, namespace access, and publication/isolation boundaries before
   changing deployment configuration or storage interfaces.
2. Wire replicas to the same database/schema and evidence archive using the
   repository's deployment/secret conventions. Provide controlled administrative
   jobs, serialize schema migrations, and keep raw inputs out of Git.
3. Test at least two replicas: a write through the designated management path
   becomes visible to both after commit; a concurrent reader sees no mixed
   release; stale activations fail; historical bytes survive publication,
   rollback, and Pod replacement. Verify evidence access after scheduling on
   another node, and explicit errors on inaccessible or corrupt evidence.
4. Once step 5 is available, repeat identical authenticated queries against both
   replicas and verify citations, authorized counts, revocation, and REST/MCP
   parity. Include the separate Agentic branch only after step 6 passes.

**Gate:** reviewed deployment configuration and operational instructions,
verified storage semantics and least-privilege access, and passing real
multi-instance tests. Infrastructure acceptance may precede step 5; do not
claim complete shared RAG service delivery until its retrieval/permission gate
also passes. Implement this deployment after the separately reviewed steps 4/5.

### 4. Import guide results and benchmark documentation

Parse guide headings, notes, configuration, tables, captions, and links; use separate adapters for the five custom JSON files. Attach table-cell measurements only to a verified scenario, not merely a matching model. Inventory the 37 figures and record their `complete`/`partial`/`summary_only`/`unknown` coverage without pixel-derived numeric values. Index `llm-d-benchmark` docs by versioned section, marking report examples as examples rather than measurements. **Gate:** counts match the pinned inventories, table/JSON values round-trip to source locations, GLM data cannot join P2P GPT-OSS figures, and definition queries never return example data as real measurements.

### 5. Deliver public-only retrieval and answer checks

Add typed numeric filters, PostgreSQL passage search, source-version/permission filtering, citations, and evidence-type-aware responses over published `rag_items`. Verify source hashes before dereferencing pinned originals. Abstain for missing comparisons or unreported plot points; keep a deterministic evidence response when the language model is unavailable. Deliver a REST query endpoint and a read-only MCP query tool backed by this single caller-scoped service, with a shared question/filter input and evidence/answer output contract. Add `benchmark-rag:query:read` to the permission catalog and require it for both; do not infer RAG access from `mcp:tool:invoke` alone. **Gate:** the public question set returns only matching experiments and source-pinned citations, excludes inactive items, and never converts prose or images into measurements; REST and MCP agree for the same principal and filters, deny unauthorized callers, and remain public-only. This usable public path does not wait for Agentic authorization work.

### 6. Gate and add the Agentic retrieval branch

First establish a caller-scoped authorization query that ties `agentic_benchmark_records` to an accessible owning context. Its current DAO `list()` is unscoped and `evaluate_run_id` is a soft reference, so neither is sufficient for a public-facing search; omit records with missing/deleted/unverifiable ownership. Only after this boundary is tested, add the separate read-only Agentic branch to the shared query service and merge authorized result envelopes on verified run/stage and metric identity. Do not copy public items into Agentic or change planning rankers. **Gate:** REST and MCP return the same caller-authorized evidence; different principals receive only their own allowed records, unauthorized matches do not leak through counts/snippets/overlap status, uncertain overlaps remain separate, and verified duplicates count once with both citations. If this gate fails, ship the public-only path through both interfaces and leave the Agentic branch disabled.

**Implementation checkpoint (review approved, further work paused):** the same REST/MCP contract now
accepts `include_internal: true`; its default remains false and performs no
internal lookup. A separate read-only DAO joins canonical `evaluate:<UUID>`
records to live, verifiable evaluation owners and applies the existing
`evaluate:run:read` action/scope/ownership rules before counts or pagination.
Stale principals and concurrent owner/access changes conflict rather than emit
stale evidence. Ownerless, deleted, malformed or conflicting contexts are omitted,
including for admins. Legacy planning history currently uses simulation task IDs
inside `evaluate:<id>`; those rows remain ineligible, with no guessed backfill or
planner changes.

Internal metrics are labeled normalized stored-Float observations, not original
exact report tokens. Verified merging requires the canonical evaluation ID in
the pinned original public JSON plus compatible metric, statistic, unit, value
and reported conditions. The merged displayed envelope keeps both provenances;
uncertain overlaps stay separate. `matched_items` counts authorized candidate
public items/internal records before expansion or merging, not independent runs.
`returned_observations` counts only displayed envelopes after verified merging,
within the shared overall limit. No full-corpus distinct-experiment claim is
made. Tests cover caller isolation, missing owners, revocation, hidden overlap
nonleakage, verified merges, read-only behavior and unchanged public questions.

### 6a. Improve Agentic benchmark and simulation ingestion (planned only)

**Execution order update (2026-10-20):** explicitly skipped by the user in
favor of steps 7/8. Preserve this plan for separate future approval; it is not
a prerequisite for public-source refresh/evaluation and does not make legacy
simulation-backed records eligible for Evaluate-only retrieval.

**Status:** requested after the step-6 commit; no implementation or migration
is authorized by this planning update. Keep steps 7/8 unchanged. Each substep
below requires review before proceeding. This work is a prerequisite for
claiming newly ingested simulation-derived benchmark history is safely available to
internal RAG; it does not block the existing public-only query path.

**Confirmed scope:** the user corrected the intended table name to
`agentic_benchmark_records`. Improve its ingestion together with the
`simulation_tasks` lifecycle and their provenance/ownership relationship.
Evaluate remains the distinct existing owning context where applicable.
Deployment batch/job/execution persistence and new Agentic planning/session
storage are not additional workstreams in this plan.

- **6a.1 Inventory and review the write/ownership contract.** Trace the actual
  benchmark producers, Evaluate and simulation create/save/complete/
  retry/delete paths. Record authoritative benchmark/run/task/stage IDs,
  caller/owner/cluster origin, DTO-to-row projections and artifact references.
  Existing owner columns alone do not prove the creator is written and
  round-trips. Review explicit source kinds and relationship cardinalities;
  distinguish lifecycle ownership from measured-evidence provenance.
  **Gate:** reviewed mapping and compatibility/migration proposal, including
  permissions, field authority, unknown values and deletion/retention semantics.
- **6a.2 Make source lifecycle persistence complete.** Carry the actual
  creating principal's owner and verified cluster/context through services,
  DTOs, DAOs, reloads and reruns; reject conflicting client-provided identity.
  Review propagation from the actual evaluation/simulation source to its
  benchmark evidence without guessing ownership from the endpoint. Keep
  reported model/runtime/hardware/workload/configuration separate from
  inferred/defaulted metadata. Define stable idempotency and terminal-state
  rules for completion/retry/concurrent saves/restarts. Simulation per-request
  results and extra metadata are file-backed: specify durable references,
  checksums, atomic publication/recovery, explicit partial/missing states,
  backup/restore and dependency-safe retention.
  **Gate:** end-to-end create/reload/complete/rerun/restart tests preserve
  ownership, conditions, result completeness and raw-evidence references;
  failed writes cannot look like successfully indexed complete observations.
- **6a.3 Publish normalized evidence from verified source completion.**
  Replace the ambiguity of `evaluate:<simulation-task-id>` with an explicitly
  reviewed source-kind/source-ID/run/stage/owner-context contract. Do not
  repurpose the existing Evaluate soft reference for simulations. Design a
  versioned adapter and, if required, an additive reviewed migration. Preserve
  raw references, metric/statistic/unit/denominator, precision and extraction
  version; unknowns must not become fabricated measured facts. Assess moving
  evidence production from the current planning-facts read path to a durable
  completion event or retryable ingestion job, with idempotency keys and visible
  failures. Actual provenance must define the identity, not an ID prefix.
  **Gate:** completed source replay and concurrent/retried ingestion produce
  one correct source-scoped observation; partial/failed/ambiguous inputs are
  explicitly withheld or separately labeled. Planner behavior changes require
  their own regression and review, not incidental changes from this RAG work.
- **6a.4 Migrate history and enable source-specific authorized reads.**
  Provide a dry-run inventory of verifiable, conflicting, orphaned and
  unresolvable legacy rows. Repair only with retained source evidence, retain
  audit/mapping history and repeat-safe rollback/recovery; never rename an
  eight-character simulation ID into a fake Evaluate UUID. If simulation-derived
  benchmark evidence is to become queryable, first verify its actual owning
  resource's existing permission and cluster/owner rules. Keep the current
  Evaluate-only RAG gate unchanged until separate source-specific SQL
  authorization, citation and merge regressions pass. Do not use unscoped
  planner history listing as a RAG access check.
  **Gate:** cross-owner/cluster isolation, owner deletion, revocation, omitted
  and explicit opt-in, hidden counts/snippets/overlaps, REST/MCP parity, source
  identity deduplication, backup/restore and planner non-regression all pass.
  Unknown legacy relationships remain excluded with an operator-visible audit
  report, not silently assigned to a user or declared successfully migrated.

### 7. Exercise refresh scenarios and enable triggers

Test addition, deletion, rename, and content/heading/table edits in each allowlisted repository; test a new Prism snapshot, parser errors, truncated API pagination, concurrent runs, changed access rights, and atomic rollback. Then enable manual Prism refresh and reviewed commit-triggered or periodic repository refreshes with import status and count reporting. **Gate:** new data appear with pinned citations, removed data disappear only after a complete inventory, old citations still resolve, failed updates preserve the prior release, and access revocation takes effect before the next answer.

**Implementation checkpoint (2026-10-20, review approved):** `admin refresh`
shares one strict `RefreshPlan`/`RagRefreshService` across manual Prism and
manual/commit/periodic repository jobs. Plans bind existing source approval,
complete inventories/hashes, extraction counts, base generation/manifest,
triggers and figure reviews. Commit events must match the pinned full SHA;
periodic jobs only replay the approved version. Empty repository releases need
explicit approval. No remote fetching, branch tracking or automatic source
approval is performed.

Numeric guide updates require an explicit exact-revision metadata compatibility
review; defaults remain initial-revision-only. The CLI binds that review to
the inventory digest before parsing, with all schema/layout guards retained.
Reviewed jobs cannot silently carry old model/hardware/workload assumptions
into changed reports. Publication and archived citations reuse the existing
atomic/CAS protocol. Direct replay is a no-op; stale plans cannot undo a newer
release, rollback or access-policy ABA. Failures leave the active head intact,
although unsearchable staged rows/blobs can remain.

`status` reports durable source head/history; explicit `rollback` checks the
current generation and previously published target. Refresh success reports
contain hashes, review, counts, times and publication identity; failed jobs exit
nonzero with a failed JSON report on stderr. External trusted CI/cron/managed
Jobs must persist stdout/stderr for attempt history; no durable job queue or
real scheduler is installed. REST/MCP remain read-only. Both locale operations
docs and isolated Git/PostgreSQL lifecycle/number-update/Prism/race/query
regressions are included. Step 6a, real deployment and new source onboarding
remain outside this implementation.

**Validation:** 309 affected import/publication/transfer/query/route/refresh
tests passed (10 pre-existing deprecation warnings, no skips; 284.85 seconds),
including 27 new refresh cases and the provisioned pinned public corpora.
Ruff/format, English checks, documentation links and both locale builds passed.
The existing `resources/OWNERS` link warning remains. IDE problems reported no
errors, but Pylance still reports an environment-resolution
`reportMissingImports` for pydantic; no complete Python typecheck is claimed.
Disposable PostgreSQL schemas/instances were cleaned up; pytest may retain
temporary evidence. Step 7 passed user review; steps 7/8 were subsequently
committed locally as `c5ee9a1`, without a push.

### 8. Evaluate and onboard additional sources separately

Replay a fixed answer set across the three public sources and, if step 6 passed, authorized Agentic records; inspect unsupported numbers, false joins, citation failures, and access leaks. Only after the baseline passes, onboard another repository/dataset, GCS/GIQ Prism source, or paper collection through its own source approval, permission/license check, adapter, manifest, and regression fixtures. **Gate:** every published number traces to permitted original evidence, papers remain explanatory unless independently validated, withdrawn sources stop appearing in current search, and no new origin becomes searchable merely because its URL was discovered.

### Step-8 implementation checkpoint (review approved, committed locally)

A strict versioned evaluation runner replays fixed case inputs and expected
Decimal/status/refusal/count/permission outcomes; suite hashes and source pins
identify the accepted baseline. A mandatory independent verifier checks every
primary/corroborating citation against retained bytes or authorized owning
records. Deterministic numeric answer lines must agree with evidence. Errors
cannot be converted to successful empty answers; all cases must pass, and
missing real-source prerequisites cannot count as successful acceptance.

The isolated internal baseline passed 12/12 cases (six numeric envelopes and
seven verified citations), including separate live owners, explicit opt-in,
denial/revocation freshness, hidden/missing IDs, verified dual provenance and
legacy/orphan exclusions. It made no data writes or planner-record changes.
The actual three-public-corpus baseline passed 20/20 fixed cases covering all
nine categories, with 27 independently verified citations and three numeric
envelopes. Imported heads match the pinned revisions, both Git inventory
digests and all Prism file hashes. Provenance mutation controls reject
fabricated measurements, citations and contexts. This is actual pinned-input
acceptance, not a substitute synthetic corpus.

The final baseline/evaluator/authorization regression passed 70 tests with
three existing deprecation warnings and zero skips (136.96 seconds).
Evaluation self-tests also preserve unset-filter planning semantics and bind
that distinction into suite hashes. Ruff/format, English/whitespace/docs links
and bilingual builds passed; the existing OWNERS link warning and Pylance
pydantic-resolution limitation remain documented. Reports are persisted in the
session's `files/rag-v1-evaluation/`, not committed. Both steps passed user review
and were subsequently committed locally as `c5ee9a1`, without a push. The user
selected a dedicated persistent instance on the current machine and approved
only the three existing pinned sources for local acceptance. Live results are
recorded below. Step 6a, additional origins, existing business
services, real clusters and pushes remain outside the current operation.

Optional reports contain source pins, suite hashes, times, per-case outcomes and
issue codes, never raw private responses or credentials. Additional origins,
GCS/GIQ datasets and papers still require separate permission/license review,
exact inventories, adapters, source fixtures, manifest publication and
withdrawal/rollback regression. No new origin or automatic URL discovery was
enabled. This acceptance does not implement deferred step 6a or real deployment.

### Persistent local-instance acceptance (2026-10-20, review approved)

- Dedicated password-protected PostgreSQL database `lens_rag_acceptance`, with
  its cluster and immutable archive under
  `/home/wenxin/.local/share/llm-d-lens-rag-acceptance/`. Node REST/MCP listens
  only on `127.0.0.1:13000`; backend only on `127.0.0.1:18081`. Existing Lens
  services, business databases and remote machines were not restarted or used.
- Configuration, cache and diagnostic roots use the matching
  `llm-d-lens-rag-acceptance` directories under `.config`, `.cache` and
  `.local/state`. Private roots are mode 0700; credentials/keys/handoff files
  are mode 0600, not committed or printed. `operator-login.json` in the config
  root hands off the real administrator account after initial password change.
  Session `files/rag-local-acceptance/` operations reuse the backend launcher and
  normal Node workspace start command without loading existing service secrets.
- The actual authenticated Node REST fixed suite passed **20/20** cases,
  **27 independently verified citations**, **three numeric envelopes**, both
  before and after owned-service restarts. Seven supplemental live checks passed,
  including real admin/reader access, immediate grant revocation, anonymous and
  invoke-only denial, CSRF, signed MCP/REST equality, explicit REST503/MCP error
  for unavailable evidence and recovery after restoring the unchanged bytes.
- Six live administrative checks passed: unapproved-origin rejection,
  periodic/commit idempotence, failed event-revision/count preservation, actual
  upstream parent-doc commit publication and query followed by CAS rollback to
  the initially approved revision, retained old bytes, stale-plan rejection,
  verified RAG-only backup and separate-schema/separate-archive restore.
  Restore inserted **14,961 rows**, then **zero** on replay with zero source
  updates. Those include history/manifests, not independent experiments.
- PostgreSQL system identity, data directory, application/restored row counts,
  source heads/generations and real users survived a full owned
  PostgreSQL/backend/Node restart. Invalid database passwords were actually
  rejected. Processes remain detached and running; no boot service, scheduler,
  cluster Job or automatic source discovery was installed.
- Reports and the RAG-only bundle are under
  `/home/wenxin/.local/state/llm-d-lens-rag-acceptance/`. This is not a full
  application/user database backup or actual cross-cluster migration.
  `agentic_benchmark_records` and `simulation_tasks` remain empty; internal
  fixture acceptance is not real historical-ingestion acceptance.
- Independent HTTP verification initially exposed recursive response whitespace
  stripping: `" 30.3 s "` became `"30.3 s"` while numeric offsets stayed `1:5`.
  Responses now preserve original strings and JSON keys; request trimming stays
  unchanged. Added tests failed before the fix, then passed; the public verifier
  now rejects trimmed-cell/unchanged-offset and trimmed-context mutations.
  The affected contract, routes/evaluator, SQL query, public/internal baseline
  and internal service selections passed **112 tests, no skips**. Ruff/format,
  English/whitespace/docs-link checks passed. The user approved this stage and
  explicitly requested a local commit without a push; this correction and
  published EN/ZH docs are committed as `c6f4796`. Local plans remain ignored.
- Direct full-history `alembic upgrade head` on an empty database failed in
  `9c1f0a7b4d22` because the no-op baseline does not create `clusters`.
  After confirming no application tables remained, the run reused the
  repository's standard fresh-install Step 0 `apply_database_setup`, which
  creates current metadata. No migration was changed or stamped to disguise
  that limitation. Complete historical replay and production/shared/remote
  deployment are not certified.
- This stage passed review and its correction was committed without a push.
  At that review the remote target was not yet selected. The subsequently
  authorized target and current schema-review stop are recorded below.

### Real target migration checkpoint (2026-10-20, awaiting schema review)

- The user authorized direct operations on `wenxin@10.112.229.74`, repository
  `/mnt/disk1/wenxin/llm-d-lens`, retaining the existing source-process deployment
  rather than converting it into Kubernetes Pods. Preserve `fix_gpu_runtimeclass`;
  the original frontend work had been committed externally by backup time,
  advancing HEAD to `369ac0ab27ca0bc647e7b8d9a556ae50f73b5fec`.
  No branch switch, reset, stash, commit or push was performed.
- A full private database safety backup, original code/runtime snapshot and
  original account/credential preservation checks are on the target under
  `/home/wenxin/.local/share/lens/backups/rag-3a-20261020`. Private backups
  remain on that machine. The separately transferred, verified RAG-only bundle
  has 14,961 retained rows and 190 blobs; its 22 exact Prism inputs are also
  present. It does not copy source accounts, credentials or business tables.
- Compatible RAG patches were checked together and applied while retaining
  the target's newer integration behavior. The source migration
  `7a12c8e49b60` was deliberately excluded. Per the approved plan, native
  autogeneration started from the target revision `d9e3f7b1c5a8`, with a guard
  permitting only the new RAG table and its indexes.
- **Blocked before migration:** autogeneration also detected existing
  `audit_logs.cluster_id` as `VARCHAR(8)`, whereas the current model specifies
  `VARCHAR(32)`. The non-RAG guard stopped immediately, before writing a
  migration file or upgrading the database. The difference was not silently
  filtered out and the business column was not altered. User review must
  decide whether to explicitly permit a RAG-only migration retaining that
  known drift, or handle the unrelated business-schema drift separately first.
- Post-stop read-only verification passed: revision remains `d9e3f7b1c5a8`,
  `rag_items` is absent, users remain 1 and clusters 2, and Agentic
  benchmark/simulation/Evaluate counts remain zero. Original account hashes,
  status and credential checksums are unchanged. All three original processes
  remain running without restart; the Node HTTPS and backend HTTP
  auth/providers checks both returned 200. Branch/HEAD are preserved and
  exact reverse checks for our RAG patches pass.
- No target rebuild/restore, validation account, restart, HTTP/MCP baseline or
  source-disconnection acceptance has run. **Step 3a is not complete.**
  Finish and review 3a before implementing 3b. The selected 3b scope is
  same-host source processes sharing PostgreSQL and RAG evidence, not a claim
  of multi-Pod, cross-node or arbitrary RWX-filesystem acceptance.
  Deferred 6a, additional origins, business-schema changes, production
  licensing/distribution and pushes require separate approval.
- Local EN/ZH plans remain ignored. No SSH password or private runtime
  configuration is written into repository files, operator scripts or plans.

### Review remediation execution record (2026-10-20)

The user authorized fixing the two P1 and three P2 review findings. The
[execution record](../benchmark-rag-review-2026-10-20.md) records the changes,
regressions, validation commands and operational limits outside this ignored
design directory, so the record can be versioned with the implementation.
No business database refresh, deployment, schema change, deferred step 6a,
commit or push is part of this remediation.

## Explicit exclusions for v1

- No numeric extraction from PNGs or reconstruction of unreported plot series.
- No semantic deduplication across different source IDs without source-level identity evidence.
- No ingestion of additional Prism GCS/GIQ sources or private/user benchmarks.
- No writes to Agentic Deploy planning records or changes to its ranking contracts; the read-only Agentic query must enforce the caller's existing authorization, never bypass it.
- No claim that archive rows, stage reports, and guide results are directly comparable without checking experimental conditions.