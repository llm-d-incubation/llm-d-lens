// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import assert from 'node:assert/strict';
import http from 'node:http';
import test, { after, before } from 'node:test';
import express from 'express';

import { monitoringRouter } from '../../src/routes/monitoring.ts';

let upstream: http.Server;
let proxy: http.Server;
let proxyUrl = '';
let delayMs = 0;
let lastHeaders: http.IncomingHttpHeaders = {};
let upstreamRequests = 0;

function listen(server: http.Server): Promise<number> {
    return new Promise((resolve) => {
        server.listen(0, '127.0.0.1', () => resolve((server.address() as { port: number }).port));
    });
}

before(async () => {
    upstream = http.createServer((req, res) => {
        upstreamRequests += 1;
        lastHeaders = req.headers;
        setTimeout(() => {
            res.writeHead(200, { 'content-type': 'application/json' });
            res.end('{"ok":true}');
        }, delayMs);
    });
    const upstreamPort = await listen(upstream);
    process.env.MONITORING_API_URL = `http://127.0.0.1:${upstreamPort}`;
    process.env.MONITORING_PROXY_TIMEOUT_MS = '100';
    process.env.LENS_INTERNAL_AUTH_SECRET = 'test-internal-secret';
    process.env.MONITORING_INSTALL_TIMEOUT_MS = '1000';

    const app = express();
    app.set('trust proxy', 'loopback');
    app.use(express.json());
    app.use((req, _res, next) => {
        (req as express.Request & { principal?: { userId: string } }).principal = { userId: 'user-1' };
        next();
    });
    app.use(monitoringRouter);
    proxy = http.createServer(app);
    const proxyPort = await listen(proxy);
    proxyUrl = `http://127.0.0.1:${proxyPort}`;
});

after(async () => {
    await new Promise((resolve) => upstream.close(resolve));
    await new Promise((resolve) => proxy.close(resolve));
});

test('read requests use the short monitoring timeout', async () => {
    delayMs = 300;
    const response = await fetch(`${proxyUrl}/api/v1/monitoring/gpu-driver/status?access_mode=dra`);
    assert.equal(response.status, 504);
    assert.deepEqual(await response.json(), { detail: 'Monitoring request timed out' });
});

test('forwards the resolved principal to the Python upstream', async () => {
    delayMs = 0;
    const response = await fetch(`${proxyUrl}/api/v1/monitoring/gpu-driver/status`);
    assert.equal(response.status, 200);
    assert.equal(lastHeaders['x-prism-principal-id'], 'user-1');
    assert.ok(lastHeaders['x-prism-internal-ts']);
    assert.ok(lastHeaders['x-prism-internal-sig']);
});

test('driver install uses the longer install timeout', async () => {
    delayMs = 300;
    const response = await fetch(`${proxyUrl}/api/v1/monitoring/gpu-driver/install`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ access_mode: 'plugin' }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true });
});

test('standalone monitoring router limits each client before forwarding to the upstream', async () => {
    delayMs = 0;
    const headers = { 'x-forwarded-for': '192.0.2.1' };
    const requestsBefore = upstreamRequests;
    for (let index = 0; index < 1000; index += 1) {
        const response = await fetch(`${proxyUrl}/api/v1/monitoring/gpu-driver/status`, { headers });
        assert.equal(response.status, 200);
        assert.equal(response.headers.get('ratelimit-limit'), '1000');
        assert.equal(response.headers.get('ratelimit-remaining'), String(999 - index));
        await response.text();
    }
    assert.equal(upstreamRequests, requestsBefore + 1000);
    for (const method of ['GET', 'POST']) {
        const response = await fetch(`${proxyUrl}/api/v1/monitoring/gpu-driver/install`, { method, headers });
        assert.equal(response.status, 429);
        assert.ok(Number(response.headers.get('retry-after')) > 0);
        await response.text();
    }
    assert.equal(upstreamRequests, requestsBefore + 1000);

    const otherClient = await fetch(`${proxyUrl}/api/v1/monitoring/gpu-driver/status`, {
        headers: { 'x-forwarded-for': '192.0.2.2' },
    });
    assert.equal(otherClient.status, 200);
    await otherClient.text();
    assert.equal(upstreamRequests, requestsBefore + 1001);
});
