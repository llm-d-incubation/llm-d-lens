import fs from 'node:fs';
import { setHardwareProfiles } from './acceleratorDisplay.js';
setHardwareProfiles(['intel_xpu', 'nvidia'].map(name => JSON.parse(fs.readFileSync(new URL(`../../../llm_d_bench/hardware/profiles/${name}.json`, import.meta.url), 'utf8'))));
// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0

import assert from 'node:assert/strict';
import test from 'node:test';

import { acceleratorValue, acceleratorDisplayLabel, utilizationLabel, acceleratorVariantForHardware, aicSystemNameForHardware, runtimeImageForHardware, DEFAULT_RUNTIME_IMAGES } from './acceleratorDisplay.js';

test('reads the accelerator profile from common run shapes', () => {
  assert.equal(acceleratorValue({ metrics: { accelerator_profile: 'intel-xpu' } }), 'intel-xpu');
  assert.equal(acceleratorValue({ configuration: { accelerator: 'cuda' } }), 'cuda');
  assert.equal(acceleratorValue({ resource_snapshot: { accelerator: { model: 'B60' } } }).model, 'B60');
});

test('labels known accelerators and leaves unknown ones neutral', () => {
  assert.equal(acceleratorDisplayLabel('intel-xpu'), 'XPU');
  assert.equal(acceleratorDisplayLabel('cuda'), 'GPU');
  assert.equal(acceleratorDisplayLabel({ id: 'intel-xpu' }), 'XPU');
  assert.equal(acceleratorDisplayLabel(''), null);
});

test('utilization label falls back to the neutral wording', () => {
  assert.equal(utilizationLabel('intel-xpu'), 'XPU utilization');
  assert.equal(utilizationLabel('cuda'), 'GPU utilization');
  assert.equal(utilizationLabel(null), 'Accelerator utilization');
});

test('picks the per-vendor runtime image from the cluster hardware', () => {
  const gpu = { accelerators: [{ id: 'nvidia', models: ['NVIDIA A100 80GB PCIe'] }] };
  const xpu = { accelerators: [{ id: 'intel', models: ['Intel Data Center GPU Flex B60'] }] };
  assert.equal(acceleratorVariantForHardware(gpu), 'gpu');
  assert.equal(acceleratorVariantForHardware(xpu), 'xpu');
  assert.equal(runtimeImageForHardware(gpu), DEFAULT_RUNTIME_IMAGES.gpu);
  assert.equal(runtimeImageForHardware(xpu), DEFAULT_RUNTIME_IMAGES.xpu);
  // A GPU cluster never falls back to the XPU image; unknown hardware is neutral.
  assert.notEqual(runtimeImageForHardware(gpu), DEFAULT_RUNTIME_IMAGES.xpu);
  assert.equal(runtimeImageForHardware({ accelerators: [] }), null);
  assert.equal(runtimeImageForHardware(null), null);
  assert.equal(aicSystemNameForHardware(gpu), 'a100_sxm');
  assert.equal(aicSystemNameForHardware(xpu), 'b60');
  assert.equal(aicSystemNameForHardware({ accelerators: [{ id: 'nvidia', model: 'Unknown GPU' }] }), null);
});
