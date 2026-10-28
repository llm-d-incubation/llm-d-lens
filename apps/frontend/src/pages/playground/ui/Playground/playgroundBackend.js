import { loadClusters } from '../../../optimization';
import { streamSseRequest } from '../../../../shared/api/sse.js';

// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Reads Server-Sent Events emitted by POST /api/v1/playground/chat
// (llm_d_lens/playground/chat.py). fetch() is used instead of EventSource
// because EventSource cannot send a POST body.
// `pendingResume` (instead of `messages`) resumes a turn paused on a
// `confirm_required` event, once the user has approved or rejected the
// write/approve-tier tool call that paused it: { conversation, toolCallId,
// decision }, where `conversation` is exactly the resume blob that event
// carried. `readOnly` restricts the turn to read-tier Lens MCP tools only
// (used for voice-transcribed messages, see PlaygroundPage.jsx).
export async function streamPlaygroundChat({ deployment, messages, pendingResume, readOnly }, onEvent, signal) {
    return streamSseRequest(
        '/api/v1/playground/chat',
        pendingResume ? { deployment, pendingResume } : { deployment, messages, readOnly },
        onEvent,
        signal,
    );
}

// Clusters and cluster sessions are Prism-wide concepts (see
// llm_d_lens/cluster/router.py); reused as-is here so "pick a cluster, then
// pick a deployment on it" matches every other Prism workflow (e.g. Model
// Market) instead of inventing a parallel concept just for the Playground.
export async function listClusters() {
    const payload = await loadClusters();
    return payload.items || [];
}

export { openClusterSession } from '../../../optimization';
