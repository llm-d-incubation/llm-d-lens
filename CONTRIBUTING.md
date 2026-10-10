## Governance Structure

`llm-d-lens` adopts the following hierarchical technical governance structure:

- A community of **contributors** who file issues and submit pull requests
- A body of **core maintainers** who own `llm-d-lens` overall and drive its development
- A **lead core maintainer** who is the catch-all decision maker when consensus cannot be reached by core maintainers

All contributions are expected to follow `llm-d-lens` design principles and best practices, as enforced by core maintainers. While high-quality pull requests are appreciated and encouraged, all maintainers reserve the right to prioritize their own work over code reviews at-will, hence contributors should not expect their work to be reviewed promptly.

Contributors can maximize the chances of their work being accepted by maintainers by meeting a high quality bar before sending a PR to maintainers.

### Core maintainers

The core maintainers lead the development of `llm-d-lens` and define the benchmarking infrastructure and strategy for the broader `llm-d project`. Their responsibilities include:

- Proposing, implementing and reviewing load profiles, parameter configurations, run rules, data collections, and analysis of workloads to `llm-d`
- Enforcing code quality standards and adherence to core design principles

The core maintainers should publicly articulate their decision-making, and share the reasoning behind their decisions, vetoes, and dispute resolution.

List of core maintainers can be found in the [OWNERS](OWNERS) file.

### Lead core maintainer

When core maintainers cannot come to a consensus, a publicly declared lead maintainer is expected to settle the debate and make executive decisions.

The Lead Core Maintainer should publicly articulate their decision-making, and give a clear reasoning for their decisions.

The Lead Core Maintainer is also responsible for confirming or removing core maintainers.

#### Lead maintainer (as of 10/08/2026)

- [Tyler Rimaldi](https://github.com/vezio)

### Decision Making

#### Uncontroversial Changes

We are committed to accepting functional bug fixes that meet our quality standards – and include minimized unit tests to avoid future regressions. Performance improvements generally fall under the same category, with the caveat that they may be rejected if the trade-off between usefulness and complexity is deemed unfavorable by core maintainers. Design changes that neither fix known functional nor performance issues are automatically considered controversial.

#### Controversial Changes

More controversial design changes (e.g., breaking changes to workload profiles, load generators, run rules or data collection and analysis tools) are evaluated on a case-by-case basis under the subjective judgment of core maintainers.

## Submitting a Pull Request

We welcome contributions to any aspect of `llm-d-lens`! If you have a bug fix, feature request, or improvement, please submit a pull request (PR) to the repository.

For every Pull Request submitted, ensure the following steps have been done:

1. [Sign your commits](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-commits)
2. Make sure that the [pre-commit](https://pre-commit.com/) hooks have been run and pass **before you push** the contents of your PR. See [Local Development Checks](#local-development-checks-pre-commit) below.

## Repository language

Write all repository content and PR titles/descriptions in English. Run
`npm run check:english` before committing or opening/updating a PR. Agents must
translate findings automatically, review their meaning, and rerun until clean.
Use `-- --text-file /tmp/pr-title.txt --text-file /tmp/pr-body.md` to include
PR metadata in the check. The scanner reports Han characters in Git-visible text
and filenames; it is not a natural-language classifier for every other language.
Do not replace text with escapes or remove useful content to bypass the policy.

## Development and validation

Before changing behavior, follow [AGENTS.md](AGENTS.md) and the
[reuse-first workflow](docs/refactoring/reuse-first-agent-design.md). Find existing implementations,
compare contracts and callers, and record reuse/new-code rationale in the PR.
Pause dependent work for unresolved business, compatibility, CI exception or
catalog decisions; obtain and record the user's actual answer before proceeding.

Run `npm run reuse:test` for reuse-tool changes and
`npm run reuse:check -- --task <id> --base <task-start-commit> --report .cache/reuse/report.json`
for behavior changes. Follow the workflow's `begin`, `search --task` and `verify`
evidence steps; run `verify-report --report .cache/reuse/report.json` before delivery.
Export the ignored task evidence and command logs for review. Reports are bound to
Git-visible inputs; ignored runtime data is outside their validity guarantee.
Update `.reuse/catalog.json` and run `npm run reuse:map` when capability entries
change. Similarity candidates require analysis, not automatic refactoring.

Follow [README.md](README.md) for setup. Choose checks appropriate to the change:

```bash
npm run build
npm run type-check
make test-js
make test-python
make lint
```

Document substantial feature designs under `specs/changes/`. Keep API descriptions
and relevant documentation in sync with frontend and backend behavior. Real
cluster deployment and external provider calls require a configured environment;
report separately whether those flows were exercised.

Preserve the upstream Apache 2.0 [license](LICENSE) and source copyright notices.
