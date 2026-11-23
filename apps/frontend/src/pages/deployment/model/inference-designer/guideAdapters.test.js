import test from 'node:test';
import assert from 'node:assert/strict';
import { adaptMarkdownGuide, guideDisposition } from './guideAdapters.js';
test('Markdown async recipes retain chart commands and select queue backend declaratively', () => {
    const guide = adaptMarkdownGuide({ id: 'batch-serving/asynchronous-processing', steps: {}, instructions: '#### Step 3: Deploy Async Processor\n```bash\nexport NAMESPACE=upstream\nexport MQ_PROVIDER=gcp-pubsub\nhelm install processor oci://example/chart --set ${TARGET_KEY}=http://${IP}:80\n```\n## Testing\n```bash\nkubectl delete namespace upstream\n```' });
    assert.equal(guide.variables.MQ_PROVIDER.default, 'redis');
    assert.match(guide.steps.deploy.markdown[0].run, /helm install/);
    assert.doesNotMatch(guide.steps.deploy.markdown[0].run, /delete|export MQ_PROVIDER|export NAMESPACE/);
});
test('non-manifest workflows and existing operational guides are distinguished from deployable recipes', () => {
    for (const [id, kind] of [['models', 'category'], ['rl', 'external'], ['rollouts', 'operation'], ['optimized-baseline', 'recipe']]) assert.equal(guideDisposition({ id }, { assets: [] }).kind, kind);
});
