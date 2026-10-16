#!/usr/bin/env node
// Copies the handful of docs pages that live outside docs/site/i18n/en/docusaurus-plugin-content-docs/current/ into the
// Docusaurus docs tree before dev/build, so docs/design/*.md and the
// repo-root CONTRIBUTING.md stay the single canonical source (see the
// comment at the top of the former docs/fern/docs.yml) instead of being
// hand-duplicated and allowed to drift. Run via `npm run sync-external-docs`
// in the site workspace, or as part of `start`/`build` (see package.json).
//
// Destination files are generated and gitignored (docs/site/.gitignore) -
// never edit them directly; edit the source file instead and re-run this
// script (or `npm start` / `npm run build`, which do it for you).
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '..', '..', '..');
const WEBSITE_DOCS = join(__dirname, '..', 'i18n', 'en', 'docusaurus-plugin-content-docs', 'current');

const REPO_ROOT_LINKS_TO_GITHUB = [
  'README.md',
  'CODE_OF_CONDUCT.md',
  'AGENTS.md',
  'LICENSE',
  '.agents/skills/ui/SKILL.md',
  'docs/design/reuse-workflow.md',
];
const GITHUB_BLOB_BASE =
  'https://github.com/llm-d-incubation/llm-d-lens/blob/main/';

/** @type {{src: string, dest: string, slug: string, title: string, part?: string, displayedSidebar?: string, rewriteRepoLinks?: boolean}[]} */
const SOURCES = [
  {
    src: 'docs/design/agentic-deployment-design.md',
    dest: 'architecture/agentic-deployment.md',
    slug: '/architecture/agentic-deployment',
    title: 'Agentic deployment',
    part: 'architecture',
    displayedSidebar: 'docsSidebar',
  },
  {
    src: 'docs/design/agentic-deployment-design.md',
    dest: 'architecture/agentic-planning-evidence.md',
    slug: '/architecture/agentic-planning-evidence',
    title: 'Planning evidence and RAG',
    part: 'evidence',
    displayedSidebar: 'docsSidebar',
  },
  {
    src: 'CONTRIBUTING.md',
    dest: 'resources/contributing.md',
    slug: '/resources/contributing',
    title: 'Contributing',
    rewriteRepoLinks: true,
  },
];

function withFrontmatter(body, { slug, title, displayedSidebar }) {
  const sidebar = displayedSidebar ? `displayed_sidebar: ${displayedSidebar}\n` : '';
  return `---\ntitle: ${title}\nslug: ${slug}\n${sidebar}---\n\n${body}`;
}

function rewriteLinks(body) {
  let out = body;
  for (const link of REPO_ROOT_LINKS_TO_GITHUB) {
    out = out.replaceAll(`](${link})`, `](${GITHUB_BLOB_BASE}${link})`);
  }
  return out;
}

function extractDesignPart(body, part) {
  const opening = `<!-- design-part: ${part} -->`;
  const closing = `<!-- /design-part: ${part} -->`;
  const start = body.indexOf(opening);
  const end = body.indexOf(closing);
  if (start < 0 || end < start + opening.length || body.indexOf(opening, start + opening.length) >= 0
      || body.indexOf(closing, end + closing.length) >= 0) {
    throw new Error(`Expected exactly one complete design part: ${part}`);
  }
  let fence = null;
  return body.slice(start + opening.length, end).trim().split('\n').map(line => {
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (marker) {
      if (!fence) {
        fence = marker[1];
      } else if (marker[1][0] === fence[0] && marker[1].length >= fence.length
          && !marker[2].trim()) {
        fence = null;
      }
      return line;
    }
    return fence ? line : line.replace(/^( {0,3})#(#{1,5}) /, '$1$2 ');
  }).join('\n') + '\n';
}

async function main() {
  const stats = join(REPO_ROOT, 'docs/site/src/data/github-stats.json');
  await mkdir(dirname(stats), { recursive: true });
  try { await writeFile(stats, JSON.stringify({ stars: null, fetchedAt: null }) + '\n', { flag: 'wx' }); }
  catch (error) { if (error.code !== 'EEXIST') throw error; }
  for (const entry of SOURCES) {
    const srcPath = join(REPO_ROOT, entry.src);
    const destPath = join(WEBSITE_DOCS, entry.dest);
    let body = await readFile(srcPath, 'utf8');
    if (entry.part) {
      body = extractDesignPart(body, entry.part);
    }
    if (entry.rewriteRepoLinks) {
      body = rewriteLinks(body);
    }
    const out = withFrontmatter(body, entry);
    await mkdir(dirname(destPath), { recursive: true });
    await writeFile(destPath, out, 'utf8');
    console.log(`synced ${entry.src} -> docs/site/i18n/en/docusaurus-plugin-content-docs/current/${entry.dest}`);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
