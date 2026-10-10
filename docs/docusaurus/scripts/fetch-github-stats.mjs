#!/usr/bin/env node
// Fetches the repo's star count from the public GitHub REST API at build/
// dev-start time and writes it to src/data/github-stats.json, which the
// navbar GitHub star widget (src/components/NavbarGitHubStar) imports as its
// baked-in value.
//
// Why bake it in at build time rather than only fetching client-side: this
// site is often viewed from networks that can reach GitHub Pages but not
// api.github.com directly (corporate proxies/firewalls), so a browser-only
// fetch silently fails there and leaves the count blank forever. The
// component still attempts a live client-side fetch to override this value
// when the visitor's network does allow it; this file is just the fallback.
//
// Run via `npm run fetch-github-stats`, or automatically as a `pre`-step of
// `start`/`build` (see package.json). Never fails the build: on any error
// (offline, rate-limited, etc.) it leaves the existing file untouched, or
// writes nulls if no file exists yet.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ProxyAgent, setGlobalDispatcher } from 'undici';

// Node's built-in fetch (undici) does not honor HTTP(S)_PROXY env vars the
// way curl/browsers do, so an outbound-proxy environment (e.g. a corporate
// network) would otherwise make this script always fail. Route through the
// configured proxy, if any, exactly like the rest of this toolchain's HTTP
// clients already do.
const proxyUrl = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy;
if (proxyUrl) {
  setGlobalDispatcher(new ProxyAgent(proxyUrl));
}

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO = 'llm-d-incubation/llm-d-lens';
const OUT_FILE = join(__dirname, '..', 'src', 'data', 'github-stats.json');

async function main() {
  let stats = { stars: null, fetchedAt: null };

  try {
    const res = await fetch(`https://api.github.com/repos/${REPO}`);
    if (res.ok) {
      const data = await res.json();
      stats = { stars: data.stargazers_count, fetchedAt: new Date().toISOString() };
    } else {
      console.warn(`fetch-github-stats: GitHub API responded ${res.status}, keeping previous value`);
    }
  } catch (err) {
    console.warn(`fetch-github-stats: could not reach GitHub API (${err.message}), keeping previous value`);
  }

  if (stats.stars === null) {
    // Network/API failure: keep whatever was already on disk instead of
    // overwriting a good value with nulls.
    try {
      await readFile(OUT_FILE, 'utf8');
      return;
    } catch {
      // No existing file either; fall through and write the nulls so the
      // component has a file to import on a first-ever, offline build.
    }
  }

  await mkdir(dirname(OUT_FILE), { recursive: true });
  await writeFile(OUT_FILE, `${JSON.stringify(stats, null, 2)}\n`);
  console.log(`fetch-github-stats: wrote ${OUT_FILE} (stars=${stats.stars})`);
}

main();
