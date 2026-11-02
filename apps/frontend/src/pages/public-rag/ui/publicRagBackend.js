import { requestJson } from '../../../shared/api/httpClient.js';

export function publicRagSearchRequest({ question, origins = '', limit = 10 }) {
    const trimmed = question.trim();
    if (!trimmed || [...trimmed].length > 2048) {
        throw new Error('Enter a question between 1 and 2,048 characters.');
    }
    const count = Number(limit);
    if (!Number.isInteger(count) || count < 1 || count > 50) {
        throw new Error('Result limit must be an integer from 1 to 50.');
    }
    const selectedOrigins = [...new Set(origins.split(/[\n,]/).map(value => value.trim()).filter(Boolean))];
    return { question: trimmed, ...(selectedOrigins.length ? { origins: selectedOrigins } : {}), limit: count };
}

export async function searchPublicRagDocuments(query, { signal } = {}) {
    const data = await requestJson('/api/v1/benchmark-rag/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(query),
        signal,
    }, { strictJson: true, expectedStatus: 200 });
    if (data?.contract !== 'benchmark-rag.search.v1'
        || data?.retrieval_mode !== 'opensearch_lexical' || !Array.isArray(data?.results)) {
        throw new Error('Unexpected document search response.');
    }
    return data;
}

// Only a known public source host is linked. Artifact references remain inert.
export function publicRagSourceUrl(citation) {
    try {
        const origin = new URL(citation.origin_id);
        const repository = origin.pathname.match(/^\/([^/]+)\/([^/]+)\/?$/);
        if (origin.protocol !== 'https:' || origin.hostname !== 'github.com'
            || origin.port || origin.username || origin.password || origin.search || origin.hash || !repository) return null;
        const path = citation.source_path.split('/');
        if (!citation.source_revision || path.some(part => !part || part === '.' || part === '..' || part.includes('\\'))) return null;
        return `https://github.com/${encodeURIComponent(repository[1])}/${encodeURIComponent(repository[2])}/blob/${encodeURIComponent(citation.source_revision)}/${path.map(encodeURIComponent).join('/')}`;
    } catch {
        return null;
    }
}
