# The Lens MCP tool catalog (apps/server/src/mcp/tools.ts) is a fully generated
# build artifact -- see scripts/generate-mcp-tools.mjs and
# apps/server/src/mcp/specialTools.ts
# (the real hand-written source). It is gitignored, so it must be produced
# here rather than assumed to already exist in the build context. Generating
# it needs BOTH Python (to dump the llm_d_lens FastAPI app's OpenAPI schema,
# which requires the optional "embedded-db" extra -- see
# apps/backend/src/llm_d_lens/db/settings.py -- since module import eagerly opens a DB
# session) and Node (to run the generator script itself), so this starts
# from the Python image and layers Node on top via apt -- the reverse
# (starting from a Node image and installing Python) hit a Python
# 3.13/numpy wheel incompatibility for the backend's aiconfigurator
# dependency. Pinned to 3.12 (matching the "frontend-and-server" CI job's
# Python version) because pgserver -- the embedded-db extra's PostgreSQL
# backend -- does not yet publish a 3.14 build.
FROM python:3.14-slim AS mcp-tools

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY apps/backend/pyproject.toml ./apps/backend/pyproject.toml
COPY apps/backend/src ./apps/backend/src
RUN python3 -m venv .venv \
    && .venv/bin/pip install --no-cache-dir --upgrade pip \
    && .venv/bin/pip install --no-cache-dir -e "apps/backend[embedded-db]"

COPY scripts/generate-mcp-tools.mjs ./scripts/generate-mcp-tools.mjs
COPY apps/server/src/mcp/specialTools.ts ./apps/server/src/mcp/specialTools.ts
RUN node scripts/generate-mcp-tools.mjs

# Build stage
FROM node:26-alpine AS build

WORKDIR /app

COPY package*.json ./
COPY docs/site/package.json ./docs/site/package.json
COPY apps/frontend/package.json ./apps/frontend/package.json
COPY apps/server/package.json ./apps/server/package.json
COPY packages/shared/package.json ./packages/shared/package.json
RUN npm ci --ignore-scripts

COPY . .
COPY --from=mcp-tools /app/apps/server/src/mcp/tools.ts ./apps/server/src/mcp/tools.ts
RUN npm run build

# Serve stage
FROM node:26-alpine

WORKDIR /app

# Guide planning renders official llm-d Kustomize overlays and inspects an
# available Kubernetes context. Git is required by remote Kustomize URLs.
RUN apk add --no-cache curl git kubectl

# Install production dependencies for server
COPY package*.json ./
COPY docs/site/package.json ./docs/site/package.json
COPY apps/frontend/package.json ./apps/frontend/package.json
COPY apps/server/package.json ./apps/server/package.json
COPY packages/shared/package.json ./packages/shared/package.json
RUN npm ci --omit=dev --ignore-scripts --workspace @repo/server --workspace @repo/shared

# Copy server and built assets
COPY apps/server ./apps/server
COPY packages/shared ./packages/shared
COPY --from=mcp-tools /app/apps/server/src/mcp/tools.ts ./apps/server/src/mcp/tools.ts
COPY --from=build /app/apps/frontend/dist ./apps/frontend/dist

# The Node guide planner reads these profile-owned, versioned rendering inputs.
COPY apps/backend/src/llm_d_lens/versions ./apps/backend/src/llm_d_lens/versions
COPY apps/backend/src/llm_d_lens/hardware/profiles ./apps/backend/src/llm_d_lens/hardware/profiles
COPY apps/backend/src/llm_d_lens/deploy/providers/guide_overlays ./apps/backend/src/llm_d_lens/deploy/providers/guide_overlays

ENV LENS_DATA_DIR=/var/lib/lens
ENV LENS_CACHE_DIR=/var/cache/lens
ENV LENS_LOG_DIR=/var/log/lens
VOLUME ["/var/lib/lens", "/var/cache/lens", "/var/log/lens"]
ENV PORT=8080
EXPOSE 8080

CMD ["npm", "start"]
