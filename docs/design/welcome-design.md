# Welcome Page Design

**Status:** Implemented (V1). This document replaces the operator Dashboard
design; the Welcome page, its issue view and `/api/v1/welcome/*` routes are in
place.

Welcome is the operator landing page. It combines three jobs:

1. Greet the signed-in operator.
2. Start a conversation with Lens Assistant.
3. Surface current cluster issues and provide direct paths to the owning pages.

It is not a fleet dashboard, a monitoring wall, or a second usage-report page.
Detailed cluster capacity, model service tables, deployment activity and token
consumption stay on their owning pages.

## 1. Layout

```text
+--------------------------------------------------------------------------------+
| Welcome, alice                                                                  |
| Review the latest cluster and service status, and take action when needed. |
+----------------------------------------------------------+---------------------+
| +------------------------------------------------------+ | Quick actions       |
| | Ask Lens Assistant                                   | |                     |
| |                                                      | | Add a model deployment      |
| | [ What do you want to investigate or do?          ]  | | Add model service   |
| |                                                      | | Cache a new model   |
| | [AI provider v]                            [Send]    | | Create a cluster    |
| +------------------------------------------------------+ |                     |
|                                                          |                     |
| Needs attention                                          |                     |
| +------------------------------------------------------+ |                     |
| | CRITICAL  prod-a / model-alpha unavailable           | |                     |
| | 0 of 2 eligible providers ready                      | |                     |
| | [To check]                                           | |                     |
| |                                                      | |                     |
| | WARNING   prod-b / model-beta pressure               | |                     |
| | Decode waiting persisted over 5 min                  | |                     |
| | [To check] [To fix]                                  | |                     |
| +------------------------------------------------------+ |                     |
| View all issues (6)                                      |                     |
+----------------------------------------------------------+---------------------+
```

- Desktop: left column is the primary column and occupies roughly two thirds of
  the content width. The right column is a narrow quick-action rail.
- The left column is wider at every breakpoint where two columns are used. On
  narrow screens the columns stack and the left column remains first.
- The page has no summary KPI strip, no hardware cards, no token panel and no
  model-service table.
- The page uses the shared page shell, `PageHeader`/`ModuleHeader`, `Panel`,
  `Button`, `Select`, `Badge`/`StatusChip`, `EmptyState` and form primitives.
  No page-specific visual system is introduced.
- The header includes **Star on GitHub**. It opens GitHub sign-in and returns
  to the `llm-d-incubation/llm-d-lens` repository; the user clicks Star there.

## 2. Navigation and naming

- Rename the first sidebar item from **Dashboard** to **Welcome**.
- Canonical view: `welcome`.
- Keep `dashboard` as an alias to `welcome`, and `dashboard/alerts` as an alias
  to `welcome/issues`, so existing bookmarks and links keep working during the
  transition.
- The sidebar entry remains above **Services**, without an extra visible group
  heading.
- The old `home -> clusters` alias remains unchanged. Existing domain Back
  buttons continue to return to Clusters.
- `firstAccessibleView` continues to select the first permitted sidebar entry.
  Welcome becomes the operator landing page; consumer-only roles skip it.
- The full issue list remains available at `welcome/issues`. It is reached from
  **View all issues**, not from a second sidebar item.
- When no cluster is accessible and the caller may create clusters, Welcome opens
  a one-time prompt whose primary action starts the create-cluster wizard.

## 3. Left column

### 3.1 Welcome greeting

The first element is a short, personal greeting:

```text
Welcome, <username>
Review the latest cluster and service status, and take action when needed.
```

- Use the authenticated principal's username. Do not fetch additional directory
  profile data just for the greeting.
- The subtitle is static and does not claim that the fleet is healthy.
- No cluster selector is placed above the greeting. Scope belongs to the issue
  section, where it is relevant.

### 3.2 Lens Assistant starter

The chat box is a prompt starter, not a conversation surface. It contains:

- A multi-line text input.
- An AI provider selector in the lower-left corner.
- A primary **Send** action in the lower-right corner.
- A short hint that sending opens Lens Assistant with the message.

Suggested placeholder:

```text
Ask about a model, deployment, cluster issue, or next step…
```

Behavior:

- The provider selector lists only providers that Lens Assistant can use. The
  current assistant path supports OpenAI-compatible providers, so provider types
  that the assistant cannot drive are not offered.
