# Multi-source configuration recommendation RAG: implementation plan and acceptance criteria

- Date: 2026-10-26.
- Code baseline: `174f2ec`.
- Status: implementation proposal, not implemented. Proposed interfaces, configuration,
  files, and thresholds describe future work, not existing capabilities.
- Repository: [llm-d-lens](../).
- Scope: preserve existing evidence and publication mechanisms, ingest public online
  documentation and blogs, improve configuration-case retrieval and recommendations,
  and subsequently introduce vector retrieval.
- Deliverable: this plan only. It does not change application code, databases,
  deployment configuration, or running services.

## 1. Objectives, boundaries, and success

Example target request:

> Use actual hardware, model, runtime, and user preferences to find relevant
> configurations in internal experiments, public benchmarks, online documentation,
> and blogs. Recommend feasible options with evidence, differences, and validation needs.

Success means more than storing articles or returning vector matches:

1. Online content can be ingested, refreshed, and revoked under controlled policies,
   retaining verifiable versions.
2. Hard constraints, soft preferences, units, and statistical meanings are parsed correctly.
3. Retrieved evidence actually participates in both deployment creation and refinement.
4. Performance on similar hardware, metrics from different experiments, and blog
   suggestions are not presented as measurements of the current deployment.
5. No matches, insufficient permissions, synchronization failures, and unavailable
   indexes are explicit states.

The initial scope includes public pages authorized for collection, Markdown/API text,
and existing internal experiment records. It excludes unrestricted web discovery,
authenticated private documents, arbitrary browser-rendered pages, and reconstruction
of exact metrics from images.

Existing internal experiments remain private; every user-facing internal retrieval
path must enforce resource authorization. Implementation must not automatically
commit code, deploy services, enable external models, or establish new data egress.
The separately authorized publication of this plan does not authorize those actions.

## 2. Verified current state

| Component | Existing capability | Gap |
| --- | --- | --- |
| `rag_items` | Source, experiment, measurement, passage, and figure evidence; versions, hashes, publication relationships | Not a complete RAG system; public-only classification cannot directly accommodate private documents |
| SQL indexes | Model, hardware, experiment, metric, and other structured fields | Hardware labels still mix device identity and device count |
| Full-text retrieval | PostgreSQL `TSVECTOR`, GIN, and full-text query operations | Not embedding-based semantic retrieval; multilingual recall needs evaluation |
| Public RAG | Fixed public sources, pinned local inputs, deterministic answers and citations | No general web ingestion or online scheduler |
| Publication and refresh | Archives, complete inventories, staging, manifests, generations, and reuse of unchanged files | Periodic trigger arguments do not constitute an online scheduler; refresh currently requires a reviewed plan |
| Agentic public evidence | Numeric observations for exact model/hardware, ranked by explicit workload and latency | General passages are not an independent configuration-retrieval branch; context grows with observation count |
| Internal benchmarks | Similarity, time decay, candidate seeds, and performance evidence | Query descriptors contain synthetic hardware, architecture, and workload defaults that need real facts or explicit unknowns |
| OpenSearch | Diagnostic adapter supports lexical queries and explicit query vectors | No complete embedding/index/query pipeline for public configuration recommendations |

Reproduced input issues:

- `TTFT < 2 s` is interpreted as `2 ms`.
- `ISL 4096` populates both mean and P95; it does not establish that the user supplied
  both statistics.
- A Chinese-language request meaning "prioritize low latency, concurrency 32" does
  not yield a structured concurrency constraint. A later model may still understand
  the original text.
- `H100`, `16x H100`, and `H100 80 GB` produce different token sequences; string
  equality alone is insufficient for device identity matching.

Primary implementation references:

- [Evidence model](../apps/backend/src/llm_d_lens/db/models/rag_item.py).
- [Public retrieval](../apps/backend/src/llm_d_lens/db/dao/rag_query.py).
- [Sources and archives](../apps/backend/src/llm_d_lens/benchmark_rag/sources.py).
- [Publication](../apps/backend/src/llm_d_lens/benchmark_rag/publication.py).
- [Planning evidence](../apps/backend/src/llm_d_lens/agentic/facts.py).
- [Preference parsing](../apps/backend/src/llm_d_lens/agentic/generator.py).

