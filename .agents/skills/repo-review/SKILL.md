---
name: repo-review
description: Review a repository or module comprehensively when explicitly requested, preserving scope and reporting verified findings without task bookkeeping.
---

# Repository review

Inspect Git status and the requested scope; preserve unrelated existing changes.
For a whole-repository request, inventory the current source roots, tests and
configuration with `rg --files`. An old findings list does not define current
scope. Exclude generated files, dependencies and runtime data explicitly.

Review implementations, callers and tests by capability. Apply only the domain
contracts relevant to each area. Compare behavior before unifying similar code.
When implementation is requested, complete clear compatible fixes; pause only
changes with unresolved business or compatibility meaning. Respect read-only
requests and do not implicitly commit, deploy or resolve historical decisions.

Run affected tests and checks directly. Use `npm run verify:full` for an explicitly
requested full local validation, and inspect failed logs. Report findings with
locations, changes, tests actually run and areas not verified. Passing tool checks
do not prove semantic coverage; do not claim a complete review of unexamined code.
No task JSON, per-file hashes or generated evidence reports are required.
