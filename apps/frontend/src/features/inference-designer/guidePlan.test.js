import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveGuidePlan, guideVariables } from './guidePlan.js';
const base = { id: 'optimized-baseline', variables: { GUIDE_NAME: 'optimized-baseline', ACCELERATOR_TYPE: { default: 'fixture' }, MODEL_SERVER: { default: 'engine' }, INFRA_PROVIDER: { default: 'base' } }, steps: { deploy: { values: [{ run: 'export VALUES="${REPO_ROOT}/guides/${GUIDE_NAME}/router/values.yaml"' }], standalone: [{ run: 'helm install ${GUIDE_NAME} ${ROUTER_STANDALONE_CHART} -f ${REPO_ROOT}/guides/recipes/router/base.values.yaml -f ${VALUES}' }], modelserver: [{ run: 'kubectl apply -k ${REPO_ROOT}/guides/${GUIDE_NAME}/modelserver/${ACCELERATOR_TYPE}/${MODEL_SERVER}/${INFRA_PROVIDER}' }] } } };
const catalog = { environment: { ROUTER_STANDALONE_CHART: 'oci://example.test/llm-d-router-standalone', ROUTER_CHART_VERSION: '1.2.3' }, guides: [base, { id: 'workload-autoscaling/keda-epp', variables: { SIGNAL: { default: 'queue', values: ['queue', 'saturation'] } }, steps: { deploy: { queue: [{ when: { SIGNAL: ['queue'] }, run: 'kubectl apply -k SOURCE/guides/scaling/queue' }], saturation: [{ when: { SIGNAL: ['saturation'] }, run: 'kubectl apply -k SOURCE/guides/scaling/saturation' }] } } }], assets: [{ path: 'guides/optimized-baseline/modelserver/fixture/engine/base', kind: 'kustomize' }, { path: 'guides/optimized-baseline/router/values.yaml', kind: 'values' }, { path: 'guides/recipes/router/base.values.yaml', kind: 'values' }, { path: 'guides/scaling/queue', kind: 'kustomize' }, { path: 'guides/scaling/saturation', kind: 'kustomize' }] };
test('Guide closure includes serving dependency, source router layers and reasons', () => {
    const plan = resolveGuidePlan(catalog, { guides: ['workload-autoscaling/keda-epp'] });
    assert.deepEqual(plan.errors, []);
    assert.deepEqual(plan.guides, ['optimized-baseline', 'workload-autoscaling/keda-epp']);
    assert.equal(plan.units.length, 2);
    assert.match(plan.reasons['optimized-baseline'][0], /scales/);
    assert.equal(plan.variables['optimized-baseline'].MONITORING, 'true');
    assert.equal(plan.router.release, 'optimized-baseline');
    assert.equal(plan.router.values[0].path, 'guides/recipes/router/base.values.yaml');
    assert.equal(plan.router.version, '1.2.3');
});
test('Guide variables select exactly one branch and removing the request removes dependencies', () => {
    const plan = resolveGuidePlan(catalog, { guides: ['workload-autoscaling/keda-epp'], variables: { 'workload-autoscaling/keda-epp': { SIGNAL: 'saturation' } } });
    assert.ok(plan.units.some(unit => unit.path.endsWith('/saturation')));
    assert.ok(!plan.units.some(unit => unit.path.endsWith('/queue')));
    assert.deepEqual(resolveGuidePlan(catalog, { guides: [] }).units, []);
});
test('nested shell defaults are resolved without evaluating commands or growing self references', () => {
    const values = guideVariables(catalog, { variables: { BASE: { default: '${BASE:-${GUIDE:-optimized-baseline}}' }, GUIDE: 'agentic-api', UNSAFE: '$(touch /tmp/not-executed)' } });
    assert.equal(values.BASE, 'optimized-baseline');
    assert.equal(values.UNSAFE, undefined);
});
test('missing source paths and incompatible hardware fail before rendering', () => {
    const plan = resolveGuidePlan(catalog, { guides: ['optimized-baseline'], accelerator: 'unknown' });
    assert.match(plan.errors.join(' '), /unavailable/);
    const hardwareCatalog = { ...catalog, guides: [{ ...base, support: { accelerators: { fixture: { engines: { engine: 'unsupported' } } } } }] };
    assert.match(resolveGuidePlan(hardwareCatalog, { guides: ['optimized-baseline'] }).errors.join(' '), /not supported/);
});
test('source prescribed relabeling stays scoped to the originating resource', () => {
    const flow = { id: 'flow-control', variables: { GUIDE_NAME: 'flow-control' }, steps: { deploy: { modelserver: [{ run: 'kubectl kustomize SOURCE/guides/optimized-baseline/modelserver/fixture/engine/base | sed "s/optimized-baseline/${GUIDE_NAME}/g" | kubectl apply -f -' }] } } };
    const plan = resolveGuidePlan({ ...catalog, guides: [...catalog.guides, flow] }, { guides: ['flow-control'] });
    assert.deepEqual(plan.replacements, [{ from: 'optimized-baseline', to: 'flow-control', paths: ['guides/optimized-baseline/modelserver/fixture/engine/base'] }]);
});

