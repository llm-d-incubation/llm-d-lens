import { requestJson } from '../../../shared/api/httpClient';

function problemMessage(payload, status) {
    const detail = payload?.detail || payload?.error;
    const base = typeof detail === "string"
        ? detail
        : Array.isArray(detail)
            ? detail.map((item) => {
                const location = Array.isArray(item?.loc) ? item.loc.filter((part) => part !== "body").join(" → ") : "";
                return `${location ? `${location}: ` : ""}${item?.msg || "Invalid request"}`;
            }).join("; ")
            : detail?.message || `Request failed (${status})`;
    return payload?.requestId ? `${base} (ref ${payload.requestId})` : base;
}

export function evaluationApi(path, options = {}) {
    return requestJson(path, { headers: { 'Content-Type': 'application/json' }, ...options }, {
        errorFactory: (payload, response) => {
            const error = new Error(problemMessage(payload, response.status));
            error.status = response.status;
            error.details = payload?.detail || payload?.error;
            return error;
        },
    });
}

export const listEvaluationWorkflows = () => evaluationApi("/api/v1/evaluate/workflow-runs");
export const listBenchmarkRuns = () => evaluationApi("/api/v1/evaluate/runs");

export function createEvaluation(request) {
    return evaluationApi("/api/v1/evaluate/evaluations", { method: "POST", body: JSON.stringify(request) });
}

export function confirmEvaluationPlans(workflow, acknowledgeUnknown) {
    return evaluationApi(`/api/v1/evaluate/evaluations/${encodeURIComponent(workflow.id)}/confirm`, {
        method: 'POST', body: JSON.stringify({ plan_digests: Object.fromEntries(workflow.prepared_plans.map(plan => [plan.id, plan.digest])), acknowledge_unknown: acknowledgeUnknown }),
    });
}

export function evaluationPlanReview(workflow) {
    const plans = workflow.prepared_plans || [];
    return { workflow, plan: {
        digest: plans.map(plan => plan.digest).join('\n'),
        cases: plans.flatMap(plan => plan.cases),
        preflight: {
            status: plans.some(plan => plan.preflight.status === 'fail') ? 'fail' : plans.some(plan => plan.preflight.status === 'unknown') ? 'unknown' : 'pass',
            errors: plans.flatMap(plan => plan.preflight.errors || []),
            unmodeled: [...new Set(plans.flatMap(plan => plan.preflight.unmodeled || []))],
        },
    } };
}

export function createBenchmarkRun(request) {
    return evaluationApi("/api/v1/evaluate/runs", { method: "POST", body: JSON.stringify(request) });
}

export function getEvaluationDetails(evaluationId) {
    return evaluationApi(`/api/v1/evaluate/workflow-runs/${encodeURIComponent(evaluationId)}/details`);
}

export function runEvaluationAction(run, action) {
    const collection = run.kind === "workflow" ? "workflow-runs" : "runs";
    const suffix = action === "delete" ? "" : `/${encodeURIComponent(action)}`;
    return evaluationApi(`/api/v1/evaluate/${collection}/${encodeURIComponent(run.id)}${suffix}`, {
        method: action === "delete" ? "DELETE" : "POST",
    });
}

export function refreshDeploymentCase(runId, caseId) {
    return evaluationApi(`/api/v1/deployments/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}/refresh`, { method: "POST" });
}

export function getDeploymentCaseLogs(runId, caseId) {
    return evaluationApi(`/api/v1/deployments/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}/logs`);
}

export function bindDeploymentClusterSession(runId, clusterSessionId) {
    return evaluationApi(`/api/v1/deployments/runs/${encodeURIComponent(runId)}/cluster-session`, {
        method: "POST",
        body: JSON.stringify({ cluster_session_id: clusterSessionId }),
    });
}

export function runDeploymentCaseAction(runId, caseId, action, { modelToken = "" } = {}) {
    const operation = action === "delete" ? "clean" : action;
    const body = action === "delete"
        ? { preserve_rendered_overlay: false }
        : action === "restart" ? { model_token: modelToken || null } : undefined;
    return evaluationApi(`/api/v1/deployments/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}/${encodeURIComponent(operation)}`, {
        method: "POST",
        body: body === undefined ? undefined : JSON.stringify(body),
    });
}

/** Resolve only discovered deployments; arbitrary URLs do not grant lifecycle ownership. */
// Existing-endpoint evaluation and simulation runs must target a published,
// continuously health-probed Model Service (not a raw deployment execution,
// whose own EPP has no liveness gating) — see docs/design for the rationale.
export function resolveEvaluationTarget({ targetId }) {
    if (!targetId) throw new Error('Select an existing model service.');
    return targetId;
}
