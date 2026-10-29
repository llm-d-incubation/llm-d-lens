# Benchmark RAG review remediation: 2026-10-20

## Scope

The user authorized fixing the two P1 and three P2 findings from the review of
`c6f4796` and recording execution. This is a code-and-isolated-test change, not
approval to refresh a business database, deploy to the migration target, change
schemas, implement deferred Agentic ingestion work, or push. The initial
implementation request did not authorize a commit; after pausing execution, the
user explicitly requested a checkpoint commit before resuming fixes and
validation.

This execution record was explicitly added to Git despite the default `docs/*`
ignore rule, without changing that rule. The existing ignored local RAG design
links to this versioned record; its historical acceptance and deployment
checkpoints are unchanged.

## Fixes in priority order

- **Priority:** P1
  - **Finding:** A p99 question returned a mean measurement as `answered`.
  - **Implementation and regression:** Extract explicit
      percentile/mean/median/min/max terms into the statistic filter, normalize
      query aliases, and reject conflicts with explicit filters. Multiple
      statistics abstain before either retrieval branch. PostgreSQL regression
      requires `no_exact_match` when only mean is available, while a mean
      question still returns the exact original value.

- **Priority:** P1
  - **Finding:** An unrecognized model family was dropped, allowing Qwen
      results for a Llama question.
  - **Implementation and regression:** Replace the family allowlist with
      structured model identifiers and bounded labeled/subject forms. Preserve
      exact names, reject conflicting explicit model filters, and abstain on
      multiple/ambiguous model subjects. Public and internal regressions require
      no evidence for the absent requested model.

- **Priority:** P2
  - **Finding:** Prism refresh retained stale experiment metadata when run
      JSON bytes were unchanged.
  - **Implementation and regression:** Include cited merged-record and
      original-ZIP dependencies in invalidation. Real isolated PostgreSQL tests
      cover corrected hardware/runtime/configuration and citation hashes,
      unchanged payload bytes, staged isolation, retained old items/bytes and
      no-change idempotence.

- **Priority:** P2
  - **Finding:** A figure review with tables `(0, 99)` accepted table 0 alone
      as complete coverage.
  - **Implementation and regression:** Require verified experiments for every
      selected table. Regressions reject mixed valid/missing selections and
      preserve valid/empty selection behavior.

- **Priority:** P2
  - **Finding:** `What is TTFT?` required an experiment, while TPOT
      measurement questions could become definitions.
  - **Implementation and regression:** Share metric vocabulary between intent
      resolution and metric filtering, including TTFT/TPOT long forms.
      Definition phrasing without an explicit measurement scope searches
      passages; explicit intent remains authoritative. A PostgreSQL regression
      verifies a pinned TTFT passage is returned as prose, never a measurement.

## Execution log

1. Added P1 regression tests before changing query planning: **25 failed, 3
   passed**. After the P1 fixes, the planner/public-query selection passed **53
   tests**, including the pinned three-corpus acceptance.
2. Added definition/metric-routing regressions before the P2 intent fix: **9
   failed, 9 passed**. Model-only edge checks also exposed two regressions
   during implementation (hardware treated as a model, and the existing guide
   alias with an explicit source path); both received targeted fixes.
3. The independent ingestion work added
   `test_snapshot_refresh_reparses_unchanged_payload_dependencies` and
   `test_figure_review_requires_experiments_for_every_selected_table`. Its
   initial selection had **4 failures and 3 passes**, then **7 passes**. The
   scoped Prism/repository/publication/repository-refresh suite passed **83
   tests**, including provisioned pinned-source acceptance.
4. The first combined regression run was stopped at the user's request before a
   final pytest result; it is not counted as a successful run. MCP tests passed
   **4/4**, Ruff/format and documentation links passed, and both locale builds
   succeeded with the existing OWNERS broken-link and missing-blog warnings.
5. On resumption, the repository-wide English check and staged whitespace check
   passed. Commit **`9ae2183`** preserves the five fixes, regressions and this
   execution record as an explicitly incomplete-validation checkpoint. The
   pre-existing untracked `server/` directory was excluded. No push was made.
6. Added regressions for three observed query edge cases: an explicit metric
   filter lost numeric intent, quoted model names containing structured tokens
   were counted twice, and a missing model name became a placeholder filter. The
   selection initially had **10 failures and 3 passes**. After treating labeled
   names atomically, rejecting empty labels and retaining explicit metric scope,
   all **13 passed**. The tests also verify abstention before either public or
   opt-in internal retrieval and preserve ordinary `model-8B` identifiers.