test('profile input resolves dependent accelerator variables before source paths are chosen', () => {
    const values = guideVariables(catalog, { variables: { ACCELERATOR_TYPE: { default: 'source-default' }, ACCELERATOR: { default: '${ACCELERATOR_TYPE}' } } }, { ACCELERATOR_TYPE: 'profile-choice' });
    assert.equal(values.ACCELERATOR_TYPE, 'profile-choice');
    assert.equal(values.ACCELERATOR, 'profile-choice');
});

test('all autoscaling topologies derive their serving and monitoring dependencies', () => {
    const scaling = { id: 'workload-autoscaling/keda-epp-token-aware', variables: { TOPOLOGY: 'optimized-baseline' }, steps: { deploy: { apply: [{ run: 'kubectl apply -k SOURCE/guides/scaling/queue' }] } } };
    const plan = resolveGuidePlan({ ...catalog, guides: [...catalog.guides, scaling] }, { guides: [scaling.id] });
    assert.deepEqual(plan.errors, []);
    assert.equal(plan.variables['optimized-baseline'].MONITORING, 'true');
    assert.ok(plan.guides.includes('optimized-baseline'));
});
test('Kueue source patches resolve named existing targets without changing cluster state', () => {
    const guide = { id: 'workload-autoscaling/kueue-rebalancing', variables: { DEPLOYMENT_A: 'model-a', DEPLOYMENT_B: 'model-b' }, steps: { deploy: [{ run: "kubectl patch deployment ${DEPLOYMENT_A} ${DEPLOYMENT_B} -n ${NAMESPACE} --type=merge -p 'spec: {strategy: {rollingUpdate: {maxSurge: 0, maxUnavailable: 1}}}'" }] } };
    const plan = resolveGuidePlan({ ...catalog, guides: [guide] }, { guides: [guide.id], namespace: 'models' });
    assert.deepEqual(plan.errors, []);
    assert.deepEqual(plan.workloadPatches.map(patch => patch.name), ['model-a', 'model-b']);
    assert.equal(plan.workloadPatches[0].namespace, 'models');
});
test('cached model identity replaces source model variables and categories cannot masquerade as recipes', () => {
    const guide = { ...base, variables: { ...base.variables, MODEL: 'source/model' } };
    const plan = resolveGuidePlan({ ...catalog, guides: [guide] }, { guides: [guide.id], model: 'cached/model' });
    assert.equal(plan.variables[guide.id].MODEL, 'cached/model');
    const category = resolveGuidePlan({ ...catalog, guides: [{ id: 'models' }] }, { guides: ['models'] });
    assert.match(category.errors.join(' '), /category/);
});

test('add-ons reuse a selected topology without requiring explicit companion selection', () => {
    const pd = { ...base, id: 'pd-disaggregation', variables: { ...base.variables, GUIDE_NAME: 'pd-disaggregation' } };
    const scaler = { id: 'workload-autoscaling/keda-epp-token-aware', variables: { TOPOLOGY: { default: 'optimized-baseline', values: ['optimized-baseline', 'pd-disaggregation'] } }, steps: { deploy: [] } };
    const agentic = { id: 'agentic-api', variables: { BASE_GUIDE_NAME: { default: 'pd-disaggregation' } }, steps: { deploy: [] } };
    const current = { ...catalog, guides: [base, pd, scaler, agentic] };
    const plan = resolveGuidePlan(current, { guides: ['pd-disaggregation', scaler.id] });
    assert.equal(plan.variables[scaler.id].TOPOLOGY, 'pd-disaggregation');
    assert.ok(!plan.guides.includes('optimized-baseline'));
    const api = resolveGuidePlan(current, { guides: ['optimized-baseline', 'agentic-api'] });
    assert.equal(api.variables['agentic-api'].BASE_GUIDE_NAME, 'optimized-baseline');
    assert.ok(!api.guides.includes('pd-disaggregation'));
});

