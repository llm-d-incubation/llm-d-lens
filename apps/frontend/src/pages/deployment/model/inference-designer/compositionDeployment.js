import yaml from 'js-yaml';

const CONTROL_PORTS = new Set([5557, 9002, 9003, 9090, 9400]);

/** Pick the traffic entry Service (gateway/router, HTTP port), never EPP or metrics, so users need not choose. */
export function defaultInferenceEndpoint(endpoints = []) {
    const score = item => {
        const name = String(item.serviceName || item.value || '').toLowerCase();
        const port = Number(item.port);
        if (/metrics|monitor/.test(name) || CONTROL_PORTS.has(port)
            || (/(^|-)epp($|-)/.test(name) && ![80, 443].includes(port))) return -Infinity;
        let value = 0;
        if (/gateway|router/.test(name)) value += 40;
        else if (/inference/.test(name)) value += 20;
        if ([80, 443, 8000, 8080].includes(port)) value += 10;
        return value;
    };
    const ranked = [...endpoints].sort((left, right) => score(right) - score(left));
    if (ranked[1] && ranked[1].value !== ranked[0].value && score(ranked[0]) === score(ranked[1])) return '';
    return ranked[0] && Number.isFinite(score(ranked[0])) && (score(ranked[0]) > 0 || endpoints.length === 1) ? ranked[0].value : '';
}

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
