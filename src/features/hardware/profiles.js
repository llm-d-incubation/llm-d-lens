// Hardware identity and defaults shared by Node planning and browser consumers.
export function profileForKey(profiles, key) {
    return profiles.find(profile => [profile.id, profile.vendor, profile.benchmark_profile, profile.upstream_variant, ...(profile.accelerator_keys || [])].includes(key)) || null;
}

export function profileForHardware(profiles, hardware) {
    const identities = (hardware?.accelerators || []).map(item => item?.id || item);
    const matches = profiles.filter(profile => identities.some(key => profileForKey([profile], key)));
    return matches.length === 1 ? matches[0] : null;
}

export function aicSystemForHardware(profiles, hardware) {
    const profile = profileForHardware(profiles, hardware);
    if (!profile) return null;
    const accelerators = hardware?.accelerators || [];
    const identities = accelerators.flatMap(item => (
        typeof item === 'string' ? [item] : [item?.model, item?.name, item?.id, ...(item?.models || [])]
    )).filter(Boolean);
    for (const mapping of profile.planning?.aic_system_models || []) {
        try {
            if (identities.some(identity => new RegExp(mapping.pattern, 'i').test(String(identity)))) return mapping.system;
        } catch {
            // Invalid plugin patterns are ignored like other optional profile matchers.
        }
    }
    return null;
}

export function profileForResource(profiles, resource) {
    return profiles.find(profile => !(profile.monitor_resource_suffixes || []).some(suffix => resource.endsWith(suffix))
        && (profile.resource_prefixes || []).some(prefix => resource.startsWith(prefix))) || null;
}

export function imageRepository(image) {
    const value = String(image || '').split('@')[0];
    return value.slice(value.lastIndexOf('/') + 1).includes(':') ? value.slice(0, value.lastIndexOf(':')) : value;
}

export function managedImageProfile(profiles, image) {
    const repository = imageRepository(image);
    return profiles.find(profile => [profile.deployment?.runtime_image, ...(profile.deployment?.managed_image_repositories || [])].some(value => value && imageRepository(value) === repository)) || null;
}

export function overlayRoot(profile, guide, modelServer) {
    const template = profile?.deployment?.overlay_root;
    if (!template) throw new Error('Hardware profile has no deployment overlay root');
    return template.replaceAll('{guide}', guide).replaceAll('{variant}', profile.upstream_variant).replaceAll('{model_server}', modelServer).replace(/\/$/, '');
}

export function matchOverlayPath(profile, sourcePath) {
    const template = profile?.deployment?.overlay_root;
    if (!template) return null;
    const escape = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const parts = template.split(/(\{guide\}|\{variant\}|\{model_server\})/);
    const pattern = parts.map(part => part === '{guide}' ? '(?<guide>[^/]+)' : part === '{model_server}' ? '(?<modelServer>[^/]+)' : part === '{variant}' ? escape(profile.upstream_variant || '') : escape(part)).join('');
    const match = sourcePath.match(new RegExp(`^${pattern}/(?<relative>.+)$`));
    return match?.groups ? { guide: match.groups.guide, modelServer: match.groups.modelServer || template.split('/').at(-1), relative: match.groups.relative } : null;
}
