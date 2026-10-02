# llm-d-lens

Lens manages models, inference deployments, evaluation, simulation, and cluster
resources.

## Pages

- Model market (`model-market`)
- Deployments (`optimization-deployments`)
- Evaluation (`optimization-evaluate`), including task creation, configuration, and results
- Simulation (`optimization-simulate`)
- Model services (`model-service`, admin at `admin/model-service`)
- Lens Assistant (`playground`)
- Clusters (`clusters`)
- Storage (`storage-management`)
- Model cache (`model-cache`)
- External providers (`ai-providers`)
- Observability (`cluster-monitoring-stack`)

The original upstream Prism benchmark browser, Results Store, workload catalog, schema
explorer, regression dashboards, and well-lit-path demonstration pages have been
removed. Their GCS/GIQ/Drive data ingestion and GitHub submission authentication
are no longer required. Shared UI and infrastructure used by Lens remain.

## Local development

```bash
git clone https://github.com/llm-d-incubation/llm-d-lens.git
cd llm-d-lens
npm install
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev]'
node scripts/generate-mcp-tools.mjs
npm run dev
```

The frontend runs on port 5173, the Node API on port 3000, and the Python backend
is started by `scripts/run-backend.sh`. Use `npm run dev:web` when running the
Python backend separately. For the full development environment and configurable
ports, see `scripts/dev.sh`.

`scripts/dev.sh start` and `restart` automatically regenerate and validate the
reuse map before changing services. Invalid registrations or pending user
decisions block startup without stopping existing services. Coding agents register
new reusable capabilities as part of the feature; no separate manual map command
is needed. See [the reuse workflow](docs/refactoring/reuse-first-agent-design.md).

```bash
npm run build
npm run type-check
make test-js
make test-python
```

The generated MCP catalog is required for the assistant and Node server. Regenerate
it when backend routes change. Docker builds generate it automatically.

The original upstream Prism Cloud Run publishing workflow has been removed. Use the Lens
Dockerfiles, Compose configuration, or `scripts/dev.sh` for this application.

## Documentation

The documentation site is built with [Fern](https://buildwithfern.com) and lives
in [`docs/fern/`](docs/fern/docs.yml). Existing repository docs under `docs/` are
referenced from the site navigation, so they stay the single source of truth.

```bash
npm run docs:dev     # local preview with hot reload
npm run docs:check   # validate configuration and page paths
```

- [AI development: reuse-first workflow](docs/refactoring/reuse-first-agent-design.md)
- [Reusable capability map](docs/reuse-map.md)
- [File storage, migration, and feature development rules](docs/design/storage-layout.md)

- [PR change description](PR_README.md)

- [Evaluation](docs/evaluation/README.md)
- [Simulation setup](docs/design/simulation-runner-design.md)
- [Deployment API](docs/deploy-api.md)
- [Capacity planner in-tree integration](docs/design/capacity-planner-integration-design.md) ([legacy document path](docs/design/capacity-planner-integration-design.zh-CN.md))
- [Agentic deployment architecture](docs/design/agentic-deployment-architecture.zh-CN.md)
- [Cluster monitoring](docs/design/cluster-monitoring-service-design.md)

The project retains the upstream [Apache 2.0 license](LICENSE) and copyright
notices.
