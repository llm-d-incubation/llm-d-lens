import assert from 'node:assert/strict';
import test from 'node:test';
import { evaluationApi, resolveEvaluationTarget, evaluationPlanReview, confirmEvaluationPlans } from './client.js';

test('resumed review confirms exactly the captured plan digests', async (t) => {
    const workflow = { id: 'workflow', prepared_plans: [{ id: 'plan', digest: 'sha256:reviewed', cases: [], preflight: { status: 'pass' } }] };
    const review = evaluationPlanReview(workflow);
    assert.equal(review.plan.preflight.status, 'pass');
    t.mock.method(globalThis, 'fetch', async (_url, options) => {
        assert.deepEqual(JSON.parse(options.body), { plan_digests: { plan: 'sha256:reviewed' }, acknowledge_unknown: false });
        return Response.json({ status: 'queued' });
    });
    await confirmEvaluationPlans(review.workflow, false);
});

test('evaluation adapter preserves validation-array details and field labels', async (t) => {
    const detail = [{loc:['body','name'],msg:'Required'}];
    t.mock.method(globalThis, 'fetch', async () => Response.json({detail},{status:422}));
    await assert.rejects(evaluationApi('/test'), error => {
        assert.equal(error.message,'name: Required');
        assert.equal(error.status,422);
        assert.deepEqual(error.details,detail);
        return true;
    });
});


test('submission resolves the selected model service group id', () => {
    assert.equal(resolveEvaluationTarget({targetId: 'msg-1'}), 'msg-1');
    assert.throws(() => resolveEvaluationTarget({targetId: ''}), /Select an existing model service/);
});
