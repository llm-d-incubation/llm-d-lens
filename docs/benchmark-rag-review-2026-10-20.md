# Benchmark RAG review remediation: 2026-10-20

## Scope

The user authorized fixing the two P1 and three P2 findings from the review of
`c6f4796` and recording execution. This is a code-and-isolated-test change, not
approval to refresh a business database, deploy to the migration target, change
schemas, implement deferred Agentic ingestion work, or commit/push.

This execution record is outside the ignored `docs/design/` area so it can be
versioned with the fixes. The existing local RAG design links to this record;
its historical acceptance and deployment checkpoints are unchanged.

## Fixes in priority order

| Priority | Finding | Implementation and regression |
| --- | --- | --- |
| P1 | A p99 question returned a mean measurement as `answered`. | Extract explicit percentile/mean/median/min/max terms into the statistic filter, normalize query aliases, and reject conflicts with explicit filters. Multiple statistics abstain before either retrieval branch. PostgreSQL regression requires `no_exact_match` when only mean is available, while a mean question still returns the exact original value. |
| P1 | An unrecognized model family was dropped, allowing Qwen results for a Llama question. | Replace the family allowlist with structured model identifiers and bounded labeled/subject forms. Preserve exact names, reject conflicting explicit model filters, and abstain on multiple/ambiguous model subjects. Public and internal regressions require no evidence for the absent requested model. |
| P2 | Prism refresh retained stale experiment metadata when run JSON bytes were unchanged. | Include cited merged-record and original-ZIP dependencies in invalidation. Real isolated PostgreSQL tests cover corrected hardware/runtime/configuration and citation hashes, unchanged payload bytes, staged isolation, retained old items/bytes and no-change idempotence. |
| P2 | A figure review with tables `(0, 99)` accepted table 0 alone as complete coverage. | Require verified experiments for every selected table. Regressions reject mixed valid/missing selections and preserve valid/empty selection behavior. |
| P2 | `What is TTFT?` required an experiment, while TPOT measurement questions could become definitions. | Share metric vocabulary between intent resolution and metric filtering, including TTFT/TPOT long forms. Definition phrasing without an explicit measurement scope searches passages; explicit intent remains authoritative. A PostgreSQL regression verifies a pinned TTFT passage is returned as prose, never a measurement. |

## Execution log

1. Added P1 regression tests before changing query planning: **25 failed,
   3 passed**. After the P1 fixes, the planner/public-query selection passed
   **53 tests**, including the pinned three-corpus acceptance.
2. Added definition/metric-routing regressions before the P2 intent fix:
   **9 failed, 9 passed**. Model-only edge checks also exposed two regressions
   during implementation (hardware treated as a model, and the existing guide
   alias with an explicit source path); both received targeted fixes.
3. The independent ingestion work added
   `test_snapshot_refresh_reparses_unchanged_payload_dependencies` and
   `test_figure_review_requires_experiments_for_every_selected_table`.
   Its initial selection had **4 failures and 3 passes**, then **7 passes**.
   The scoped Prism/repository/publication/repository-refresh suite passed
   **83 tests**, including provisioned pinned-source acceptance.
4. Final combined regression is in progress. Intermediate passing runs do not
   certify subsequent edits. Final commands and results are recorded below.

## Validation

Pending the final combined run.

## Compatibility and operational boundaries

- REST and MCP still share one query service and unchanged request/response
  schemas. Access checks, opt-in internal retrieval, read-only behavior,
  Decimal evidence values and original-byte verification remain intact.
- Automatic question parsing remains bounded and English-only, not general
  NLP. Other conditions require explicit filters. Use separate exact queries
  for multiple models/statistics rather than dropping a required condition.
  Published EN/ZH REST/Python and MCP documentation describes these rules.
- No schema or parser-version change is required for the dependency fix;
  existing manifests remain readable. An identical-inventory refresh does
  not retroactively repair already-stale stored items. No existing index was
  refreshed in this task. Reusing a revision with changed content remains
  subject to immutable-version rejection.
- Test databases are disposable pgserver instances/schemas, not the persistent
  acceptance database or the remote deployment. Existing processes and the
  pre-existing untracked directory are untouched. No deployment acceptance
  or repair of historical production data is claimed.

## Relevant implementation and tests

- [Query planning and answer service](../apps/backend/src/llm_d_lens/benchmark_rag/service.py)
- [Prism importer](../apps/backend/src/llm_d_lens/benchmark_rag/prism.py)
- [Repository parser](../apps/backend/src/llm_d_lens/benchmark_rag/repository_corpus.py)
- [Planning regressions](../apps/backend/tests/unit/benchmark_rag/test_query_planning.py)
- [Public query regressions](../apps/backend/tests/unit/db/test_rag_query.py)
- [Internal query regressions](../apps/backend/tests/unit/db/test_rag_agentic_service.py)
- [Prism dependency regressions](../apps/backend/tests/unit/db/test_prism_import.py)
- [Figure review regressions](../apps/backend/tests/unit/db/test_repository_corpus.py)
