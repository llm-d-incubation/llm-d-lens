import yaml from 'js-yaml';

/** Read actual generated resources; never substitute a preset for the saved YAML. */
export function compositionDeployment(result) {
    const manifest = result.files.find(file => file.name === 'deployment.yaml');
    const provenance = JSON.parse(result.files.find(file => file.name === 'provenance.json')?.content || '{}');
    const documents = yaml.loadAll(manifest?.content || '').filter(Boolean);
    const services = documents.filter(doc => doc.kind === 'Service').flatMap(doc => (doc.spec?.ports || [])
        .filter(port => port.protocol !== 'UDP').map(port => ({ value: `${doc.metadata.name}:${port.port}`, serviceName: doc.metadata.name, port: port.port, protocol: port.name === 'https' ? 'https' : 'http' })));
    const workloads = documents.filter(doc => doc.kind === 'Deployment');
    const errors = [];
    if (!workloads.length) errors.push('This composition has no Deployment workload supported by managed deployment.');
    if (!services.length) errors.push('The generated YAML needs an inference Service before it can be deployed.');
    if (provenance.guidePlan?.existingModelIntegration || provenance.guidePlan?.existingPoolIntegration || provenance.guidePlan?.workloadPatches?.length) errors.push('This recipe modifies existing workloads. Download its YAML and manage it with the owning deployment.');
    return { documents, provenance, services, workloads, errors };
}

/** Bind generated namespaced resources to the existing isolated deployment lifecycle. */
export function compositionRenderRequest(result, endpointValue) {
    const { documents, provenance, services, workloads, errors } = compositionDeployment(result);
    if (errors.length) throw new Error(errors.join(' '));
    const endpoint = services.find(service => service.value === endpointValue);
    if (!endpoint) throw new Error('Choose the inference endpoint from the generated Services.');
    const binding = provenance.modelBinding;
    if (!binding?.storageVolumeId || !binding.model) throw new Error('Generate again with a ready cached model.');
    const namespace = binding.namespace;
    if (documents.some(doc => (doc.kind === 'Namespace' && doc.metadata?.name !== namespace) || (doc.metadata?.namespace && doc.metadata.namespace !== namespace))) throw new Error('Managed deployment requires all resources to use the selected namespace.');
    const rebind = value => typeof value === 'string'
        ? value === namespace ? '__PRISM_NAMESPACE__' : value.split(`.${namespace}.svc`).join('.__PRISM_NAMESPACE__.svc').split(`namespace="${namespace}"`).join('namespace="__PRISM_NAMESPACE__"').split(`--watch-namespace=${namespace}`).join('--watch-namespace=__PRISM_NAMESPACE__')
        : Array.isArray(value) ? value.map(rebind) : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([key, child]) => [key, rebind(child)])) : value;
    const managed = rebind(documents.filter(doc => doc.kind !== 'Namespace'));
    managed.forEach(doc => { delete doc.metadata.namespace; });
    return {
        candidate: { schema_version: '1.0', type: 'baseline', candidate_source: { name: 'manual' }, target: { model: binding.model },
            runtime: { modelServer: 'vllm', modelSource: 'cached-readonly', storageVolumeId: binding.storageVolumeId, modelPvcClaimName: binding.pvcName, namespaceLabels: documents.find(doc => doc.kind === 'Namespace')?.metadata?.labels || {} } },
        render: { guide_ref: 'source-composition', template_ref: 'source-composition/deployment.yaml',
            guide_source: { repository: result.target.repository, requestedRef: result.target.ref || provenance.commit, commit: provenance.commit, guide: 'source-composition', accelerator: result.target.accelerator, modelServer: 'vllm', variant: 'composition', files: provenance.components.map(component => component.path) },
            rendered_manifest: managed.map(doc => yaml.dump(doc, { noRefs: true, lineWidth: -1 })).join('---\n') },
        documents: managed,
        provenance,
        deployment: { readinessDeployments: workloads.map(doc => doc.metadata.name), endpoint: { protocol: endpoint.protocol, serviceName: endpoint.serviceName, port: endpoint.port } },
    };
}
