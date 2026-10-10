#!/usr/bin/env node
// Copies the handful of docs pages that live outside docs/docusaurus/docs/ into the
// Docusaurus docs tree before dev/build, so docs/design/*.md and the
// repo-root CONTRIBUTING.md stay the single canonical source (see the
// comment at the top of the former docs/fern/docs.yml) instead of being
// hand-duplicated and allowed to drift. Run via `npm run docs:sync`, or
// automatically as a `pre`-step of `start`/`build` (see package.json).
//
// Destination files are generated and gitignored (docs/docusaurus/.gitignore) -
// never edit them directly; edit the source file instead and re-run this
// script (or `npm start` / `npm run build`, which do it for you).
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '..', '..', '..');
const WEBSITE_DOCS = join(__dirname, '..', 'docs');

const REPO_ROOT_LINKS_TO_GITHUB = [
  'README.md',
  'CODE_OF_CONDUCT.md',
  'AGENTS.md',
  'LICENSE',
  '.agents/skills/ui/SKILL.md',
  'docs/refactoring/reuse-first-agent-design.md',
];
const GITHUB_BLOB_BASE =
  'https://github.com/llm-d-incubation/llm-d-lens/blob/main/';

/** @type {{src: string, dest: string, slug: string, title: string, rewriteRepoLinks?: boolean}[]} */
const SOURCES = [
  {
    src: 'docs/design/agentic-deployment-architecture.zh-CN.md',
    dest: 'architecture/agentic-deployment.md',
    slug: '/architecture/agentic-deployment',
    title: 'Agentic deployment',
  },
  {
    src: 'docs/design/agentic-planning-evidence-and-rag.zh-CN.md',
    dest: 'architecture/agentic-planning-evidence.md',
    slug: '/architecture/agentic-planning-evidence',
    title: 'Planning evidence and RAG',
  },
  {
    src: 'CONTRIBUTING.md',
    dest: 'resources/contributing.md',
    slug: '/resources/contributing',
    title: 'Contributing',
    rewriteRepoLinks: true,
  },
];

function withFrontmatter(body, { slug, title }) {
  return `---\ntitle: ${title}\nslug: ${slug}\n---\n\n${body}`;
}

function rewriteLinks(body) {
  let out = body;
  for (const link of REPO_ROOT_LINKS_TO_GITHUB) {
    out = out.replaceAll(`](${link})`, `](${GITHUB_BLOB_BASE}${link})`);
  }
  return out;
}

async function main() {
  for (const entry of SOURCES) {
    const srcPath = join(REPO_ROOT, entry.src);
    const destPath = join(WEBSITE_DOCS, entry.dest);
    let body = await readFile(srcPath, 'utf8');
    if (entry.rewriteRepoLinks) {
      body = rewriteLinks(body);
    }
    const out = withFrontmatter(body, entry);
    await mkdir(dirname(destPath), { recursive: true });
    await writeFile(destPath, out, 'utf8');
    console.log(`synced ${entry.src} -> docs/docusaurus/docs/${entry.dest}`);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
