/** Preserve names, hierarchy and order from the revision-owned public Guide index. */
export function officialGuideIndex(markdown, guides = []) {
    const known = new Set(guides.map(guide => guide.id));
    const result = [], headings = [];
    for (const [offset, line] of markdown.split('\n').entries()) {
        const heading = line.match(/^(#{2,3})\s+(.+)$/);
        if (heading) { headings.length = heading[1].length - 2; headings.push(heading[2]); }
        const match = line.match(/^\s*[*-]\s+\[([^\]]+)\]\(\.\/([^#)]+)\/README\.md(?:#[^)]*)?\)(.*)$/);
        if (!match || !known.has(match[2]) || result.some(item => item.id === match[2])) continue;
        result.push({ id: match[2], title: match[1], group: headings.join(' / '), description: match[3].replace(/^\s*-\s*/, '').trim(), source: `guides/${match[2]}/README.md`, indexLine: offset + 1 });
    }
    return result;
}

/** Category sub-plans stay nested; they never become top-level Guide entries. */
export function guideSubplans(catalog, id) {
    const guide = catalog.guides.find(item => item.id === id);
    if (guide?.disposition?.kind !== 'category') return [];
    return catalog.guides.filter(item => item.id.startsWith(`${id}/`) && item.disposition?.kind === 'recipe' && (Object.keys(item.steps?.deploy || {}).length || catalog.assets.some(asset => asset.guide === item.id && asset.kind === 'chart')));
}