## 3. Target architecture and invariants

```text
Approved public sources
    -> collection jobs
    -> raw snapshots and normalized text
    -> validation, chunking, configuration extraction
    -> rag_items and active manifest
         |                         |
      SQL/full-text          vector projection
         +------------+------------+
                      |
Authorized internal experiments -> structured retrieval
                      |
Actual hardware + structured requirements
    -> multi-source case retrieval
    -> version/permission checks, applicability, deduplication, ranking
    -> bounded evidence packages and candidate seeds
    -> existing candidate generation, validation, selection, and approval
```

Invariants:

- Keep `rag_items` and archives as the evidence foundation. Search indexes are
  rebuildable projections, not the only source of truth.
- Preserve exact numeric query semantics. Similar-configuration retrieval is a new
  planning capability, not an implicit relaxation of the existing API.
- Historical cases, AIC, and LLMs cannot override live resource facts or deterministic
  constraint validation.
- Public accessibility, source completeness, extraction correctness, and applicability
  to a configuration are separate judgments.
- Web content, LLMs, and parser output cannot expand approved source scope or publication
  permissions.
- Unknown is neither zero nor a match, and is not automatically incompatible.
  Display missing fields and their implications.
- Known-incompatible configurations cannot become deployable candidates. Incomplete
  cases can only supply explicitly qualified adaptation suggestions.

## 4. Implementation design

### 4.1 Requirements and hardware identity

Add a versioned, strongly typed requirements contract to existing planning facts:

```text
PlanningRequirements
  hardware: profile_id, device_model, memory_gib, form_factor,
            device_count, interconnect
  model: model_id, revision, architecture, quantization
  runtime: backend, backend_version, driver_version, container_digest
  workload: explicit ISL/OSL/concurrency/QPS with distinct statistics
  hard_constraints: field, operator, value, unit
  performance_targets: metric, statistic, operator, value, unit
  soft_preferences: ordered preferences and weights only when supplied
  provenance: per-field form input, source-text location, verified discovery,
              or inference
```

Implementation requirements:

1. Obtain identities, aliases, and capabilities through hardware profiles/providers;
   do not add vendor branches in consumers.
2. Separate count from model. Normalize equivalent spellings such as `H100 80 GB`
   and `H100 80GB`, but do not collapse PCIe/SXM variants.
3. A historical record containing only `H100` still lacks variant information.
   Do not fill it with the target device's memory or form factor.
4. Deterministically parse common Chinese and English numeric requirements, convert
   seconds to milliseconds, and preserve the difference between `<` and `<=`.
5. Do not invent missing P95, QPS, architecture, quantization, or runtime versions.
6. Existing LLM integration may assist with complex text, but its output must pass
   type, unit, and conflict validation.
7. Conflicting form and text values produce field-level errors or confirmation
   requirements, rather than silently choosing one.
8. Creation, refinement, historical retrieval, and candidate validation consume the
   same normalized requirements.

### 4.2 Source registration and synchronization state

Introduce a synchronization-state model referencing the existing registry record.
Do not duplicate approval information or the publication pointer as a second authority.
Initially, treat each explicitly registered page as one synchronization source.
Site discovery is a later bulk-registration capability, not automatic authorization.

Proposed state fields:

| Group | Fields |
| --- | --- |
| Identity | `registry_item_id`, `connector_type`, `canonical_url`, `allowed_hosts` |
| Schedule | `sync_enabled`, `interval_seconds`, `next_check_at` |
| Checks | `last_checked_at`, `last_success_at`, `last_published_at` |
| HTTP cache | `fetched_etag`, `fetched_last_modified`, corresponding recoverable fetch snapshot |
| Content | `raw_sha256`, `normalized_sha256`, `parser_version` |
| Concurrency | `lease_owner`, `lease_expires_at`, `fencing_token` |
| Result | `last_job_id`, `last_status`, `last_error_code`, `failure_count` |

Reuse an appropriate existing durable job mechanism for execution history. If none
fits, add minimal run records containing start/end times, status, change counts,
versions, and sanitized errors.

