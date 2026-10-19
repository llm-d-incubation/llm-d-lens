import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import test, { type TestContext } from 'node:test';
import express from 'express';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { z } from 'zod';
import { strictJsonRequest } from '../../src/mcp/internal.ts';
import { runWithInternalAuth, verifyInternalHeaders } from '../../src/middleware/internalAuth.ts';
import { authMiddleware, permissionAnyGranted, permissionForNodeRoute } from '../../src/middleware/auth.ts';
import { createPrismMcpServer } from '../../src/mcp/server.ts';
import { mcpRouter } from '../../src/mcp/router.ts';
import { findTool, prismTools } from '../../src/mcp/tools.ts';
import { specialTools } from '../../src/mcp/specialTools.ts';
import { backendApiProxyRouter } from '../../src/proxy/backend-api-proxy.ts';

const path = '/api/v1/benchmark-rag/query';
const toolName = 'query_public_benchmark_rag';
function signedEnvironment(t: TestContext) {
    const previous = process.env.LENS_INTERNAL_AUTH_SECRET;
    process.env.LENS_INTERNAL_AUTH_SECRET = 'test-rag-secret';
    t.after(() => {
        if (previous === undefined) delete process.env.LENS_INTERNAL_AUTH_SECRET;
        else process.env.LENS_INTERNAL_AUTH_SECRET = previous;
    });
}
const request = {
    question: 'Published throughput?',
    intent: 'numeric',
    filters: { metric: 'throughput', unit: 'tokens/s', value_min: '0.000000001', workload: { batch_size: 8 } },
    limit: 3,
};
const response = {
    contract: 'benchmark-rag.query.v1', question: request.question, intent: 'numeric',
    filters: { ...request.filters, origins: ['prism:public-snapshot'], normalized: false, series: {} },
    status: 'answered', answer: 'Published measurement.', matched_items: 1, truncated: false,
    generation_mode: 'deterministic', internal_branch_enabled: false, warnings: [], abstentions: [],
    evidence: [{
        item_id: 'metric-1', origin: 'public', evidence_type: 'raw_json',
        citation: {
            origin_id: 'prism:public-snapshot', source_revision: 'abc123', source_path: 'public/result.json',
            sha256: 'a'.repeat(64), artifact_uri: 'archive://public/result.json',
            source_pointer: { json_pointer: '/throughput' }, heading_path: [], published_manifest_id: 'manifest-1',
        },
        metric: 'throughput', statistic: 'mean', value: '1234567890.123456789',
        unit: 'tokens/s', missing_series: [], related_experiments: [],
    }],
};

test('strict transport requires a signed caller, not an anonymous or disabled-auth sentinel', async (t) => {
    signedEnvironment(t);
    t.mock.method(globalThis, 'fetch', async () => assert.fail('unsigned request must not leave Node'));
    await assert.rejects(strictJsonRequest(path, 'POST', request, z.unknown()), /signed caller.*401/);
    process.env.LENS_INTERNAL_AUTH_SECRET = '';
    await assert.rejects(runWithInternalAuth('caller', () => strictJsonRequest(path, 'POST', request, z.unknown())), /signed caller.*401/);
});

test('query is one generated read tool, available under MCP/Playground policy but absent from Agentic planner', () => {
    const tool = findTool(toolName);
    assert.ok(tool, 'regenerate MCP tools after the REST operation is registered');
    assert.equal(tool.riskTier, 'read');
    assert.equal(tool.strictInput, true);
    assert.equal(prismTools.filter(item => item.name === toolName).length, 1);
    assert.equal(specialTools.some(item => item.name === toolName), false);
    const policy = permissionForNodeRoute('POST', '/api/mcp');
    assert.ok(policy);
    assert.equal(permissionAnyGranted(['mcp:tool:invoke'], policy), true);
    assert.equal(permissionAnyGranted(['playground:chat:use'], policy), true);
    assert.equal(permissionAnyGranted(['benchmark-rag:query:read'], policy), false);
    const planner = readFileSync(new URL('../../../backend/src/llm_d_lens/agentic/generator.py', import.meta.url), 'utf8');
    assert.equal(planner.includes(toolName), false);
});

