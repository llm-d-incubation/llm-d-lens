const cache = new Map();
const MAX_ENTRIES = 20;

export function welcomeCacheKey({ userId, authzVersion, clusterId }) {
    return `${userId || 'anonymous'}:${authzVersion ?? 0}:${clusterId || '*'}`;
}

export function readWelcomeOverview(key) {
    return cache.get(key)?.payload || null;
}

export function writeWelcomeOverview(key, payload) {
    cache.set(key, { payload, storedAt: Date.now() });
    if (cache.size > MAX_ENTRIES) {
        const oldest = [...cache.entries()].sort((left, right) => left[1].storedAt - right[1].storedAt);
        for (const [entryKey] of oldest.slice(0, cache.size - MAX_ENTRIES)) {
            cache.delete(entryKey);
        }
    }
}

export function clearWelcomeOverviewCache() {
    cache.clear();
}
