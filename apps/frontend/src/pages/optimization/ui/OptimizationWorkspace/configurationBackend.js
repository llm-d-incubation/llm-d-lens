// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import { createResourceLoader } from '../../../../shared/lib/resourceLoader';
import { postJson, requestJson } from '../../../../shared/api/httpClient';

async function postConfiguration(operation, body) {
    return postJson(`/api/v1/configurations/${operation}`, body);
}

export function renderConfiguration(candidateConfig, render = {}) {
    return postConfiguration('render', {
        candidate_config: candidateConfig,
        render: { ...render, format: 'manifest' },
    });
}

export function saveConfiguration(deployableConfiguration, name) {
    return postConfiguration('save', {
        deployable_configuration: deployableConfiguration,
        file: { name },
    });
}

export const loadConfigurationCapabilities = createResourceLoader(() =>
    requestJson('/api/v1/configurations/capabilities')
);

export async function loadConfigurationArtifacts() {
    const payload = await requestJson('/api/v1/configurations/artifacts', {}, { fallback: [] });
    return Array.isArray(payload) ? payload : [];
}

export async function deleteConfigurationArtifact(artifactId) {
    await requestJson(`/api/v1/configurations/artifacts/${encodeURIComponent(artifactId)}`, { method: 'DELETE' });
}

export function loadEstimateTools(options = {}) {
    return requestJson('/api/v1/estimate-tools', options, { strictJson: true });
}

export function checkEstimateSupport(payload, options = {}) {
    return postJson('/api/v1/estimate-tools/support', payload, options);
}

export function estimateConfiguration(payload, { artifactId, ...options } = {}) {
    const path = artifactId
        ? `/api/v1/estimate-tools/configurations/${encodeURIComponent(artifactId)}/estimate`
        : '/api/v1/estimate-tools/estimate';
    return postJson(path, payload, options);
}
