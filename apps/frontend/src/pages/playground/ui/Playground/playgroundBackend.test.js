import assert from 'node:assert/strict';
import test from 'node:test';
import { listClusters, openClusterSession, streamPlaygroundChat } from './playgroundBackend.js';

test('cluster adapter preserves lists and both session ID spellings', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [{ id: 'a' }] }));
    assert.deepEqual(await listClusters(), [{ id: 'a' }]);
    for (const key of ['sessionId', 'session_id']) {
        t.mock.method(globalThis, 'fetch', async url => {
            assert.equal(new URL(url, 'http://local').searchParams.get('clusterId'), 'a/b');
            return Response.json({ [key]: 'session' });
        });
        assert.equal(await openClusterSession('a/b'), 'session');
    }
    t.mock.method(globalThis, 'fetch', async () => Response.json({}));
    await assert.rejects(openClusterSession('a'), /did not return an active session/);
});

test('chat remains streaming and preserves event delivery and request signal', async (t) => {
    const signal = new AbortController().signal;
    const encoder = new TextEncoder();
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.equal(url, '/api/playground/chat');
        assert.equal(options.signal, signal);
        assert.deepEqual(JSON.parse(options.body), { deployment: 'one', messages: [], readOnly: true });
        return new Response(new ReadableStream({
            start(controller) {
                controller.enqueue(encoder.encode('event: delta\ndata: {"text":"hel'));
                controller.enqueue(encoder.encode('lo"}\n\nevent: done\ndata: {}\n\n'));
                controller.close();
            },
        }));
    });
    const events = [];
    await streamPlaygroundChat({ deployment: 'one', messages: [], readOnly: true }, (...event) => events.push(event), signal);
    assert.deepEqual(events, [['delta', { text: 'hello' }], ['done', {}]]);
});

test('chat and confirmed tool resumption send the current CSRF cookie as a header', async (t) => {
    const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
    t.after(() => {
        if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument);
        else delete globalThis.document;
    });
    Object.defineProperty(globalThis, 'document', {
        configurable: true, writable: true, value: { cookie: 'other=value; prism_csrf=first%2Ftoken' },
    });
    const pendingResume = { conversation: [], toolCallId: 'call-1', decision: 'approve' };
    const requests = [
        { deployment: 'one', messages: [], readOnly: true },
        { deployment: 'one', pendingResume },
    ];
    let call = 0;
    t.mock.method(globalThis, 'fetch', async (url, options) => {
        assert.equal(url, '/api/playground/chat');
        assert.equal(options.method, 'POST');
        const headers = new Headers(options.headers);
        assert.equal(headers.get('X-Prism-CSRF'), call === 0 ? 'first/token' : 'rotated-token');
        assert.deepEqual(JSON.parse(options.body), requests[call++]);
        return new Response('event: done\ndata: {}\n\n');
    });
    await streamPlaygroundChat(requests[0], () => {});
    globalThis.document.cookie = 'prism_csrf=rotated-token';
    await streamPlaygroundChat(requests[1], () => {});
    assert.equal(call, 2);
});

test('chat surfaces structured authorization errors without retrying', async (t) => {
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({
        status: 403, detail: 'CSRF token is missing or does not match', code: 'csrf_failed',
    }, { status: 403 }));
    await assert.rejects(streamPlaygroundChat({ deployment: 'one', messages: [] }, () => {
        assert.fail('A rejected request must not deliver stream events');
    }), error => {
        assert.equal(error.status, 403);
        assert.equal(error.code, 'csrf_failed');
        assert.match(error.message, /CSRF token/);
        return true;
    });
    assert.equal(fetch.mock.callCount(), 1);
});

test('chat preserves cancellation errors', async (t) => {
    const failure = new DOMException('Aborted', 'AbortError');
    t.mock.method(globalThis, 'fetch', async () => { throw failure; });
    await assert.rejects(
        streamPlaygroundChat({ deployment: 'one', messages: [] }, () => {}),
        error => error === failure,
    );
});