- Preselect the first compatible provider when one exists.
- **Send** is disabled when:
  - the input is empty (after trimming),
  - no compatible provider is selected,
  - the provider list is loading,
  - the caller lacks `playground:chat:use`, or
  - a handoff is already being prepared.
- Submit shortcuts: `Ctrl+Enter` / `Cmd+Enter` sends. `Enter` inserts a newline
  in the multi-line input.
- The input is capped at a documented length (proposed 4,000 characters) and the
  counter appears only when the user approaches the limit.
- The provider dropdown is inside the composer card, lower-left, and the Send
  action is lower-right. The layout must remain usable at narrow widths by
  stacking the provider selector above the Send button if needed.

#### No provider available

If the provider request succeeds but no compatible provider exists:

- Keep the composer visible but disable Send.
- Show an inline empty state inside the composer:

  ```text
  No AI provider is available for Lens Assistant.
  Add an OpenAI-compatible provider in External providers.
  [Open External providers]
  ```

- The link opens `ai-providers` (External providers) only when the caller can
  view that page. Adding a provider still requires
  `ai-provider:provider:create`; when the caller cannot add one, say that an
  administrator must add a provider instead of showing a dead action.
- If the caller can read providers but the provider request fails, show a
  retryable error inside the composer. Do not claim that no provider exists.

#### No assistant permission

If the caller lacks `playground:chat:use`, do not render an unusable form.
Replace the composer with a short note:

```text
Lens Assistant is not available for this account.
```

The rest of the Welcome page remains usable.

#### Handoff to Lens Assistant

Sending a prompt submits a chat turn in Lens Assistant, not on the Welcome
page. The handoff is a one-shot draft:

1. Build `{ prompt, providerId, createdAt }`.
2. Store it in `sessionStorage` under a dedicated key, for example
   `prism_welcome_assistant_draft`.
3. Navigate to `playground` (Lens Assistant).
4. The assistant page consumes the draft once, selects the provider when it
   still exists, places the prompt in the composer, focuses it, and removes the
   stored draft.
5. As soon as the provider and its chat service are ready, Lens Assistant
   submits the prompt automatically. Tool calls still follow the assistant's
   normal confirmation rules.

Rules:

- Do not put the prompt text in the URL. It may contain sensitive cluster or
  model details and would otherwise enter history, logs and shared links.
- Auto-submit waits until the selected provider is resolved. If the provider is
  missing and no compatible fallback exists, do not submit; show the normal
  provider guidance instead.
- Do not submit twice. Clear the pending auto-submit marker before sending, and
  ignore late drafts after a turn has started.
- Drafts expire after a short interval (proposed 5 minutes). An expired,
  malformed or already-consumed draft is ignored.
- If the selected provider was deleted between the two pages, keep the prompt,
  select the first available compatible provider, and show a non-blocking note
  that the original provider is no longer available.
- If the assistant page is not accessible, report the access problem normally;
  do not silently discard the prompt.

### 3.3 Cluster issues

Below the composer, show current cluster issues for the caller's authorized
scope. This is the only operational summary on Welcome.

Section header: **Needs attention**.

Each issue row contains:

- Severity: critical, warning or info, with text and icon, never color alone.
- A specific title, for example `model-alpha has no ready provider`.
- Cluster and resource identity.
- One line of evidence with units and the first time the issue was seen.
- One primary diagnostic link, and at most one secondary management link.
- Coverage or stale state when the evidence is partial.

Examples of rules retained from the operational issue evaluator:

| Category | Example finding | Primary link |
| --- | --- | --- |
| Onboarding | No accessible cluster, a missing HF_TOKEN Secret, no model-cache storage, an empty model-cache volume, no ready model in the cluster cache, or no compatible External AI provider (info). The missing-storage issue opens the storage create form with the cluster and Model cache purpose prefilled. | Clusters, Storage or Model cache. |
| Access | Active users with no direct role and no role inherited from their groups, or groups with no role binding (info). Each user finding names the user and links to that user's role editor. | Users or Groups. |
| Cluster component | Monitoring, device plugin or node condition is not ready. Dashboard-only monitoring degradation is informational when core metrics are healthy. | Cluster or Observability. |
| Model serving | Enabled model has no ready provider, or Gateway/IPP is unavailable. | Model services. |
| Serving pressure | Sustained engine waiting or backend error rate on a backing execution. | Observability / profiling for that execution. |
| Deployment | A recent deployment operation failed. | Deployments filtered to the execution. |
| Deployment | A ready deployment is not published by any model service. | Model services with the add form requested. |
| Storage / cache | Provisioning or download/sync failed. | Storage or Model cache. |