test('cluster accelerator controls dependency YAML paths despite source defaults and stale overrides', () => {
    for (const accelerator of ['cluster-a', 'cluster-b']) {
        const guide = structuredClone(base);
        guide.steps.deploy.values.unshift({ run: 'export ACCELERATOR_TYPE="fixture"' });
        const current = { ...catalog, guides: [guide, catalog.guides[1]], assets: [...catalog.assets, { path: `guides/optimized-baseline/modelserver/${accelerator}/engine/base`, kind: 'kustomize' }] };
        const plan = resolveGuidePlan(current, { guides: ['workload-autoscaling/keda-epp'], accelerator, variables: { 'optimized-baseline': { ACCELERATOR_TYPE: 'stale' } } });
        assert.deepEqual(plan.errors, []);
        assert.equal(plan.variables['optimized-baseline'].ACCELERATOR_TYPE, accelerator);
        assert.ok(plan.units.some(unit => unit.path === `guides/optimized-baseline/modelserver/${accelerator}/engine/base`));
        assert.ok(!plan.units.some(unit => unit.path.includes('/modelserver/fixture/')));
    }
});

const commentedDeployment = () => {
    const fixture = structuredClone(catalog);
    fixture.guides = [structuredClone(base)];
    fixture.guides[0].steps.deploy.modelserver = [{
        when: { ACCELERATOR_TYPE: ['fixture'] },
        run: '# Comment out the above `kubectl apply` and uncomment the below to run on the selected accelerator\n#\n# kubectl apply -n ${NAMESPACE} \\\n#  -k ${REPO_ROOT}/guides/${GUIDE_NAME}/modelserver/${ACCELERATOR_TYPE}/${MODEL_SERVER}/${INFRA_PROVIDER}/\n#',
    }];
    fixture.guides[0].steps.deploy.monitoring = [{ run: 'kubectl apply -k SOURCE/guides/recipes/modelserver/components/monitoring' }];
    fixture.assets.push({ path: 'guides/recipes/modelserver/components/monitoring', kind: 'component' });
    return fixture;
};
test('explicit commented conditional deployment includes serving alongside monitoring', () => {
    const plan = resolveGuidePlan(commentedDeployment(), { guides: ['optimized-baseline'], accelerator: 'fixture' });
    assert.deepEqual(plan.errors, []);
    assert.ok(plan.units.some(unit => unit.path === 'guides/optimized-baseline/modelserver/fixture/engine/base'));
    assert.equal(plan.units.length, 2);
});
test('inactive commented branches cannot be replaced by monitoring', () => {
    const plan = resolveGuidePlan(commentedDeployment(), { guides: ['optimized-baseline'], accelerator: 'another-profile' });
    assert.equal(plan.units.length, 1);
    assert.match(plan.errors.join(' '), /do not provide a model workload/);
});
test('ordinary comments and unsupported alternative commands remain inactive', () => {
    for (const replacement of [
        run => run.replace('uncomment the below', 'example only'),
        run => run.replace('kubectl apply -n ${NAMESPACE}', 'kubectl delete -n ${NAMESPACE}'),
    ]) {
        const fixture = commentedDeployment();
        const step = fixture.guides[0].steps.deploy.modelserver[0];
        step.run = replacement(step.run);
        const plan = resolveGuidePlan(fixture, { guides: ['optimized-baseline'] });
        assert.match(plan.errors.join(' '), /do not provide a model workload/);
    }
});
test('documented alternative still requires an existing source asset', () => {
    const fixture = commentedDeployment();
    fixture.assets = fixture.assets.filter(asset => !asset.path.includes('/fixture/'));
    const plan = resolveGuidePlan(fixture, { guides: ['optimized-baseline'] });
    assert.match(plan.errors.join(' '), /component .* is unavailable/);
});