7. The resumed combined run completed with **337 passed and 1 failed** in 375.96
   seconds. The existing public baseline caught a compatibility regression: "an
   unregistered baseline record" was misclassified as an ambiguous model
   subject, changing `no_exact_match` into `model_scope_required`. The
   baseline's expected outcome was not weakened or edited. A focused regression
   reproduced the error (**1 failed, 1 passed**); bounded record-description
   grammar now distinguishes that subject while retaining model constraints
   alongside record IDs.
8. After that correction, the final
   query/authorization/refresh/public-and-internal- baseline selection passed
   **257 tests, zero skips**, in **252.07 seconds**. Ingestion/publication code
   and its **83 passing tests** are unchanged; their completed results were
   reused rather than rerunning unrelated imports. All five original findings
   and the follow-up parser regressions now have passing affected checks. No
   fixed-baseline expected outcome was changed.

## Validation

Final query regression command (repository root):

```bash
PYTHONPATH=apps/backend/src .venv/bin/python -m pytest -q --tb=short \
  --show-capture=no --durations=5 \
  apps/backend/tests/unit/benchmark_rag \
  apps/backend/tests/unit/db/test_rag_query.py \
  apps/backend/tests/unit/db/test_rag_agentic_query.py \
  apps/backend/tests/unit/db/test_rag_agentic_service.py \
  apps/backend/tests/unit/db/test_rag_refresh.py \
  apps/backend/tests/unit/db/test_rag_public_baseline.py \
  apps/backend/tests/unit/db/test_rag_internal_baseline.py
```

Result: **257 passed, 10 pre-existing deprecation warnings, zero skips** (252.07
seconds). This includes the provisioned three-corpus **20/20 public fixed
cases** and the **12/12 authorized internal fixed cases**, plus real-source
answer acceptance, provenance verification, negative-condition queries, caller
isolation, REST behavior and refresh/query regressions.

Additional checks:

- MCP adapter: **4 passed** using
  `node --import tsx --test apps/server/testing/mcp/benchmarkRag.test.ts`.
  Adapter code and inputs have not changed since that run.
- Ruff lint/format and Pylance diagnostics on the final query implementation
  passed; IDE problems reported no errors in the changed Python files.
- `npm run docs:check`, `npm run docs:build` (English and Simplified Chinese)
  and `npm run check:english` passed after the resumed API-documentation edits.
  The existing `resources/OWNERS` broken-link and missing-blog warnings remain;
  these unrelated warnings were not suppressed or fixed.
- Whitespace checks passed. After validation, the user explicitly authorized a
  separate follow-up commit containing the post-checkpoint fixes and this final
  record. The original checkpoint is preserved; no push is authorized or
  performed.

## Compatibility and operational boundaries

- REST and MCP still share one query service and unchanged request/response
  schemas. Access checks, opt-in internal retrieval, read-only behavior, Decimal
  evidence values and original-byte verification remain intact.
- Automatic question parsing remains bounded and English-only, not general NLP.
  Other conditions require explicit filters. Use separate exact queries for
  multiple models/statistics rather than dropping a required condition.
  Published EN/ZH REST/Python and MCP documentation describes these rules.
- No schema or parser-version change is required for the dependency fix;
  existing manifests remain readable. An identical-inventory refresh does not
  retroactively repair already-stale stored items. No existing index was
  refreshed in this task. Reusing a revision with changed content remains
  subject to immutable-version rejection.
- Test databases are disposable pgserver instances/schemas, not the persistent
  acceptance database or the remote deployment. Existing processes and the
  pre-existing untracked directory are untouched. No deployment acceptance or
  repair of historical production data is claimed.

## Relevant implementation and tests

- [Query planning and answer service](../apps/backend/src/llm_d_lens/benchmark_rag/service.py)
- [Prism importer](../apps/backend/src/llm_d_lens/benchmark_rag/prism.py)
- [Repository parser](../apps/backend/src/llm_d_lens/benchmark_rag/repository_corpus.py)
- [Planning regressions](../apps/backend/tests/unit/benchmark_rag/test_query_planning.py)
- [Public query regressions](../apps/backend/tests/unit/db/test_rag_query.py)
- [Internal query regressions](../apps/backend/tests/unit/db/test_rag_agentic_service.py)
- [Prism dependency regressions](../apps/backend/tests/unit/db/test_prism_import.py)
- [Figure review regressions](../apps/backend/tests/unit/db/test_repository_corpus.py)
