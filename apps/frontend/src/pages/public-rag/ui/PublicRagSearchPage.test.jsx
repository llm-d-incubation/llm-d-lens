import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import PublicRagSearchPage, { PublicRagSearchResults } from './PublicRagSearchPage.jsx';
import { publicRagSearchRequest, publicRagSourceUrl, searchPublicRagDocuments } from './publicRagBackend.js';
import { createLatestResourceRequest } from '../../../shared/lib/resourceLoader.js';
import { AsyncState } from '../../../shared/ui/AsyncState.jsx';
import { canAccessView } from '../../auth/permissions.js';
import { firstAccessibleView, resolveView } from '../../../app/routes/views.js';

function response(overrides = {}) {
    return {
        contract: 'benchmark-rag.search.v1',
        retrieval_mode: 'opensearch_lexical',
        retrieval_build_id: 'build-42',
        index_stale: false,
        affected_origins: [],
        candidate_count: 1,
        excluded_count: 0,
        truncated: false,
        results: [],
        ...overrides,
    };
}

test('search validates and normalizes explicit submissions and omits blank origins', () => {
    assert.deepEqual(publicRagSearchRequest({ question: '  cache  ' }), { question: 'cache', limit: 10 });
    assert.deepEqual(publicRagSearchRequest({ question: 'cache', origins: 'a, b\na, ', limit: '50' }), {
        question: 'cache', origins: ['a', 'b'], limit: 50,
    });
    for (const question of ['', '  ', 'x'.repeat(2049)]) {
        assert.throws(() => publicRagSearchRequest({ question }), /question/);
    }
    for (const limit of ['', 0, 51, 1.5, 'abc']) {
        assert.throws(() => publicRagSearchRequest({ question: 'cache', limit }), /limit/);
    }
});

test('transport posts JSON with cancellation, credentials and strict success parsing', async (t) => {
    const controller = new AbortController();
    const query = publicRagSearchRequest({ question: 'cache' });
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.equal(url, '/api/v1/benchmark-rag/search');
        assert.equal(options.method, 'POST');
        assert.equal(options.credentials, 'include');
        assert.equal(options.signal, controller.signal);
        assert.equal(options.headers.get('Content-Type'), 'application/json');
        assert.deepEqual(JSON.parse(options.body), query);
        return Response.json(response());
    });
    assert.deepEqual(await searchPublicRagDocuments(query, { signal: controller.signal }), response());
    globalThis.fetch = async () => new Response('not JSON');
    await assert.rejects(searchPublicRagDocuments(query), SyntaxError);
    globalThis.fetch = async () => Response.json({ results: [] });
    await assert.rejects(searchPublicRagDocuments(query), /Unexpected document search response/);
});

test('transport requires the exact contract discriminator and lexical retrieval mode', async (t) => {
    const fetch = t.mock.method(globalThis, 'fetch');
    for (const overrides of [
        { contract: undefined },
        { contract: undefined, contract_version: 'benchmark-rag.search.v1' },
        { contract: 'benchmark-rag.search.v2' },
        { contract: 'BENCHMARK-RAG.SEARCH.V1' },
        { retrieval_mode: 'hybrid' },
        { results: null },
    ]) {
        fetch.mock.mockImplementation(async () => Response.json(response(overrides)));
        await assert.rejects(searchPublicRagDocuments({ question: 'cache', limit: 10 }), /Unexpected document search response/);
    }
});

test('403 and 503 retain structured API errors rather than becoming empty results', async (t) => {
    const fetch = t.mock.method(globalThis, 'fetch');
    for (const status of [403, 503]) {
        fetch.mock.mockImplementation(async () => Response.json({
            detail: { code: 'rag_unavailable', message: 'Search unavailable', retryable: status === 503 },
            requestId: 'request-42',
        }, { status }));
        await assert.rejects(searchPublicRagDocuments({ question: 'cache', limit: 10 }), error => {
            assert.equal(error.status, status);
            assert.equal(error.code, 'rag_unavailable');
            assert.equal(error.retryable, status === 503);
            assert.match(error.message, /request-42/);
            return true;
        });
    }
});

test('latest-request lifecycle suppresses stale successes, errors and cancelled results', async () => {
    const requests = createLatestResourceRequest();
    const events = [];
    let resolveOld;
    let rejectOld;
    let oldSignal;
    const old = requests.run(signal => {
        oldSignal = signal;
        return new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; });
    }, { onSuccess: value => events.push(value), onError: error => events.push(error.message) });
    await requests.run(async () => 'new', { onSuccess: value => events.push(value) });
    assert.equal(oldSignal.aborted, true);
    resolveOld('stale');
    await old;
    const cancelled = requests.run(() => new Promise((resolve, reject) => { rejectOld = reject; }), {
        onError: error => events.push(error.message),
    });
    requests.cancel();
    rejectOld(new Error('late failure'));
    await cancelled;
    assert.deepEqual(events, ['new']);
    assert.equal(requests.pending, false);
});