test('HTTP MCP requires invocation policy AND upstream feature access, forwarding no spoofed identity', async (t) => {
    signedEnvironment(t);
    const envKeys = ['LLM_D_DEPLOY_API_URL', 'PRISM_MCP_INTERNAL_BASE_URL', 'PRISM_AUTH_MODE', 'PRISM_ALLOW_UNAUTHENTICATED', 'SIMULATION_ALLOW_UNAUTHENTICATED'];
    const previous = envKeys.map(key => [key, process.env[key]] as const);
    t.after(() => {
        for (const [key, value] of previous) {
            if (value === undefined) delete process.env[key];
            else process.env[key] = value;
        }
    });
    delete process.env.PRISM_AUTH_MODE;
    delete process.env.PRISM_ALLOW_UNAUTHENTICATED;
    delete process.env.SIMULATION_ALLOW_UNAUTHENTICATED;
    let queryCalls = 0;
    const upstream = http.createServer((req, res) => {
        const caller = verifyInternalHeaders(req.method || '', req.url || '', {
            principalId: String(req.headers['x-prism-principal-id'] || ''),
            timestamp: String(req.headers['x-prism-internal-ts'] || ''),
            signature: String(req.headers['x-prism-internal-sig'] || ''),
        });
        const userId = caller || /prism_session=(\w+)/.exec(req.headers.cookie || '')?.[1];
        res.setHeader('content-type', 'application/json');
        if (req.url === '/api/v1/auth/session') {
            if (!userId) {
                res.writeHead(401).end(JSON.stringify({ code: 'unauthenticated' }));
                return;
            }
            const permissions = userId === 'featureonly' ? ['benchmark-rag:query:read']
                : userId === 'playground' ? ['playground:chat:use']
                    : userId === 'both' ? ['mcp:tool:invoke', 'benchmark-rag:query:read'] : ['mcp:tool:invoke'];
            res.end(JSON.stringify({ userId, username: userId, permissions, reachableClusters: null, mustChangePassword: false }));
            return;
        }
        assert.equal(req.url, path);
        queryCalls++;
        assert.ok(caller, 'backend query must receive an actual HMAC-signed identity');
        if (caller !== 'both') {
            res.writeHead(403).end(JSON.stringify({ code: 'forbidden', evidence: response.evidence }));
            return;
        }
        res.end(JSON.stringify(response));
    });
    const app = express();
    app.use(express.json(), authMiddleware, mcpRouter, backendApiProxyRouter);
    const gateway = http.createServer(app);
    const listen = (server: http.Server) => new Promise<number>(resolve => {
        server.listen(0, '127.0.0.1', () => resolve((server.address() as { port: number }).port));
    });
    const upstreamPort = await listen(upstream);
    const gatewayPort = await listen(gateway);
    t.after(async () => {
        await Promise.all([new Promise<void>(resolve => upstream.close(() => resolve())), new Promise<void>(resolve => gateway.close(() => resolve()))]);
    });
    process.env.LLM_D_DEPLOY_API_URL = `http://127.0.0.1:${upstreamPort}`;
    process.env.PRISM_MCP_INTERNAL_BASE_URL = `http://127.0.0.1:${gatewayPort}`;
    const invoke = (session?: string) => fetch(`http://127.0.0.1:${gatewayPort}/api/mcp`, {
        method: 'POST',
        headers: {
            'content-type': 'application/json', accept: 'application/json, text/event-stream',
            'mcp-protocol-version': '2025-06-18',
            ...(session ? { cookie: `prism_session=${session}` } : {}),
            'x-prism-principal-id': 'both', 'x-prism-internal-ts': '1', 'x-prism-internal-sig': 'spoof',
        },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: toolName, arguments: request } }),
    });
    assert.equal((await invoke()).status, 401);
    assert.equal((await invoke('featureonly')).status, 403);
    assert.equal(queryCalls, 0);
    for (const session of ['invoker', 'playground']) {
        const denied = await invoke(session);
        const text = await denied.text();
        assert.equal(denied.status, 200);
        assert.match(text, /"isError":true/);
        assert.match(text, /status 403/);
        assert.equal(text.includes('metric-1'), false);
    }
    const allowed = await invoke('both');
    assert.match(await allowed.text(), /1234567890\.123456789/);
    const before = queryCalls;
    process.env.PRISM_AUTH_MODE = 'disabled';
    const disabled = await invoke();
    assert.match(await disabled.text(), /signed caller required/);
    assert.equal(queryCalls, before);
});

