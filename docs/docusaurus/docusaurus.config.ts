import { themes as prismThemes } from 'prism-react-renderer';
import type { Config } from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// GitHub Pages project-site config: served at
// https://llm-d-incubation.github.io/llm-d-lens/ (org user/org page would be
// llm-d-incubation.github.io itself; this repo is a regular project repo, so
// it gets a /llm-d-lens/ baseUrl instead).
const ORG_NAME = 'llm-d-incubation';
const PROJECT_NAME = 'llm-d-lens';
const SLACK_URL = 'https://llm-d.slack.com/';

// Inline SVGs (not static image files) so the icon-only navbar links and
// icon+label footer links can use `currentColor` and follow the active
// light/dark navbar color automatically - see `.navbar__icon-link` /
// `.footer__icon-link` in src/css/custom.css.
const GITHUB_ICON_SVG = `<svg viewBox="0 0 16 16" aria-hidden="true"><path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>`;
const SLACK_ICON_SVG = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>`;

const config: Config = {
  title: 'Lens Documentation',
  tagline: 'Deploy and operate large language models on llm-d.',
  favicon: 'img/favicon.png',

  future: {
    v4: true,
  },

  url: `https://${ORG_NAME}.github.io`,
  baseUrl: `/${PROJECT_NAME}/`,

  organizationName: ORG_NAME,
  projectName: PROJECT_NAME,

  // Pre-existing Fern-era content has a handful of links to not-yet-written
  // pages (tracked separately); don't fail CI builds on those, just surface
  // them in the build log.
  onBrokenLinks: 'warn',
  onBrokenAnchors: 'warn',

  // Plain `.md` files (the canonical design docs copied in from docs/design/
  // and CONTRIBUTING.md) use CommonMark, not MDX, so math notation like
  // `S_{model}` isn't misread as a JSX expression. `.mdx` pages still get
  // full MDX/JSX support (Card, Tabs, etc.).
  markdown: {
    format: 'detect',
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'zh-CN'],
    localeConfigs: {
      en: { label: 'English' },
      'zh-CN': { label: '简体中文' },
    },
  },

  presets: [
    [
      'classic',
      {
        docs: {
          path: 'docs',
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          editUrl: `https://github.com/${ORG_NAME}/${PROJECT_NAME}/tree/main/docs/docusaurus/`,
          // No release has been versioned yet (no git tags exist), so there
          // is only the unversioned "current" docs set, built straight from
          // the `main` branch. Docusaurus labels that set "Next" by default;
          // relabel it "main" so the navbar button reflects the branch it's
          // built from instead of an unreleased-sounding default. `path: ''`
          // keeps it served at the site root (no URL change).
          //
          // Once a release is tagged, run `npm run docusaurus docs:version
          // <X.Y.Z>` from docs/docusaurus/: it freezes the current docs into
          // versioned_docs/version-<X.Y.Z>/, records it in versions.json, and
          // the navbar item below automatically becomes a real dropdown
          // listing "main" plus every released version - no further config
          // change needed here.
          lastVersion: 'current',
          versions: {
            current: {
              label: 'main',
              path: '',
            },
          },
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  // Client-side, fully static search index (no external service/Algolia
  // account needed, which fits a GitHub Pages-only deployment). With no
  // explicit `search` navbar item configured below, theme-classic renders
  // its search icon right after the light/dark color-mode toggle by
  // default - see @docusaurus/theme-classic Navbar/Content.
  themes: [
    [
      '@easyops-cn/docusaurus-search-local',
      {
        hashed: true,
        indexDocs: true,
        indexPages: true,
        docsRouteBasePath: '/',
        // Both languages so the local search index tokenizes Chinese text
        // (zh-CN locale content) correctly alongside the English default.
        language: ['en', 'zh'],
      },
    ],
  ],

  themeConfig: {
    image: 'img/lens_logo.png',
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Lens',
      logo: {
        alt: 'Lens',
        src: 'img/lens_logo.png',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          type: 'doc',
          docId: 'resources/contributing',
          position: 'left',
          label: 'Contributing',
        },
        // Right after "Contributing" (not pinned to the far right), per the
        // "version number next to Contributing" placement.
        {
          type: 'docsVersionDropdown',
          position: 'left',
        },
        {
          // Position is irrelevant here: src/theme/Navbar/Content pulls this
          // item out of the normal left/right item flow and renders it
          // itself, immediately to the right of the light/dark mode toggle
          // (per the "language picker to the right of system mode" request).
          type: 'localeDropdown',
          position: 'right',
        },
        {
          // Registered in src/theme/NavbarItem/ComponentTypes.tsx: GitHub
          // logo + live star count fetched from the public GitHub API (see
          // src/components/NavbarGitHubStar).
          type: 'custom-githubStar',
          position: 'right',
        },
        {
          type: 'html',
          position: 'right',
          value: `<a href="${SLACK_URL}" target="_blank" rel="noopener noreferrer" class="navbar__icon-link navbar__icon-link--labeled" aria-label="Join Slack">${SLACK_ICON_SVG}<span>Join Slack</span></a>`,
        },
        // No explicit `search` navbar item here: the theme-classic navbar
        // appends the default SearchBar right after the color mode toggle
        // automatically whenever no explicit `search` item is configured
        // (see @docusaurus/theme-classic Navbar/Content) and the search
        // plugin below (themes: ...) is active - that lands the search icon
        // exactly to the right of the light/dark toggle, as requested.
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            { label: 'Quickstart', to: '/quickstart' },
            { label: 'User Guide', to: '/user-guide/model-market' },
            { label: 'Architecture', to: '/architecture' },
          ],
        },
        {
          title: 'More',
          items: [
            {
              html: `<a href="https://github.com/${ORG_NAME}/${PROJECT_NAME}" class="footer__link-item footer__icon-link" target="_blank" rel="noopener noreferrer">${GITHUB_ICON_SVG}<span>GitHub</span></a>`,
            },
            {
              html: `<a href="${SLACK_URL}" class="footer__link-item footer__icon-link" target="_blank" rel="noopener noreferrer">${SLACK_ICON_SVG}<span>Slack</span></a>`,
            },
            {
              label: 'llm-d',
              href: 'https://llm-d.ai/',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} The llm-d Authors. Apache 2.0 License.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
