# Repository development rules

## Default: focused changes

Start with the requested file or a targeted `rg` search. Read its implementation,
relevant callers and tests, then make the smallest compatible change. Reuse
existing code before adding abstractions or dependencies. Honor the user's stack.
Focused means avoiding unrelated changes, not stopping partway through a
multi-part request. When the user names a set (all modules, all pages, every
endpoint), that whole set is the task; do not treat each element as a finished
task or stop after the first passing module.
Do not read the whole capability catalog, design library or all skills by default.
If the owner is unclear, search `.reuse/map.md` for the relevant entries only.

Ordinary features and fixes do not require task JSON, decision ledgers, semantic indexing,
duplicate scans, per-capability reports, full builds or service restarts.
Cross-module work needs affected-caller analysis and tests, not automatically a
formal audit. For requested reviews see [Repo review](.agents/skills/repo-review/SKILL.md);
run tests directly without automatic audit records.
Do not add helpers, frameworks, tests of trivial wiring or repeated documentation
just to satisfy a process. Explain non-obvious choices briefly; no mandatory plan
or decision table for a straightforward fix.

## Human approval for structural changes

- Do not create or delete code files without explicit human confirmation. This
  covers source, tests, scripts, migrations and executable configuration. A
  feature request authorizes behavior, not unstated file additions or deletions;
  describe the proposed paths and wait for approval before acting.
- Before changing `.gitignore`, assess the impact and obtain explicit human
  confirmation. Report what becomes ignored, whether tracked files, secrets,
  generated output or runtime data are affected, and any cleanup or migration
  required. A request to change ignore rules is not approval until that impact is
  explained and confirmed.

## Read only applicable contracts

Read the relevant sections of these skills when the change touches their contracts;
file location alone does not require reading every section or linked document.

| Change | Reference |
| --- | --- |
| React behavior, shared UI, charts | [UI](.agents/skills/ui/SKILL.md) |
| API, persistence, backend tasks, Kubernetes transport | [Backend](.agents/skills/backend/SKILL.md) |
| Startup, containers, CI, cluster lifecycle | [Deployment](.agents/skills/deployment/SKILL.md) |
| Schemas, DAOs, migrations | [Database](.agents/skills/database/SKILL.md) |
| Login, permissions, ownership, sessions, authenticated routes | [Auth](.agents/skills/auth/SKILL.md) |
| Explicit comprehensive review | [Repo review](.agents/skills/repo-review/SKILL.md) |

Keep domain rules in their domain; shared modules must not depend on pages.
For application file lifecycle changes, read [storage layout](docs/design/storage-layout.md).
Generated files, dependencies and runtime data are not implementation sources.
Fix catalog references when moving or removing a registered entry. Adding a feature
does not require catalog registration. Catalog expansion is explicit maintenance;
regenerate its map only when the catalog changes.

## Verification and decisions

