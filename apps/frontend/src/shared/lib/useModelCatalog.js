import { useCallback, useEffect, useRef, useState } from 'react';
import { MODELS, catalogModel } from '@repo/shared/catalog';
import { requestJson } from '../api/httpClient';
import { usePolling } from './usePolling';

export const catalogUrl = '/api/v1/model-catalog';

export function saveCatalogModel(model, values) {
    return requestJson(model?.source === 'custom' ? `${catalogUrl}/${encodeURIComponent(model.id)}` : catalogUrl, {
        method: model?.source === 'custom' ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...values, ...(model?.source === 'custom' ? { version: model.version } : {}) }),
    });
}

export function deleteCatalogModel(model) {
    return requestJson(`${catalogUrl}/${encodeURIComponent(model.id)}?version=${model.version}`, { method: 'DELETE' });
}

export function useModelCatalog(enabled = true) {
    const [models, setModels] = useState(MODELS);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);
    const controller = useRef(null);
    const reload = useCallback(async () => {
        controller.current?.abort();
        const request = new AbortController();
        controller.current = request;
        try {
            const payload = await requestJson(catalogUrl, { signal: request.signal });
            if (!request.signal.aborted) { setModels(payload.items.map(catalogModel)); setError(''); }
        } catch (failure) {
            if (!request.signal.aborted) setError(`Unable to load model catalog: ${failure.message}`);
        } finally {
            if (!request.signal.aborted) setLoading(false);
        }
    }, []);
    useEffect(() => {
        if (enabled) reload();
        return () => controller.current?.abort();
    }, [enabled, reload]);
    usePolling(reload, { enabled, intervalMs: 15000 });
    return { models, categories: ['All', ...new Set(models.map(model => model.category))], loading, error, reload };
}
