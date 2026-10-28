import { streamSseRequest } from '../../../shared/api/sse.js';

export function streamAgenticRecommendation(payload, onEvent, signal) {
    return streamSseRequest('/api/v1/agentic-deployments/stream', payload, onEvent, signal);
}

export function streamAgenticRefinement(runId, payload, onEvent, signal) {
    return streamSseRequest(`/api/v1/agentic-deployments/${encodeURIComponent(runId)}/refine/stream`, payload, onEvent, signal);
}
