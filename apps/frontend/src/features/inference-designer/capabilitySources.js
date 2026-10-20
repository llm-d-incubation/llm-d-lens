import { nodeParameterPaths } from './deploymentSelection.js';

const terms = {
    prefix: /prefix.cach|radix.cach/i, chunked: /chunked.prefill|max.num.batched/i,
    parallel: /tensor.parallel|data.parallel|max.num.seqs/i,
    affinity: /prefix.cache.affinity/i, 'load-routing': /token.load.scorer/i,
    latency: /predicted.latency|latency.predict|ttftSource/i,
    index: /kv.events|indexer|precise.prefix/i, transfer: /kv.transfer|disaggregat/i,
    cache: /offload|connector|cpu/i, 'disk-cache': /filesystem|nvme|disk|local.disk/i,
    'p2p-cache': /peer|p2p/i, tracing: /tracing|otel/i,
    speculation: /speculat|mtp/i, 'wide-ep': /expert.parallel|data.parallel/i,
};
/** Links name their exact provenance: selected configuration, source example, or explanation. */
export function capabilitySources(entity, catalog, draft) {
    if (!entity || !/^[a-f0-9]{40}$/.test(catalog?.commit || '')) return [];
    const base = `https://github.com/llm-d/llm-d/blob/${catalog.commit}/`;
    const guide = catalog.guides.find(item => item.id === entity.guide);
    const match = terms[entity.id];
    const links = [];
    if (guide?.instructions) {
        const lines = guide.instructions.split('\n');
        const heading = lines.findIndex(line => /^#{2,6} /.test(line) && match?.test(line));
        const detail = heading < 0 ? lines.findIndex(line => match?.test(line)) : heading;
        const line = Math.max(0, detail) + 1;
        links.push({ label: detail >= 0 ? `Explanation · ${lines[heading >= 0 ? heading : 0].replace(/^#+\s*/, '')}` : `Guide overview · ${guide.title.replace(/[*`]/g, '')}`, href: `${base}guides/${guide.id}/README.md#L${line}` });
    }
    const paths = new Set(nodeParameterPaths(entity, draft));
    const selected = catalog.assets.filter(asset => paths.has(asset.path));
    const assets = selected.length ? selected : catalog.assets.filter(asset => asset.guide === entity.guide && (!match || match.test(asset.content || asset.path)));
    for (const asset of assets.slice(0, 2)) {
        const file = asset.file || asset.path.split('#')[0];
        if (!/\.(?:ya?ml|md)$/.test(file)) continue;
        const text = file.endsWith('.md') ? catalog.guides.find(item => file === `guides/${item.id}/README.md`)?.instructions : asset.content;
        const offset = text?.split('\n').findIndex(line => match?.test(line));
        links.push({ label: `${selected.length ? 'Selected configuration' : 'Source example'} · ${file.split('/').slice(-2).join('/')}`, href: `${base}${file}${offset >= 0 ? `#L${offset + 1}` : ''}` });
    }
    return links;
}