Run affected checks once on final inputs. Reuse results in this session while
those inputs/dependencies remain unchanged; expand testing for shared contracts,
failures or uncertain impact. Report actual checks and limitations concisely.
`npm run verify:full` is a manual checkpoint, not the default for every task;
usage and limits are in [CONTRIBUTING.md](CONTRIBUTING.md#periodic-full-local-validation).
Read failed logs, not successful full logs. Complete the full requested scope
before stopping. Do not treat one passing module, page or test as completion when
the request covers multiple items. Do not expand a finished task into an unrelated
repository review, but do not narrow a multi-part request either. Intermediate
checks may guide work but must not end the task. Do not claim full validation from
a partial check or carry an old passing result across source/environment changes.

Continue clear authorized work. Ask only when business meaning, compatibility,
ownership or an exception remains unresolved after inspecting the relevant code.
Existing unresolved decisions still apply; never fabricate approval or delete
historical ledger entries to pass checks. Discuss new decisions in the conversation.
Commit only when requested. Before commits/PRs run `npm run check:english` over
the repository; PR titles/bodies must also pass `--text-file` checks. Fix findings
without hiding content or weakening checks. All repository text is English except
published translations under `docs/site/i18n/zh-CN/`.

## MCP tool catalog

A FastAPI route intended for Lens Assistant or MCP must declare an explicit
`operation_id`, `summary` and `description` on its decorator. After changing
backend routes, run `node scripts/generate-mcp-tools.mjs --check`; it validates
the Python catalog and fails on any unexpected skipped operation. Add UI-only,
internal, streaming, secret-bearing or otherwise unsuitable routes to
`intentionalSkips` in `apps/backend/src/llm_d_lens/mcp/definitions.json`.
The catalog implementation is `llm_d_lens.mcp.catalog`; special handlers are in
`llm_d_lens.mcp.tools`. No generated Node server artifact is required.

## REST and in-process Python SDK parity

Any change to a REST route, request/response contract, permission, default, status
code or behavior must update the matching in-process Python SDK surface in the same
change:

- `apps/backend/src/llm_d_lens/sdk/local.py`: typed namespace, `LocalModule`
  binding or `lens.services` path;
- permission resolution and tests under `apps/backend/tests/unit/sdk/`;
- both `REST API` and `In-process Python SDK` tabs on the module page, in every
  supported docs locale.

Do not ship a REST-only change that leaves the SDK or its examples stale. An
`In-process Python SDK` example must call `lens.*`; direct `llm_d_lens.*` service
calls are an internal Python API, not the SDK. If an operation intentionally
cannot be exposed in-process, record the reason in the SDK and docs instead of
leaving a silent gap.

Response-class coverage is part of the same contract. A route with
`response_model=dict` or no `response_model` is not complete for SDK parity:
replace it with a concrete Lens response class, or record why no class is
appropriate. Adding `TypeAdapter(response_model)` normalization in the SDK without
filling those route schemas is only a partial fix, not completion.

Each domain module defines its public request and response classes in
`models.py` (or the existing canonical models module). The REST handler, the
Python service operation and the in-process Python SDK operation must share the
same request class and the same response class:

```text
REST transport -> Request model -> service operation -> Response model -> HTTP JSON
SDK context     -> Request model -> service operation -> Response model -> Python caller
```

The service operation is the single implementation. A router is only an HTTP
adapter (path/query/form/header mapping, status codes, streaming); it must not
rebuild a second response dict. The SDK is only an identity/permission adapter;
it must not wrap dicts in a generic response class. Do not add
`response_model=dict`, `_payload()` response projections, `LensResponse` /
`LensListResponse` fallbacks, or `lens.services.*` examples for public
operations. HTTP-only concerns (pagination transport, file downloads, event
streams) may remain adapter-specific, but their domain request/response contract
is still shared.

## Keep the docs site in sync

When updating published documentation, update every supported language version
in the same change (currently English and Simplified Chinese, as configured in
`docs/site/docusaurus.config.ts`). Keep corresponding pages, API examples and
UI instructions consistent; do not leave translations for a later task. This
applies to changes initiated in any language. Check the matching locale pages
for omissions before finishing; internal English-only design documents do not
require new translations.

Update docs for changed public behavior: user guides for page behavior, and the
matching module's REST/Python API tabs for changed endpoints or underlying public
callables. Pages live under `docs/site/i18n/en/docusaurus-plugin-content-docs/current/`.
Private refactors do not require unrelated doc edits. `CONTRIBUTING.md` and marked
parts of `docs/design/agentic-deployment-design.md` are canonical sources for their
synced site pages; edit those sources, not generated copies.
Use `npm run docs:check` for local links; use `npm run docs:build` when rendering,
MDX, routes, anchors, site configuration or dependencies need validation.
Keep one canonical design per domain in `docs/design/`, with lowercase hyphenated
English filenames; update references when moving it. Avoid duplicate design docs.

## Hardware is profile-driven (never hardcoded)

Vendor labels, resource names, drivers, runtime images, telemetry and capability
gates come from `apps/backend/src/llm_d_lens/hardware/` profiles/providers, not
vendor branches or default GPU assumptions in consumers. Unknown hardware stays
neutral; never overwrite a user choice on discovery. See the
[hardware architecture](docs/design/hardware-plugin-architecture.md) for changes.
Per-resource configuration must be available in the UI, not only environment
variables. Keep create/edit behavior consistent.

## Local development

Use Vite hot reload. Restart only an affected service when necessary with
`scripts/lens-local.sh restart backend|server|frontend`; use `restart all` for
shared environment/ports or API changes requiring a new MCP catalog. Tooling and
documentation changes do not require application startup.