Distinguish `queued`, `running`, `unchanged`, `published`, `needs_review`, and `failed`.
Revalidate registry enablement, approved scope, and generation before execution and
again before publication.

Proposed defaults: check each source every six hours; scan due work every five minutes.
Allow per-source configuration in the management UI. Use exponential backoff with jitter
and at most three attempts per logical job before the next cycle or a manual retry.
These are proposed settings, not existing production freshness guarantees.

### 4.3 Fetching, normalization, and chunking

Initial connectors support public static HTML and approved Markdown/text endpoints.
Do not enable browser rendering initially. Return `unsupported_content` when extraction
fails rather than publishing empty content.

Processing sequence:

1. Validate URLs, approved scope, collection policy, and response types. Permit only
   explicitly approved HTTP(S) targets.
2. Revalidate every redirect. Reject loopback, link-local, metadata-service, and
   unapproved private-network addresses; enforce matching egress controls.
3. Set explicit timeouts, redirect limits, response/decompressed size limits, and
   per-source rate limits.
4. Use ETag/Last-Modified conditional requests. Archive raw bytes and response metadata,
   but not credentials.
5. Extract main content while preserving headings, code, tables, links, and qualifications;
   remove navigation and advertising.
6. Archive and hash raw content and normalized text separately, recording transformation
   version and provenance.
7. Split at heading and block boundaries. Proposed target: 400-800 tokens per chunk,
   with up to 80 tokens of overlap for ordinary prose; preserve code and tables where possible.
8. Split oversized code/tables at complete rows or lines, retaining headers and
   continuation relationships. Never silently truncate.
9. Locate citations in the actual indexed snapshot. Character offsets must refer to
   the content whose hash is recorded.

A parser or embedding model upgrade must allow reprocessing archived content even
when HTTP returns 304.

If fetching succeeds but parsing fails, saving only an ETag must not cause the failed
version to be skipped forever on subsequent 304 responses. Retain a retryable snapshot
and processing state.

### 4.4 Mapping into rag_items

| Record | Creation |
| --- | --- |
| `source/registry` | Administrator-approved source using the existing registry |
| `source/file` | Publication service creates it for the normalized file, preserving URL, snapshot, and transformation provenance |
| `passage` | Text chunk with `heading_path`, `source_pointer`, and stable logical identity |
| `experiment` | A real configuration case with explicitly reported conditions; performance measurements are optional |
| `measurement` | Verified number, statistic, unit, and experiment association; not reconstructed from images or vague claims |
| `source/manifest` | Publication service maintains complete membership and the active version |

Initially publish sources and passages. Introduce configuration extraction later:

- LLM extraction may produce pending results with field-level source evidence, but
  cannot directly publish measurements.
- Represent extraction status and applicability separately; do not label them as
  measured evidence.
- The current passage contract does not permit `experiment_id`; do not insert an
  unsupported field.
- Initially associate configurations and passages through evidence IDs in a versioned
  case projection. Keep unverified automatic extraction in job results.
- Add persistent passage-to-experiment relationships only after schema/migration review.
- Do not use arbitrary fields to bypass Pydantic or database shape constraints.

Source expansion must cover `sources.py`, query-origin validation, publication/refresh
validation, and backup/restore formats.
Preserve version rules for existing sources. Define explicit web snapshot revisions
rather than fabricating Git commits.
New sources do not silently join the legacy API's default source set; new planning
retrieval explicitly requests approved online sources.

### 4.5 Incremental refresh, complete inventories, and publication

Reuse archives and `RagPublicationService`; do not directly insert published rows.

```text
Acquire lease
  -> read registry generation
  -> fetch and compare content
  -> build snapshot and complete inventory
  -> parse new/changed files
  -> validate structure, citations, scope, and change magnitude
  -> stage
  -> optionally prepare vector index
  -> recheck generation, lease, and source state
  -> activate manifest
  -> record result and next schedule
```

Constraints:

- Complete inventory = still-valid existing files + new/changed files - confirmed
  revoked files.
- A list of files fetched in this run is not necessarily the complete source inventory.
- Repeated synchronization of identical content must not create duplicate active
  evidence. Content and parser versions participate in idempotent identity.