test('initial page does not request data and presents explicit accessible search controls', (t) => {
    const fetch = t.mock.method(globalThis, 'fetch', () => { throw new Error('Unexpected automatic search'); });
    const html = renderToStaticMarkup(<PublicRagSearchPage />);
    assert.equal(fetch.mock.callCount(), 0);
    assert.match(html, /Document Search/);
    assert.match(html, /for="public-rag-question"/);
    assert.match(html, /<button(?=[^>]*type="submit")(?=[^>]*disabled)[^>]*>/);
    assert.match(html, /Search published documents/);
    assert.match(html, /aria-live="polite"/);
});

test('results render stale and empty states without hiding provenance', () => {
    const html = renderToStaticMarkup(<PublicRagSearchResults data={response({
        index_stale: true, affected_origins: ['origin-a'], excluded_count: 2, truncated: true,
    })} />);
    for (const text of ['Search index is stale', 'origin-a', 'No matching documents', 'build-42', 'Excluded: 2', 'Results truncated']) {
        assert.ok(html.includes(text), text);
    }
    assert.match(renderToStaticMarkup(<AsyncState loading />), /animate-spin/);
    const failure = renderToStaticMarkup(<AsyncState error={new Error('Backend offline')} onRetry={() => {}} />);
    assert.match(failure, /Backend offline/);
    assert.match(failure, /Retry/);
});

test('passages, headings and artifact references are inert text with complete citations', () => {
    const html = renderToStaticMarkup(<PublicRagSearchResults data={response({ results: [{
        evidence_id: 'evidence-1', chunk_id: 'chunk-1', title: '<img src=x onerror=alert(1)>',
        heading_path: ['<script>heading</script>'], text: '<script>alert(1)</script><a href="javascript:alert(1)">link</a>',
        matched_branches: ['body'], lexical_rank: 1, score: 0.8,
        citation: {
            origin_id: 'https://github.com/llm-d/llm-d', source_revision: 'rev-42', source_path: 'docs/cache guide.md',
            sha256: 'sha-42', artifact_uri: 'javascript:alert(2)', source_pointer: { start: 0, end: 42 },
            heading_path: ['Cache'], published_manifest_id: 'manifest-42',
        },
    }] })} />);
    assert.doesNotMatch(html, /<script>|<img|href="javascript:/);
    assert.match(html, /&lt;script&gt;alert/);
    for (const text of ['sha-42', 'rev-42', 'manifest-42', 'chunk-1', 'evidence-1', 'javascript:alert(2)', 'Lexical rank', 'Source pointer']) {
        assert.ok(html.includes(text), text);
    }
    assert.match(html, /https:\/\/github.com\/llm-d\/llm-d\/blob\/rev-42\/docs\/cache%20guide.md/);
    assert.match(html, /rel="noopener noreferrer"/);
});

test('source links reject arbitrary hosts, schemes and paths; encode revision and path segments', () => {
    const citation = { origin_id: 'https://github.com/llm-d/llm-d', source_revision: 'branch/name', source_path: 'docs/a #b.md' };
    assert.equal(publicRagSourceUrl(citation), 'https://github.com/llm-d/llm-d/blob/branch%2Fname/docs/a%20%23b.md');
    for (const origin_id of ['javascript:alert(1)', 'file:///etc/passwd', 'https://evil.test/repo/name', 'https://user@github.com/a/b', 'https://github.com/a/b?redirect=evil']) {
        assert.equal(publicRagSourceUrl({ ...citation, origin_id }), null);
    }
    for (const source_path of ['/etc/passwd', '../secret', 'docs/../secret', 'docs\\secret', '']) {
        assert.equal(publicRagSourceUrl({ ...citation, source_path }), null);
    }
});

test('Document Search route and navigation require GLOBAL permission, including wildcard grants', () => {
    assert.equal(resolveView('public-rag'), 'public-rag');
    assert.equal(canAccessView(['benchmark-rag:query:read'], 'public-rag'), false);
    assert.equal(canAccessView(['*:*:*'], 'public-rag', []), false);
    assert.equal(canAccessView([], 'public-rag', ['benchmark-rag:query:read']), true);
    assert.equal(canAccessView([], 'public-rag', ['benchmark-rag:*:read']), true);
    assert.equal(firstAccessibleView(view => canAccessView([], view, ['benchmark-rag:query:read'])), 'public-rag');
});
