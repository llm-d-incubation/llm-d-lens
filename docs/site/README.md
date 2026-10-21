# Lens documentation site

The `@repo/docs` npm workspace lives here. Docusaurus reads both languages
directly from their source directories; there is no translation-copy stage.

## Layout and authoring

```text
docs/
  design/                     Canonical module designs
  site/
    i18n/
      en/docusaurus-plugin-content-docs/current/
      zh-CN/docusaurus-plugin-content-docs/current/
    scripts/                  Site preparation tools
    src/                      Pages, components and styles
    static/                   Images and static assets
    docusaurus.config.ts
    sidebars.ts
    package.json
```

Edit corresponding files under the two `current/` directories together, preserving
document IDs and public routes. The local search plugin hashes both source trees
so edits in either language invalidate its cached index. Chinese navigation/sidebar translations remain
under `i18n/zh-CN/`; the English homepage is in `src/pages/`, with its Chinese
translation under `i18n/zh-CN/docusaurus-plugin-content-pages/`.

The English Agentic architecture/evidence pages and contribution guide are the
only generated document pages. Edit `docs/design/agentic-deployment-design.md`
or root `CONTRIBUTING.md` instead. `scripts/sync-external-docs.mjs` projects them
into the English tree; their output paths are gitignored. Other English and
Chinese documents are tracked source files, not generated copies.

Only `i18n/zh-CN/` is exempt from the repository English check. English files
under `i18n/en/`, site source/configuration and repository documents remain checked.

## User documentation style

Write user guides and tutorials as tasks, not implementation reports:

- Start with the user's goal and put the most common task first.
- Name the page, visible control, input and expected result. Match labels to the
  current UI; in Chinese, retain the English label when needed to find a control.
- Give one recommended path before optional settings. Use short numbered steps
  and small decision tables instead of long paragraphs or inventories of fields.
- Put a requirement beside the step that needs it and link to the task that
  satisfies it. Explain what to do when an operation is blocked.
- Keep detailed references, such as evaluation Guide choices and parameters,
  out of the User Guide sidebar. Link them from the relevant module steps instead.
- Link specialized design references from the relevant Architecture overview
  section rather than listing them as sidebar children.
- Keep warnings about data loss, credentials, permissions and service interruption.
  Do not bury them in background explanation.
- Keep API schemas, internal request handling, component reuse, historical fixes
  and compatibility implementation details in API references or canonical designs.
- End a task with an observable result, not "continue in the workflow."
- Update both languages and check tutorial steps against their module guides.

## Development and build

Install dependencies with `npm ci` from the repository root, then run:

```bash
npm run docs:dev
npm run docs:check
```

To preview Chinese from the repository root:

```bash
npm run start --workspace @repo/docs -- --locale zh-CN
```

Both `start` and `build` explicitly run canonical-page synchronization and the
bounded GitHub-stat refresh before Docusaurus. They do not rely on npm lifecycle
hooks, which the repository's `ignore-scripts=true` disables. GitHub-stat failures
warn and retain the existing value or create an empty fallback.

Broken routes, Markdown links and anchors fail `docs:build`; the docs CI job
runs it and therefore blocks merge. Build output is `docs/site/build/`, including
`zh-CN/` for Chinese. The `.docusaurus/`, `build/`, generated pages and fetched
stats are not committed.
Use `npm run serve --workspace @repo/docs` to inspect the built site.

## CI and publishing

`.github/workflows/docs-deploy.yaml` installs/builds this workspace and uploads
`docs/site/build/` to GitHub Pages. Site files, the canonical Agentic design,
CONTRIBUTING, root dependency manifests and npm configuration trigger publishing.
The site keeps the existing `/llm-d-lens/` base URL and locale routes.