- Database leases and fencing/CAS prevent an expired worker from overwriting a newer
  publication.
- Do not hold long database write transactions during network requests.
- Timeouts, 429, and 5xx responses do not imply deletion. Stop serving sources after
  401/403 responses that may indicate lost access.
- Quarantine 404 responses pending confirmation. Remove 410 or administrator-confirmed
  deletions from active membership. Retaining an archive does not authorize retrieval.
- RSS/sitemap absence is not proof of deletion. Initially use discovery only to suggest
  pages for registration.
- Require review for initial imports, unexpectedly empty content, redirects to new
  domains, or removal of more than 20% of files in one update.
- Automatically publish only validated prose updates within approved scope.
  New measurements or changes to measurement semantics require additional verification.

Do not bypass existing `RefreshPlan` review by treating program-generated
`expected_counts` as human approval. Online automatic refresh needs a separate,
versioned update-approval policy. Counts verify structural consistency, not source
trustworthiness or extraction correctness.

Retaining the previous publication on failure applies to temporary fetch/parse faults.
Revocation and loss of authorization must block retrieval of old content.

### 4.6 Multi-source cases and planning integration

Start with a request-local case projection rather than immediately migrating all history:

```text
ConfigurationCase
  case_id, case_revision
  hardware, model, runtime, workload, configuration
  measurements[]: metric/statistic/unit/value/series/evidence_id
  supporting_passage_ids[]
  provenance[]: origin/version/hash/pointer/access_scope
  applicability: exact | adaptable | reference_only | incompatible
  known_differences[], unknown_fields[], validation_required[]
```

Retrieval orchestration:

1. Resolve actual hardware and normalized requirements.
2. Query authorized internal experiments, exact public experiments, and public
   documentation/blog passages in parallel.
3. Allow candidate retrieval of documents with missing hardware metadata, but preserve
   that uncertainty in applicability decisions.
4. Associate records using stable IDs, configurations, and test conditions, not title
   similarity.
5. Apply hard constraints and authorization before ranking by workload fit, preferences,
   and evidence coverage.
6. Lexical/vector results may use reciprocal rank fusion (RRF), with a proposed rank
   constant of 60. Retrieval relevance is not performance confidence.
7. Aggregate by case and bound evidence context. Preserve experiment/series conditions
   rather than combining unrelated measurements.
8. Feed existing `CandidateProposal` generation and reuse `CandidateValidator` plus
   pre-approval validation.
9. Apply identical orchestration to creation and refinement. Without exact evidence,
   only clearly qualified adaptation suggestions are allowed.
10. Use internal query adapters that enforce actual experiment permissions. Do not
    expose a principal-free full-database scan through a new endpoint.

Proposed initial budgets: at most 50 candidates per branch, 10 cases and 20 passages
after fusion, and 8,000 evidence tokens, further capped by the model's remaining budget.
Use an explicit tokenizer or conservative counting policy. Record `candidate_total`,
`selected_count`, `truncated`, and exclusion reasons.

Keep complete minimum evidence units when the budget is exhausted. Do not remove units,
test conditions, or citations to fit more numbers. Ambiguous metrics or incompatible
statistics can support explanations only, not current SLO compliance.

Recommendation output must show:

- Configuration and corresponding validated candidate ID.
- Exact/adaptable/reference-only status and exclusion reasons.
- Satisfied constraints and preferences addressed.
- Source URL, snapshot version, last successful synchronization time, and experiment
  time, shown separately.
- Known differences, unknown conditions, and metrics requiring validation.
- Distinctions between reported measurements, inference, and AIC estimates.

### 4.7 Vector indexing as a later stage

Validate the SQL/full-text planning pipeline before enabling semantic retrieval.
Vectors are not a prerequisite for online synchronization.

First evaluate the existing OpenSearch adapter and actual deployment. If OpenSearch
is not deployed or its version lacks required filtering/vector behavior, make a
deployment decision first; do not introduce two vector engines simultaneously.
Do not reuse diagnostic indexes as public configuration-recommendation indexes.

Proposed independent, versioned index:

```text
evidence_id, case_id, origin_id, source_revision, content_sha256
publication_membership/version, access_scope
hardware/model/runtime metadata
text, embedding, embedding_model_id, dimension, parser_version
```

- Generate document and query embeddings with compatible models. Prefer a local model
  for private deployments; validate licensing, hardware requirements, and multilingual quality.
- Embed documents in synchronization/indexing jobs, not during user retrieval.
- Apply source, authorization, and current-version filters before nearest-neighbor
  selection where supported; revalidate results against authoritative storage.
- Discarding invalid hits only after top-k reduces recall. Implement replenishment
  or correct active-membership filtering.
- Prepare vectors before activating the hybrid retrieval version. The database
  publication pointer remains authoritative; unactivated versions cannot be used.
- Active manifests can retain unchanged older `item_id` values. Determine validity
  through membership, not by requiring every item's revision to equal the latest revision.
- PostgreSQL and OpenSearch are not one transaction. Test index success followed by
  publication failure, and crashes immediately after publication.
- When the index is unavailable, explicitly degrade to SQL/full-text with
  `retrieval_mode` and `degraded_reason`; do not report hybrid success.
- Support rebuilds and version rollback without relying on the index as sole storage.

## 5. Interfaces, operations, and UI

These interfaces are proposed, not existing routes:

| Endpoint | Behavior |
| --- | --- |
| `POST /api/v1/benchmark-rag/sources` | Approve/register a source; requires management permission |
| `GET /api/v1/benchmark-rag/sources` | List sources, active versions, and synchronization state |
| `PATCH /api/v1/benchmark-rag/sources/{id}` | Change schedule or disable; scope expansion requires renewed approval |
| `POST /api/v1/benchmark-rag/sources/{id}/sync` | Enqueue and return HTTP 202 with `job_id`, without waiting for network work |
| `GET /api/v1/benchmark-rag/sync-jobs/{job_id}` | Return status, changes, errors, and review requirements |

Keep `/api/v1/benchmark-rag/query` compatible. Initially integrate case retrieval as
an internal planning service. Define a separate public case-search contract only if
an independent configuration-search page is required.

Management operations enforce caller authorization and CSRF policy, and audit writes.
Read-only query permission must not grant source-management access. The scheduler
uses a restricted service identity and processes only administrator-approved sources.

UI requirements:

- Source list: URL, enabled state, schedule, last check/success/publication, current
  version, and failure reason.
- Actions: register, synchronize, disable, inspect jobs, and review anomalous updates.
  Confirm placement of the initial management page.
- Recommendations: display parsed requirements, case sources, applicability, and uncertainty.
- Manual and scheduled synchronization share one implementation; repeated clicks
  cannot produce duplicate active publications.
- Expose new per-resource configuration in the UI, not only environment variables.
- MCP-suitable routes declare `operation_id`, `summary`, and `description`.
  Explicitly classify unsuitable management/asynchronous operations in `intentionalSkips`
  and validate the catalog.

Operational metrics include synchronization successes/failures, consecutive failures,
freshness lag, pending reviews, active jobs, index lag, retrieval degradation, and
no-match rates.

Log job ID, source ID, version, error code, and duration, not sensitive document bodies,
credentials, or full user inputs. Retention follows references: do not delete snapshots
needed by active manifests or retained audits/planning references.

## 6. Phased execution

| Phase | Prerequisites | Deliverable | Gate |
| --- | --- | --- | --- |
| P0: correct inputs and identities | None | Units/statistics fixes, real hardware/model descriptors, unified requirements | A01-A04 |
| P1: public online ingestion and synchronization | Source/license/new-file approval | Registration, collection, archives, incremental publication, scheduling, state UI | A05-A12 |
| P2: configuration-case planning pipeline | P0 + P1 | Document branch, case projection, applicability, budgets, creation/refinement integration | A13-A17 |
| P3: hybrid semantic retrieval | P2 + search/embedding decisions | Independent index, vector lifecycle, fusion, degradation | A18-A21 |
| P4: controlled rollout | P2; hybrid rollout also requires P3 | Migration rehearsal, backup/restore, staged rollout, monitoring | A22-A24 |

P0 and P1 can proceed independently. Do not wait for P3 to verify that documentation
actually influences recommendations. Each phase includes its own tests and documentation.

