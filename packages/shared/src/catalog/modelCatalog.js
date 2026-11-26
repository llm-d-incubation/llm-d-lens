// Built-in catalog seeds shared with the backend. Custom entries come from the catalog API.
import metadata from './modelCatalogMetadata.json' with { type: 'json' };

import entries from './modelCatalogEntries.json' with { type: 'json' };

const categoryFor = (modelType) => {
    if (modelType === 'feature-extraction') return 'Encoder';
    if (modelType === 'text-ranking') return 'Rerank';
    if (modelType.includes('speech') || modelType.includes('audio')) return 'Speech';
    if (modelType.includes('image') || modelType.includes('video')) return 'Vision';
    return 'Text';
};

export function catalogModel(entry) {
    const details = entry.source === 'custom' ? {} : metadata[entry.repository] || {};
    const weightGiB = entry.weightGiB ?? details.weightGiB;
    const contextLength = entry.contextLength ?? details.contextLength;
    return {
        ...entry,
        id: entry.id || entry.repository.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''),
        name: entry.name || entry.repository.split('/').at(-1),
        family: entry.repository.split('/')[0],
        revision: entry.revision || 'main',
        version: entry.version || 1,
        source: entry.source || 'builtin',
        category: categoryFor(entry.modelType),
        sizeGiB: weightGiB || 0, weightGiB, contextLength,
        context: contextLength ? String(contextLength) : 'Not listed',
        toolParser: entry.servingDefaults?.toolCalling ? entry.servingDefaults.toolParser : '',
        vllmSupported: true,
    };
}

export const MODELS = entries.map(catalogModel);

export function defaultServing(model) {
    return { toolCalling: false, toolParser: '', reasoningParser: '', chatTemplate: '', dtype: '', extra: '', ...model?.servingDefaults };
}

export const CATEGORIES = ['All', ...new Set(MODELS.map((model) => model.category))];

// Strip a HuggingFace revision suffix ("org/repo@revision" -> "org/repo") and
// normalize case so lookups are forgiving about how the repo id was typed.
function normalizeRepository(repository) {
    return String(repository || '').split('@')[0].trim().toLowerCase();
}

const MODELS_BY_REPOSITORY = new Map(MODELS.map((model) => [normalizeRepository(model.repository), model]));
const MODELS_BY_ID = new Map(MODELS.map((model) => [model.id, model]));

// Look up a catalog entry by HuggingFace repository id (or model-catalog id
// as a fallback). Returns undefined when the model isn't in the catalog.
export function findModelByRepository(repository) {
    if (!repository) return undefined;
    return MODELS_BY_REPOSITORY.get(normalizeRepository(repository)) || MODELS_BY_ID.get(repository);
}

export function filterCatalogModels(models, { category = 'All', publisher = '', search = '', sort = 'newest' } = {}) {
    const words = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return models.filter(model => (category === 'All' || model.category === category)
        && (!publisher || model.family === publisher)
        && words.every(word => [model.name, model.family, model.repository, model.modelType, model.category, model.dtype]
            .some(value => String(value || '').toLowerCase().includes(word))))
        .sort((a, b) => {
            if (sort !== 'name') {
                const left = Date.parse(a.releaseDate), right = Date.parse(b.releaseDate);
                if (Number.isFinite(left) !== Number.isFinite(right)) return Number.isFinite(left) ? -1 : 1;
                if (Number.isFinite(left) && left !== right) return sort === 'oldest' ? left - right : right - left;
            }
            return a.name.localeCompare(b.name, undefined, { numeric: true }) || a.repository.localeCompare(b.repository);
        });
}
