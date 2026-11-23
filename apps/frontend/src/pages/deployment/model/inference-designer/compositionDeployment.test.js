import test from 'node:test';
import assert from 'node:assert/strict';
import yaml from 'js-yaml';
import { compositionDeployment, defaultInferenceEndpoint } from './compositionDeployment.js';

const documents = [
    { apiVersion: 'v1', kind: 'Namespace', metadata: { name: 'cache-space' } },
    { apiVersion: 'apps/v1', kind: 'Deployment', metadata: { name: 'worker', namespace: 'cache-space' }, spec: { template: { spec: { containers: [{ name: 'modelserver', args: ['Example/Model'], env: [{ name: 'ROUTER', value: 'router.cache-space.svc' }] }] } } } },
    { apiVersion: 'v1', kind: 'Service', metadata: { name: 'router', namespace: 'cache-space' }, spec: { ports: [{ name: 'http', port: 80 }] } },
];
const snapshot = (guidePlan = {}) => ({ target: { repository: '/source', accelerator: 'registered-profile' }, files: [
    { name: 'deployment.yaml', content: documents.map(doc => yaml.dump(doc)).join('---\n') },
    { name: 'provenance.json', content: JSON.stringify({ commit: 'a'.repeat(40), components: [{ path: 'guides/model' }], guidePlan, modelBinding: { namespace: 'cache-space', storageVolumeId: 'volume', pvcName: 'cache', model: 'Example/Model' } }) },
] });

test('composition reads generated workloads and services and flags existing-workload ownership', () => {
    const result = compositionDeployment(snapshot());
    assert.deepEqual(result.errors, []);
    assert.deepEqual(result.workloads.map(doc => doc.metadata.name), ['worker']);
    assert.deepEqual(result.services.map(service => service.value), ['router:80']);
    assert.equal(result.documents[1].metadata.namespace, 'cache-space');
    assert.match(compositionDeployment(snapshot({ existingPoolIntegration: true })).errors.join(' '), /existing workloads/);
});

test('default inference endpoint prefers the traffic entry over EPP and metrics ports', () => {
    const endpoints = [
        { value: 'model-epp:9002', serviceName: 'model-epp', port: 9002 },
        { value: 'model-epp:9090', serviceName: 'model-epp', port: 9090 },
        { value: 'model-router:80', serviceName: 'model-router', port: 80 },
    ];
    assert.equal(defaultInferenceEndpoint(endpoints), 'model-router:80');
    assert.equal(defaultInferenceEndpoint([{ value: 'only:8000', serviceName: 'only', port: 8000 }]), 'only:8000');
    assert.equal(defaultInferenceEndpoint(endpoints.slice(0, 2)), '');
    assert.equal(defaultInferenceEndpoint(endpoints.slice(0, 1)), '');
    assert.equal(defaultInferenceEndpoint(), '');
});

test('P/D Envoy traffic entry is selected despite sharing the EPP Service name', () => {
    const endpoints = [8000, 9002, 9090, 80].map(port => {
        const serviceName = port === 8000 ? 'pd-disaggregation-epp-model-servers' : 'pd-disaggregation-epp';
        return { value: `${serviceName}:${port}`, serviceName, port };
    });
    assert.equal(defaultInferenceEndpoint(endpoints), 'pd-disaggregation-epp:80');
    assert.equal(defaultInferenceEndpoint([{ value: 'metrics:80', serviceName: 'metrics', port: 80 }]), '');
});

test('gateway outranks inference model services and ambiguous entries require a choice', () => {
    const model = { value: 'inference-model:8000', serviceName: 'inference-model', port: 8000 };
    const gateway = { value: 'inference-gateway:80', serviceName: 'inference-gateway', port: 80 };
    assert.equal(defaultInferenceEndpoint([model, gateway]), gateway.value);
    assert.equal(defaultInferenceEndpoint([gateway, model]), gateway.value);
    const other = { value: 'other-router:80', serviceName: 'other-router', port: 80 };
    assert.equal(defaultInferenceEndpoint([gateway, other]), '');
});