### 6.1 Expected implementation locations

Modify only affected portions of existing files:

| Area | Repository-relative locations |
| --- | --- |
| Input, hardware, historical descriptors | `apps/backend/src/llm_d_lens/agentic/{generator,facts,planner,performance_evidence,historical_candidate_seeds}.py`; hardware profiles/providers |
| Sources and publication | `apps/backend/src/llm_d_lens/benchmark_rag/{sources,publication_contracts,publication,refresh,admin}.py` |
| Query contracts and orchestration | `apps/backend/src/llm_d_lens/benchmark_rag/{query_contracts,service,router}.py`; `agentic/service.py` |
| Models and DAOs | `apps/backend/src/llm_d_lens/db/models/rag_item.py`; `db/dao/rag_*.py` |
| Search adapter | `apps/backend/src/llm_d_lens/agentic/opensearch_evidence_search.py`; extract shared transport only if needed, without mixing diagnostic semantics |
| Recommendation display | `apps/frontend/src/pages/deployment/ui/AgenticDeploymentWorkspace.jsx` and directly related presentation components |
| Backup/restore | `benchmark_rag/transfer*.py` and `db/dao/rag_transfer.py` |
| Design and user documentation | `docs/design/agentic-deployment-design.md`, related hardware/storage designs, and matching English/Simplified Chinese site pages |

Proposed new file boundaries, with exact paths to confirm before implementation:

- `benchmark_rag/web_source.py`: fetching, normalization, and online source parsing.
- `benchmark_rag/sync.py`: orchestration, idempotency, state, and publication coordination.
- `db/models/rag_source_sync.py` and `db/dao/rag_source_sync.py`: synchronization
  configuration, state, and leases.
- Corresponding Alembic migration.
- `benchmark_rag/case_retrieval.py`: planning case projection and multi-source
  orchestration, only if existing services cannot accommodate this clearly.
- Focused unit/integration tests, management UI components, and scheduled-task
  configuration for the chosen deployment target.
- P3 index-writing/embedding adapters, to be listed after the engine decision.

Backend module paths above are relative to `apps/backend/src/llm_d_lens/` unless a
full repository-relative path is shown.

This list is not approval to create code files. Repository rules require explicit
confirmation before adding/removing source, tests, migrations, or executable configuration.
Do not introduce Celery, Kafka, or a new vector database just for this plan.
Change dependencies only for confirmed implementation needs.

## 7. Acceptance criteria

### 7.1 Functionality and consistency

