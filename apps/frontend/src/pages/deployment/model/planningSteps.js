// [label, backend phases, what runs internally, fallback estimate in seconds]
export const PLANNING_STEPS = [
    ['Check inputs and model cache', ['validation', 'input', 'requirements', 'workload'], 'Validates your request, workload limits and the cached model files.', 3],
    ['Read cluster capacity', ['facts'], 'Reads nodes, accelerator types, free devices and memory.', 4],
    ['Gather performance evidence', ['public_rag', 'configuration_cases', 'aic', 'tools', 'ai'], 'Collects public benchmarks, past configuration cases and AIConfigurator estimates. Agentic also asks the AI provider.', 10],
    ['Generate candidate topologies', ['generation', 'candidate_validation'], 'Builds replica, tensor-parallel and Prefill/Decode layouts that fit the model and free capacity.', 6],
    ['Score and select', ['scoring', 'deterministic_ranking', 'selection'], 'Ranks valid candidates against your objective and picks one.', 3],
    ['Compose Guides and check resources', ['resources'], 'Renders the pinned llm-d Guides (Helm and Kustomize) for each candidate, binds the model cache, then checks devices and memory. Usually the slowest step.', 45],
];
const STEP_HISTORY_KEY = 'lens-planning-step-seconds';
const stepIndex = phase => PLANNING_STEPS.findIndex(([, phases]) => phases.includes(phase));

function stepHistory() {
    try { return JSON.parse(localStorage.getItem(STEP_HISTORY_KEY) || '{}'); } catch { return {}; }
}

/** Per-step timing from received events (each stamped with `at`), with measured or fallback estimates. */
export function planningTimeline(events, now = Date.now()) {
    const history = stepHistory();
    const reached = Math.max(0, ...events.map(event => stepIndex(event.phase)));
    const starts = PLANNING_STEPS.map((_, index) => events.find(event => stepIndex(event.phase) === index)?.at);
    return PLANNING_STEPS.map(([label, , detail, fallback], index) => {
        const start = starts[index];
        const end = starts.slice(index + 1).find(Boolean) ?? (index < reached ? start : undefined);
        const seconds = start ? Math.max(0, Math.round(((end ?? now) - start) / 1000)) : null;
        return { label, detail, estimate: history[label] ?? fallback, measured: label in history, seconds,
            state: index < reached ? 'done' : index === reached ? 'active' : 'pending',
            events: events.filter(event => stepIndex(event.phase) === index) };
    });
}

/** Remember step durations so later runs show measured estimates. */
export function rememberPlanningDurations(events) {
    const history = stepHistory();
    for (const step of planningTimeline(events)) {
        if (step.seconds == null || step.state === 'pending') continue;
        history[step.label] = history[step.label] == null ? step.seconds : Math.round((history[step.label] + step.seconds) / 2);
    }
    try { localStorage.setItem(STEP_HISTORY_KEY, JSON.stringify(history)); } catch { /* Estimates are optional. */ }
}