test('MCP forwards the real signed caller and exact JSON; denied/corrupt results are tool errors without evidence', async (t) => {
    signedEnvironment(t);
    let upstream: Response;
    let calls = 0;
    t.mock.method(globalThis, 'fetch', async (input, init) => {
        calls++;
        assert.equal(new URL(String(input)).pathname, path);
        assert.equal(init?.method, 'POST');
        const headers = new Headers(init?.headers);
        assert.equal(verifyInternalHeaders('POST', path, {
            principalId: headers.get('x-prism-principal-id') || undefined,
            timestamp: headers.get('x-prism-internal-ts') || undefined,
            signature: headers.get('x-prism-internal-sig') || undefined,
        }), 'actual-caller');
        assert.deepEqual(JSON.parse(String(init?.body)), request);
        return upstream;
    });
    const server = createPrismMcpServer();
    const client = new Client({ name: 'rag-focused-test', version: '1' });
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    await server.connect(serverTransport);
    await client.connect(clientTransport);
    t.after(async () => { await client.close(); await server.close(); });
    const listed = (await client.listTools()).tools.find(item => item.name === toolName);
    assert.ok(listed);
    assert.match(listed.description || '', /^\[read\]/);
    assert.equal(listed.inputSchema.additionalProperties, false);
    const call = (args = request) => runWithInternalAuth('actual-caller', () => client.callTool({ name: toolName, arguments: args }));

    upstream = Response.json(response);
    const success = await call();
    assert.notEqual(success.isError, true);
    assert.deepEqual(success.content, [{ type: 'text', text: JSON.stringify(response, null, 2) }]);

    const linkedFigure = {
        ...response,
        intent: 'figure',
        evidence: [{
            ...response.evidence[0],
            evidence_type: 'image_trend',
            coverage: 'partial',
            related_experiments: [{
                item_id: 'published-scenario-1', origin_id: 'prism:public-snapshot',
                model: 'published-model', hardware: null, runtime: null,
                workload: { batch_size: 8 }, configuration: null,
                source_pointer: { json_pointer: '/scenarios/0' },
            }],
        }],
    };
    upstream = Response.json(linkedFigure);
    const figure = await call();
    assert.notEqual(figure.isError, true);
    assert.deepEqual(figure.content, [{ type: 'text', text: JSON.stringify(linkedFigure, null, 2) }]);

    for (const status of [401, 403, 422, 503]) {
        upstream = Response.json({ ...response, code: 'forbidden' }, { status });
        const denied = await call();
        assert.equal(denied.isError, true);
        assert.match(JSON.stringify(denied), new RegExp(`status ${status}`));
        assert.equal(JSON.stringify(denied).includes('metric-1'), false);
    }
    for (const corrupt of [
        new Response('not-json'), Response.json(null), Response.json({}),
        Response.json({ ...response, generation_mode: 'agentic' }),
        Response.json({ ...response, internal_branch_enabled: true }),
        Response.json({ ...response, evidence: [{ ...response.evidence[0], origin: 'internal' }] }),
    ]) {
        upstream = corrupt;
        const result = await call();
        assert.equal(result.isError, true);
        assert.match(JSON.stringify(result), /Invalid upstream query response/);
        assert.equal(JSON.stringify(result).includes('metric-1'), false);
    }
    const before = calls;
    const injectedArgs = { ...request, principalId: 'admin' };
    const injected = await call(injectedArgs);
    assert.equal(injected.isError, true);
    assert.equal(calls, before);
});