Behavior:

- Default scope is every cluster the caller can read.
- Compact severity and cluster filters in the issues header can narrow the list.
  They do not affect the assistant composer or the quick-action permissions.
- Sort by severity, then serving impact, then observation time, with stable ID as
  the tie-breaker.
- Show at most eight issues on Welcome. When more exist, show
  **View all issues (N)** and open `welcome/issues`.
- Zero issues means **All systems operational** with normal health and telemetry coverage.
  Permission omissions are not data failures.
- Missing telemetry is a data-gap indicator, not a healthy result.
- A backend background task composes current issues for every cluster every 60
  seconds and stores the latest snapshot in the database.
- Request handlers read that snapshot and filter its issues again for the
  caller's cluster access and category permission; they do not compose on the
  request path when a snapshot exists.
- Poll the persisted snapshot every 15 seconds with `usePolling`; keep the
  current rows during quiet refresh. Switching pages does not trigger another
  check.
- If an issue fetch fails, retain the last observed rows and mark them stale with
  a retry action. Do not block the composer.

## 4. Right column: quick actions

The right rail contains eight actions, in this order:

| Order | Action | Destination and intent | Required access |
| --- | --- | --- | --- |
| 1 | Token usage analytics | Open Usage reports. | `usage:report:read`. |
| 2 | Add a model deployment | Open Model market (deployment entry). | `deployment:run:read` and `deployment:run:create`. |
| 3 | Publish a model service | Open Model services with the add form requested (`action=add`). | `model-service:group:read` and `model-service:group:manage`. |
| 4 | Launch benchmark | Open Evaluation's new-task wizard. | `evaluate:run:read` and `evaluate:run:create`. |
| 5 | Replay traces | Open Simulation with the new-task form requested (`action=new`). | `simulation:task:read` and `simulation:task:create`. |
| 6 | Manage storages | Open Storage. | `storage:volume:read`. |
| 7 | Cache a new model | Open Model cache with the download dialog requested (`action=download`). | `model-cache:entry:read` and `model-cache:entry:create`. |
| 8 | Create a new cluster | Open Clusters with the create wizard requested (`action=create`). | `cluster:cluster:read` and `cluster:cluster:create`. |

Rules:

- Render only actions whose destination view and action permission are present.
  Never show an action that the backend will reject.
- The rail is navigation only. It does not submit forms or run writes.
- In a future cluster-scoped variant, an action may carry the selected cluster as
  intent. It must never silently choose a cluster the user did not select.
- The action list does not include Observability, Evaluation, Simulation or
  account administration. Those remain in the sidebar and in issue deep links.
- If no action is permitted, hide the rail rather than showing an empty card.

## 5. Permissions and scope

| Surface | Requirement |
| --- | --- |
| Welcome page and greeting | `cluster:cluster:read`. |
| Assistant composer | `playground:chat:use`. |
| AI provider selector | `ai-provider:provider:read`; only providers the assistant supports. |
| External providers link | `ai-provider:provider:read`; adding requires `ai-provider:provider:create`. |
| Cluster issues | The permission and scope rules of the issuing domain, plus reachable clusters. |
| Quick actions | Destination-view access and the exact creation/manage action in scope. |

Backend authorization is authoritative. Filter every issue, count, provider and
shortcut before rendering. Do not retrieve privileged issue data merely to hide
it in the browser. A maintainer sees issues in reachable clusters, not the global
identity directory or another cluster's resources.

## 6. Data and API

### 6.1 Welcome overview

Replace the current dashboard overview with a Welcome-specific read contract:

```http
GET /api/v1/welcome/overview?clusterId=<optional>
```

Response:

```json
{
  "generatedAt": "2026-10-18T12:41:03Z",
  "scope": {
    "mode": "all",
    "clusters": [{ "id": "cluster-a", "name": "prod-a" }]
  },
  "issues": {
    "state": "complete",
    "generatedAt": "2026-10-18T12:41:03Z",
    "observedAt": "2026-10-18T12:40:30Z",
    "stale": false,
    "coverage": { "observed": 1, "expected": 1, "unit": "clusters" },
    "data": []
  }
}
```

- The Welcome response contains only issue data and observation metadata.
- It does not contain hardware summaries, cluster capacity, service tables,
  deployment activity, token usage or AI provider configuration.
- The backend may reuse the existing issue evaluator and collectors internally;
  the response is narrower.