| ID | Test or operation | Pass condition |
| --- | --- | --- |
| A01 | `TTFT < 2 s`, `TTFT <= 2000 ms`, Chinese concurrency requests, invalid units | Correct conversion and operators; invalid inputs produce field-level errors |
| A02 | ISL only, mean only, explicit P95, conflicting form/text | No invented P95; traceable values; no silent conflict override |
| A03 | Equivalent model spellings, 16x count, memory/PCIe/SXM variants, mixed or unknown hardware | Equivalent identities normalize; count remains separate; critical variants remain distinct; unknown is not exact |
| A04 | Historical queries missing architecture, quantization, workload, or versions | Synthetic dense/cluster/default traffic is not represented as genuine similarity; regression results document supported existing behavior |
| A05 | Approved page containing code, tables, and multiple headings | Complete source/passages, resolvable citations, correct raw/normalized hashes; visible only after activation |
| A06 | Synchronize identical content three times, including 304 | Active evidence count/version unchanged; no duplicate embedding work; correct check state |
| A07 | Change one passage, retain others; add/remove approved files | New version is current; superseded content is not returned as current; unchanged files retained; complete membership |
| A08 | Parse failure followed by 304, parser upgrade, process crash | Retry from archive; no permanent skip; previous valid publication remains available with explicit errors; staged data invisible |
| A09 | Concurrent workers, expired lease, resumed old worker | At most one effective new publication; expired workers cannot overwrite newer generations; requests are idempotent |
| A10 | 404/410, 403, timeout, 5xx, RSS absence, unexpectedly empty text | Follow section 4.5; no deletion on temporary faults; no revoked results; anomalous updates require review |
| A11 | Unapproved host, private-network redirect, oversized content, unauthorized management | Explicit rejection; prohibited follow-up requests are not sent; approval scope does not expand |
| A12 | Manual/scheduled runs, backoff, source disablement | Same execution path; due jobs claimed within the five-minute scan window; disabled sources cannot publish |
| A13 | Relevant cases in documents, internal records, and public tables | All three branches retrieve authorized evidence; preserve provenance without inflating independent experiment counts |
| A14 | Exact/similar/incompatible hardware and missing conditions | Correct applicability; incompatible configurations cannot become deployable candidates |
| A15 | Different experiments, series, P50/P95/mean, units, topologies | No cross-experiment metric assembly, P50-as-P95 substitution, or exact cross-configuration performance claims |
| A16 | Creation/refinement with identical requirements, data, versions, fixed model responses | Consistent requirements, authorization, and case filtering; both paths use relevant document evidence |
| A17 | Many matches, insufficient context budget, no matches, partial source failure | Enforce section 4.6 limits; preserve citations/conditions; explicit truncation, uncertainty, and degradation |
| A18 | Chinese/English paraphrases and exact device terms | Embedding and lexical branches both execute; fusion and model versions are observable |
| A19 | Vector hits for superseded, staged, revoked, or unauthorized items | Zero such items in final response; replenishment retrieves valid candidates |
| A20 | Index failure, index success/publication failure, model upgrade, rebuild | No mixed model dimensions/versions; previous valid retrieval works; staged content invisible; rebuilt citations agree |
| A21 | Search engine unavailable | Explicit SQL/full-text degradation with reason; constraints and source checks unchanged |
| A22 | Existing exact queries, publication/rollback, internal authorization regressions | Existing API contracts remain compatible; defaults and permissions do not expand silently |
| A23 | Backup old/new sources, restore in clean environment, rebuild indexes | Verifiable manifests, approvals, snapshots, case citations; schedules paused by default after restore |
| A24 | Disable new sources/retrieval during rollout; revoke abnormal release | Existing numeric retrieval/planning still work; switches reversible; active citations remain resolvable |

### 7.2 Retrieval quality

Create a version-pinned, manually annotated set of at least 60 queries, with at least
10 per category:

1. Exact model/configuration.
2. Chinese/English synonymous preferences.
3. Similar hardware requiring adaptation.
4. Incompatible software or hardware.
5. No evidence or insufficient information.
6. Updates, deletion, and authorization changes.

Annotate relevant case IDs, relevance grades, unacceptable cases, expected applicability,
key citations, and permissible conclusions.

Proposed rollout gates, not current measured results:

- Case Recall@10 >= 90% on queries with relevant cases.
- nDCG@10 >= 0.80 on the same set.
- Hybrid overall Recall@10 must not regress against P2 SQL/full-text retrieval.
  Improve the paraphrase subset by at least 10 percentage points; if its baseline
  is already >= 95%, require no regression instead.
- Zero unsupported exact performance claims, known-incompatible deployable candidates,
  or unauthorized/revoked content disclosures in the acceptance set.
- Every returned measurement claim has a resolvable citation, correct statistic/unit,
  and experiment association.
- Pin the generation model version and repeat each critical safety/applicability
  case three times. Manually inspect claims rather than checking JSON shape alone.
- Passing a small set is not a universal production guarantee; continue sampling
  and expanding regression coverage after rollout.

### 7.3 Performance and freshness

Use an isolated environment and record CPU, memory, storage, database/search/embedding
versions, and model hardware. Do not mix measurements with shared development workloads.
Proposed dataset: 10,000 passages, 1,000 configuration cases, concurrency 10, and at
least 500 queries after warm-up.

- P2 end-to-end retrieval P95 <= 2 seconds.
- P3 retrieval P95 <= 3 seconds, including query embedding and fusion but excluding
  final LLM generation.
- For a local mock HTTP source with <= 200 KiB text, publication completes within
  60 seconds of job claim. Measure external network delay separately.
- With six-hour per-source intervals, healthy-source change discovery takes at most
  one interval plus the five-minute scanning window. Display failure backoff separately;
  do not promise the same bound during faults.
