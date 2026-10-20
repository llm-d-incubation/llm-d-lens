/** Enumerate the actual rendered resource fields; no stale engine flag schema. */
export function sourceParameterLeaves(documents) {
    const leaves = [];
    const visit = (value, path) => {
        if (value && typeof value === 'object') {
            for (const [key, child] of Object.entries(value)) {
                if (['__proto__', 'constructor', 'prototype'].includes(key)) throw new Error('Unsafe parameter key.');
                visit(child, [...path, Array.isArray(value) ? Number(key) : key]);
            }
        } else if (['string', 'number', 'boolean'].includes(typeof value)) leaves.push({ path, key: path.join('.'), value, type: typeof value });
    };
    visit(documents, []);
    return leaves;
}
export function sourceParameterEdit(leaf, raw) {
    const value = leaf.type === 'number' ? Number(raw) : leaf.type === 'boolean' ? raw === 'true' : raw;
    if (leaf.type === 'number' && (!raw.trim() || !Number.isFinite(value))) throw new Error('Enter a finite number.');
    return { path: leaf.path, value };
}