- `GET /api/v1/welcome/issues` keeps the existing issue-list filters
  (`clusterId`, `severity`, `category`, `search`, `cursor`, `limit`) for the full
  issue page.
- Reuse `/api/v1/ai-providers` for the provider selector. Do not add a
  Welcome-specific provider endpoint.
- The prompt handoff is browser-only; it has no backend endpoint.

### 6.2 Migration and cleanup

- Add `welcome` and `welcome/issues` views. Keep `dashboard` and
  `dashboard/alerts` as aliases during the transition.
- Move the issue list and filters from the old alerts page to `welcome/issues`.
- The old Dashboard-specific summary, cluster-capacity, model-service,
  deployment-activity and usage panels are removed from the landing page.
- `/api/v1/welcome/*` replaces the dashboard composition routes. Only the issue
  envelope and observation metadata are returned; the old dashboard response
  fields are not part of the Welcome contract.
- Keep the issue evaluator, pressure summary, authorization helpers and deep
  links. They remain the source of cluster findings.
- No new database schema is required.

## 7. UI states

| Situation | Behavior |
| --- | --- |
| First load | Render greeting, composer shell and issue skeleton. Do not block the page on provider or issue requests. |
| Providers loading | Provider select is disabled with a loading label; Send is disabled. |
| No compatible provider | Inline empty state with the External providers link (permission-gated). Send disabled. |
| Provider request failed | Inline retry; do not claim that no provider exists. |
| No assistant permission | Replace composer with a permission note; issues and shortcuts remain. |
| Issues loading | Show a small section skeleton. |
| No issues | `All systems operational` with normal health and telemetry coverage. |
| Issue fetch failed | Retain last issues, mark stale, show retry. |
| No accessible clusters | Greeting and composer remain; issues explain that cluster access is needed. Show only shortcuts whose creation actions do not require an existing cluster and are authorized. |
| Scope lost | Clear the issue scope and offer a return to all accessible clusters; do not silently widen. |
| Prompt handoff succeeds | Navigate to Lens Assistant; no toast is needed. |
| Prompt handoff cannot be prepared | Keep the input and show an inline error. |

## 8. Accessibility and interaction

- The greeting is the page heading; do not add a competing page title.
- The textarea has a visible or screen-reader label and a character limit hint.
- The provider select has an explicit label, not only a placeholder.
- `Ctrl/Cmd+Enter` sends; `Enter` inserts a newline.
- Focus moves to the assistant composer after a successful handoff.
- Every issue action is a button/link with an intent-specific label
  (`To check`, `To fix`, `To resolve`).
- Stale, partial and unknown states use text, not color alone.
- Keyboard focus is preserved during quiet issue refresh.

## 9. Acceptance scenarios

| Scenario | Required result |
| --- | --- |
| Admin or maintainer signs in | Welcome is the first sidebar item and landing page. |
| Custom role with only cluster read | Greeting and issues render; assistant and unauthorized shortcuts are omitted. |
| End-user role | Welcome is not offered; consumer landing behavior is unchanged. |
| One compatible provider | Provider is preselected; Send moves the prompt to Lens Assistant. |
| No compatible provider | Composer stays visible, Send is disabled, External providers link is offered when authorized. |
| Provider is removed before handoff | Prompt is retained in Assistant, fallback provider is selected, and a non-blocking note is shown. |
| Prompt contains sensitive text | It does not appear in the URL, history or server logs from the Welcome handoff. |
| Component offline or model pressure | A specific issue with evidence and the correct deep link is shown. |
| More than eight issues | Welcome shows eight and links to the full issue list. |
| Issue polling fails | Last issues remain, visibly stale, with retry. |
| Each quick action | Opens the correct destination and intent; unauthorized actions are absent. |
| Narrow viewport | Left column stacks first; composer and provider selector remain usable. |

## 10. Decisions adopted in V1

1. **Send behavior.** The Welcome composer carries a one-shot draft, selects the
   chosen provider, and auto-submits the prompt once Lens Assistant is ready.
   Tool approvals remain in Lens Assistant.
2. **Full issue page.** `welcome/issues` is retained for overflow, filtering and
   pagination; Welcome shows at most eight rows.
3. **Provider persistence.** The first compatible provider is preselected; the
   last-used provider is not persisted.
4. **Welcome-specific API.** `/api/v1/welcome/*` replaces the dashboard
   composition routes. The response contains only the issue envelope and scope.
5. **Greeting source.** The greeting uses only the authenticated session
   username; no additional profile-directory read is added.