- After disablement/revocation commits, new queries cannot return the source.
  In-flight queries revalidate before assembling results.
- Failed thresholds require stage-level timing and a remediation plan. Do not pass
  by silently reducing corpus size, excluding failed requests, or skipping authorization.

## 8. Verification and documentation

The following focused tests were run during the baseline investigation, from the
backend directory using the repository virtual environment:

```bash
cd apps/backend
../../.venv/bin/python -m pytest -q \
  tests/unit/db/test_rag_query.py \
  tests/unit/agentic/test_facts.py \
  tests/unit/agentic/test_benchmark_evidence.py \
  tests/unit/agentic/test_opensearch_evidence_search.py \
  tests/unit/agentic/test_historical_candidate_seeds.py
```

Baseline result: **129 passed, 1 skipped**. The skipped real three-corpus acceptance
test requires separately provisioned pinned local inputs.
This does not establish the proposed capabilities, production corpus completeness,
online synchronization, or recommendation quality.
Application tests were not rerun solely to prepare this document.

Implementation verification:

- P0: focused parser, hardware normalization, facts, and historical-matching tests.
- P1: local HTTP fixtures and real isolated PostgreSQL for manifests, concurrency,
  revocation, crash recovery, and API authorization. SQLite is not a substitute for
  PostgreSQL retrieval acceptance.
- P2: fixed-case end-to-end creation/refinement tests, first with fixed model responses,
  then real-model quality evaluation.
- P3: target-version search engine and selected embedding model, not only generated
  query JSON.
- P4: migrations, source backup/restore, index rebuilds, rollout, and rollback rehearsals.
- Run the smallest applicable lint/type-check/build for each change; frontend changes
  include related component tests and builds.
- After route changes, run `node scripts/generate-mcp-tools.mjs --check`.
- Update existing canonical domain designs rather than creating competing specifications.
  Public behavior requires matching English/Simplified Chinese site documentation.
  Run `npm run docs:check` and a documentation build when applicable.
- Do not default to full-repository suites, restarting every service, or modifying
  running databases.

## 9. Rollout and rollback

1. Apply backward-compatible schema changes; existing sources need no reimport.
   New fields must not change old behavior by default.
2. Start synchronization in collection/validation/preview mode without user-query
   exposure. Review initial sources and extraction quality.
3. Enable full-text retrieval for a small source set; verify freshness, revocation,
   and citations.
4. Enable planning's document branch and record differences from the old path.
   Keep the new branch independently disableable.
5. After P2 quality gates pass, run hybrid retrieval in shadow mode and switch only
   after acceptance.
6. On faults, disable the new branch/synchronization without deleting archives or
   existing evidence.
7. Recheck authorization when rolling back manifests/index versions. Rollback must
   not re-enable revoked sources.
8. Prefer feature-level rollback over destructive down migrations, retaining audit
   records and recoverability.

## 10. Decisions required before implementation

| Decision | Recommended default | Confirmation point |
| --- | --- | --- |
| Initial sources and permissions | 3-5 explicitly approved public URLs, not unrestricted crawling | Before P1 |
| Automatic-update policy | Valid prose updates within approved scope; review anomalies and measurement extraction | Before P1 |
| Scheduler deployment | Kubernetes CronJob or single-machine system timer; implement only the actual target | Before P1 |
| Management UI location | Existing administration area, separate from recommendations | Before P1 |
| New source/migration/test/configuration files | Approve the final concrete list from section 6.1 | Before file creation |
| Vector engine | Prefer deployed OpenSearch if suitable; otherwise decide deployment separately | Before P3 |
| Embedding | Local, multilingual, version-pinned; verify license and available hardware | Before P3 |
| Freshness, budgets, performance gates | Start from proposed values, confirm against a fixed environment baseline | Before phase acceptance |
| Private documents | Out of scope; design authorization, storage, and egress separately | Before scope expansion |

Next step: confirm initial public sources, deployment mode, and new-file scope, then
implement P0 and P1. Do not begin by replacing the evidence table or introducing a
vector database. Establish correct facts, reliable synchronization, and planning
integration first, then measure the incremental value of semantic retrieval.
