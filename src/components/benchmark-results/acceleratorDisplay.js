import { aicSystemForHardware, profileForKey, profileForHardware, imageRepository, managedImageProfile } from '../../features/hardware/profiles.js';
// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

// Map a benchmark run's accelerator profile/runtime to a display label so chart
// titles describe the hardware that actually ran (XPU, GPU, or a neutral
// fallback) instead of a hardcoded "GPU / XPU".

export function acceleratorValue(record = {}) {
  return (
    record.metrics?.accelerator_profile ||
    record.accelerator_profile ||
    record.configuration?.accelerator ||
    record.spec?.accelerator ||
    record.deployment_configuration?.content?.accelerator ||
    record.resource_snapshot?.accelerator ||
    null
  );
}

let registeredProfiles = [];
export const DEFAULT_RUNTIME_IMAGES = {};
export const MANAGED_RUNTIME_IMAGE_REPOSITORIES = [];

export function setHardwareProfiles(profiles) {
  registeredProfiles = profiles || [];
  for (const key of Object.keys(DEFAULT_RUNTIME_IMAGES)) delete DEFAULT_RUNTIME_IMAGES[key];
  MANAGED_RUNTIME_IMAGE_REPOSITORIES.splice(0);
  for (const profile of registeredProfiles) {
    if (profile.upstream_variant && profile.deployment?.runtime_image) DEFAULT_RUNTIME_IMAGES[profile.upstream_variant] = profile.deployment.runtime_image;
    MANAGED_RUNTIME_IMAGE_REPOSITORIES.push(...(profile.deployment?.managed_image_repositories || []));
  }
}

export function acceleratorDisplayLabel(value) {
  const key = typeof value === 'string' ? value : value?.id || value?.name || '';
  return profileForKey(registeredProfiles, key)?.ui?.labels?.metric_group || null;
}

export function utilizationLabel(value) {
  const label = acceleratorDisplayLabel(value);
  return label ? `${label} utilization` : 'Accelerator utilization';
}

export const runtimeImageRepository = imageRepository;
export function isDefaultRuntimeImage(image) { return Boolean(managedImageProfile(registeredProfiles, image)); }
export function acceleratorVariantForHardware(hardware) { return profileForHardware(registeredProfiles, hardware)?.upstream_variant || null; }
export function aicSystemNameForHardware(hardware) {
  return aicSystemForHardware(registeredProfiles, hardware);
}
export function runtimeImageForHardware(hardware) { return profileForHardware(registeredProfiles, hardware)?.deployment?.runtime_image || null; }
