/** Markdown recipes expose concrete source commands; categories are not deployment presets. */
export function guideDisposition(guide, catalog) {
    if (['models', 'batch-serving', 'workload-autoscaling'].includes(guide.id)) return { kind: 'category', reason: 'Choose one of the deployment recipes in this category.' };
    if (['no-kubernetes-deployment', 'rl'].includes(guide.id)) return { kind: 'external', reason: 'This workflow runs outside the Kubernetes manifest generator.' };
    if (guide.id === 'rollouts') return { kind: 'operation', reason: 'Rollouts require existing versions and traffic targets; this is an operational Guide.' };
    return { kind: 'recipe', reason: catalog.assets.some(asset => asset.guide === guide.id) ? '' : 'This Guide depends on external source components.' };
}

const shellBlocks = text => [...text.matchAll(/```(?:bash|sh|shell)\s*\n([\s\S]*?)```/g)].map(match => match[1]);
const section = (text, start, end) => {
    const offset = text.indexOf(start);
    if (offset < 0) return '';
    const stop = end ? text.indexOf(end, offset + start.length) : -1;
    return text.slice(offset, stop < 0 ? undefined : stop);
};

/** Adapt only the documented default path; source command bodies remain revision-owned. */
export function adaptMarkdownGuide(guide) {
    if (Object.keys(guide.steps?.deploy || {}).length) return guide;
    const text = guide.instructions || '';
    let blocks = [], variables = {};
    if (guide.id === 'coord-disaggregation') {
        blocks = [
            ...shellBlocks(section(text, '#### Single EPP (default)', '### 2. Deploy the Model Servers')),
            ...shellBlocks(section(text, '### 2. Deploy the Model Servers', '### 3.')),
            ...shellBlocks(section(text, '### 3. Deploy the Coordinator', '### 4.')).filter(block => block.includes('kustomize build')),
        ].filter(block => !block.includes('for ROLE') && !block.includes('httproute-3-epp') && !block.includes('kubectl patch'));
        variables = { GUIDE_NAME: guide.id, INFRA_PROVIDER: { default: 'base', values: ['base', 'coreweave', 'gke'] }, PROVIDER_NAME: { default: 'istio', values: ['istio', 'gke', 'na', 'agentgateway'] }, COORDINATOR_OVERLAY: { default: 'base', values: ['base', 'gke'] }, GATEWAY_ADDRESS: 'http://llm-d-inference-gateway-istio.${NAMESPACE}.svc:80' };
    }
    if (guide.id === 'batch-serving/asynchronous-processing') {
        blocks = shellBlocks(section(text, '#### Step 3: Deploy Async Processor', '## Testing')).map(block => block.split('\n').filter(line => !line.startsWith('[ ') && !line.startsWith('export NAMESPACE=') && !line.startsWith('export MQ_PROVIDER=')).join('\n'));
        variables = { MQ_PROVIDER: { default: 'redis', values: ['redis', 'gcp-pubsub'] }, IP: 'optimized-baseline-epp.${NAMESPACE}.svc', TARGET_KEY: 'ap.transportConfig.queues[0].igw_base_url' };
    }
    if (guide.id === 'batch-serving/batch-gateway') {
        blocks = shellBlocks(section(text, '### Step 5: Deploy', '**From Source:**'));
        variables = { INFERENCE_GW_URL: 'http://optimized-baseline-epp.${NAMESPACE}.svc:80' };
    }
    if (guide.id === 'multi-model-routing') {
        blocks = shellBlocks(section(text, '## Step 2: Create Model Mapping ConfigMaps', '## Step 4: Test the Deployment'));
    }
    if (!blocks.length) return guide;
    return { ...guide, variables: { ...guide.variables, ...variables }, steps: { ...guide.steps, deploy: { markdown: blocks.map(run => ({ run })) } } };
}

/** Picker entries require a deployment definition, not merely a README directory. */
export function deploymentGuideEntries(catalog) {
    return (catalog?.guides || []).filter(guide => guide.disposition?.kind === 'recipe' && (
        Object.keys(guide.steps?.deploy || {}).length > 0 ||
        (catalog.assets || []).some(asset => asset.guide === guide.id && asset.kind === 'chart')
    ));
}
