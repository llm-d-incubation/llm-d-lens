import React, { useEffect, useState } from 'react';
import BrowserOnly from '@docusaurus/BrowserOnly';
import githubStats from '@site/src/data/github-stats.json';
import styles from './styles.module.css';

const REPO = 'llm-d-incubation/llm-d-lens';

// Same GitHub mark used elsewhere in the navbar/footer (see docusaurus.config.ts
// GITHUB_ICON_SVG) plus a star glyph, composed as one navbar item so the live
// star count sits directly next to the GitHub logo.
const GITHUB_ICON = (
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    <path
      fillRule="evenodd"
      d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
    />
  </svg>
);

const STAR_ICON = (
  <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true">
    <path d="M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.75.75 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Zm0 2.445L6.615 5.5a.75.75 0 0 1-.564.41l-3.097.45 2.24 2.184a.75.75 0 0 1 .216.664l-.528 3.084 2.769-1.456a.75.75 0 0 1 .698 0l2.77 1.456-.53-3.084a.75.75 0 0 1 .216-.664l2.24-2.183-3.096-.45a.75.75 0 0 1-.564-.41L8 2.694Z" />
  </svg>
);

function formatCount(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k` : String(n);
}

function NavbarGitHubStarInner(): JSX.Element {
  // Seed from the count baked in at build/dev-start time (see
  // scripts/fetch-github-stats.mjs) so the number shows immediately, even on
  // networks where the browser can't reach api.github.com directly. Still
  // attempt a live fetch to refresh it when that succeeds.
  const [stars, setStars] = useState<number | null>(githubStats.stars);

  useEffect(() => {
    let cancelled = false;
    fetch(`https://api.github.com/repos/${REPO}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled || !data) return;
        setStars(data.stargazers_count);
      })
      .catch(() => {
        /* Keep the build-time count if the GitHub API is unreachable or rate-limited. */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <a
      href={`https://github.com/${REPO}`}
      target="_blank"
      rel="noopener noreferrer"
      className={`navbar__icon-link ${styles.githubStar}`}
      aria-label={`GitHub${stars === null ? '' : ` (${stars} stars)`}`}
    >
      {GITHUB_ICON}
      <span className={styles.starCount}>
        {STAR_ICON}
        {stars !== null && <span>{formatCount(stars)}</span>}
      </span>
    </a>
  );
}

function NavbarGitHubStarStatic(): JSX.Element {
  return (
    <a
      href={`https://github.com/${REPO}`}
      target="_blank"
      rel="noopener noreferrer"
      className={`navbar__icon-link ${styles.githubStar}`}
      aria-label="GitHub"
    >
      {GITHUB_ICON}
      <span className={styles.starCount}>
        {STAR_ICON}
        {githubStats.stars !== null && <span>{formatCount(githubStats.stars)}</span>}
      </span>
    </a>
  );
}

/**
 * Navbar item (registered via src/theme/NavbarItem/ComponentTypes.tsx as
 * type "custom-githubStar") combining the GitHub logo with a live star count
 * fetched client-side from the public GitHub REST API. Wrapped in
 * BrowserOnly since Docusaurus prerenders the navbar at build time and there
 * is no server-side fetch here; the `fallback` keeps the icon-only link
 * visible in the prerendered HTML (before hydration fills in the count)
 * instead of leaving an empty gap.
 */
export default function NavbarGitHubStar(): JSX.Element {
  return (
    <BrowserOnly fallback={<NavbarGitHubStarStatic />}>
      {() => <NavbarGitHubStarInner />}
    </BrowserOnly>
  );
}
