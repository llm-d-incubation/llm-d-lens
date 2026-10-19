import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { z } from 'zod';
import { buildToolSource, jsonSchemaToZod } from './generate-mcp-tools.mjs';

const components = {
    schemas: {
        QueryRequest: {
            type: 'object', additionalProperties: false, required: ['question'],
            properties: {
                question: { type: 'string', minLength: 1, maxLength: 2048 },
                filters: {
                    type: 'object', additionalProperties: false,
                    properties: {
                        workload: { type: 'object', additionalProperties: {} },
                        value_min: { anyOf: [{ type: 'number' }, { type: 'string' }, { type: 'null' }] },
                    },
                },
                limit: { type: 'integer', minimum: 1, maximum: 50 },
            },
        },
        QueryResponse: {
            type: 'object', additionalProperties: false, required: ['contract', 'evidence'],
            properties: {
                contract: { type: 'string', const: 'benchmark-rag.query.v1' },
                evidence: {
                    type: 'array', items: {
                        type: 'object', additionalProperties: false, required: ['value', 'origin'],
                        properties: {
                            origin: { type: 'string', const: 'public' },
                            value: { anyOf: [{ type: 'string', pattern: '^-?[0-9]+(\\.[0-9]+)?$' }, { type: 'null' }] },
                        },
                    },
                },
            },
        },
    },
};
const operation = {
    operationId: 'query_public_benchmark_rag', description: 'Query published public benchmark evidence.',
    requestBody: { content: { 'application/json': { schema: { $ref: '#/components/schemas/QueryRequest' } } } },
    responses: { 200: { content: { 'application/json': { schema: { $ref: '#/components/schemas/QueryResponse' } } } } },
};
const source = buildToolSource('/api/v1/benchmark-rag/query', 'post', operation, components);

test('public query alone overrides POST to read and requires the canonical endpoint', () => {
    assert.match(source, /riskTier: "read"/);
    assert.match(source, /strictInput: true/);
    assert.match(source, /strictJsonRequest\("\/api\/v1\/benchmark-rag\/query", "POST", args,/);
    const ordinary = buildToolSource('/api/ordinary', 'post', { operationId: 'ordinary', description: 'Write.' }, {});
    assert.match(ordinary, /riskTier: "write"/);
    const approve = buildToolSource('/api/approve', 'post', { operationId: 'approve_agentic_plan', description: 'Deploy.' }, {});
    assert.match(approve, /riskTier: "approve"/);
    assert.throws(() => buildToolSource('/api/other', 'post', operation, components), /must be POST/);
    assert.throws(() => buildToolSource('/api/v1/benchmark-rag/query', 'post', { ...operation, responses: {} }, components), /response schema/);
});

test('query schemas preserve structured filters and Decimal strings, rejecting extra fields', async () => {
    let forwarded;
    const [tool] = vm.runInNewContext(`[${source}]`, {
        z,
        strictJsonRequest: async (path, method, args, schema) => {
            forwarded = { path, method, args };
            const response = { contract: 'benchmark-rag.query.v1', evidence: [{ origin: 'public', value: '1234567890.123456789' }] };
            assert.equal(schema.safeParse(response).success, true);
            assert.equal(schema.safeParse({ ...response, contract: 'wrong' }).success, false);
            assert.equal(schema.safeParse({ ...response, evidence: [{ origin: 'internal', value: '1' }] }).success, false);
            assert.equal(schema.safeParse({ ...response, evidence: [{ origin: 'public', value: 'corrupt' }] }).success, false);
            return response;
        },
    });
    const input = { question: 'throughput', filters: { workload: { batch: 8 }, value_min: '0.000000001' }, limit: 3 };
    const schema = z.strictObject(tool.inputShape);
    assert.equal(schema.safeParse(input).success, true);
    assert.equal(schema.safeParse({ ...input, principalId: 'admin' }).success, false);
    assert.equal(schema.safeParse({ ...input, filters: { sql: 'SELECT 1' } }).success, false);
    assert.equal(schema.safeParse({ ...input, limit: 51 }).success, false);
    assert.equal(schema.safeParse({ ...input, question: '' }).success, false);
    const result = await tool.handler(input);
    assert.equal(forwarded.args, input);
    assert.equal(result.evidence[0].value, '1234567890.123456789');
});

test('strict conversion is opt-in; ordinary schemas retain existing passthrough behavior', () => {
    assert.match(jsonSchemaToZod(components.schemas.QueryRequest, components), /\.passthrough\(\)/);
    assert.match(jsonSchemaToZod(components.schemas.QueryRequest, components, new Set(), true), /z.strictObject/);
});
