export async function verifyModelMarketCacheReturn(page) {
    await page.addInitScript(() => {
        const originalFetch = window.fetch.bind(window);
        window.__navigationTestVolumes = [{ id: 'model-cache', name: 'Model cache volume', status: 'ready', kind: 'local-disk', purpose: 'model-cache', clusterId: 'test-cluster' }];
        window.__navigationTestDownloaded = false;
        window.fetch = async (input, options) => {
            const url = new URL(typeof input === 'string' ? input : input.url, window.location.href);
            if (!url.pathname.startsWith('/api/')) return originalFetch(input, options);
            let body = { items: [] };
            if (url.pathname === '/api/v1/auth/session') {
                body = { username: 'navigation-test', permissions: ['*:*:*'] };
            } else if (url.pathname === '/api/cluster/clusters') {
                body = { items: [{ id: 'test-cluster', name: 'Navigation test cluster', ready: true }] };
            } else if (url.pathname === '/api/cluster/overview') {
                body = { kubernetes: { hardware: { gpuCount: 8, availableGpuCount: 8, vramBytes: 687194767360 } } };
            } else if (url.pathname === '/api/v1/storage/volumes') {
                body = { items: window.__navigationTestVolumes };
            } else if (url.pathname === '/api/v1/model-cache/entries') {
                body = { items: window.__navigationTestDownloaded ? [{ id: 'downloaded-model', status: 'ready', storageVolumeId: 'model-cache', clusterId: 'test-cluster', source: { kind: 'huggingface', huggingface: { repoId: 'meta-models/Muse-Glimmer-30B' } } }] : [] };
            } else if (url.pathname === '/api/v1/storage/volume-resource-status') {
                body = { items: {} };
            }
            return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
        };
    });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.getByRole('heading', { name: 'Select a model', exact: true }).waitFor();
    await page.locator('section button').first().click();
    const modelName = await page.locator('form h1').innerText();
    await page.getByLabel('Deployment name', { exact: true }).fill('storage-return-check');
    await page.getByLabel('Description', { exact: true }).fill('Preserve this model draft');
    await page.getByLabel('Replicas', { exact: true }).fill('2');
    await page.getByLabel('Tensor parallelism', { exact: true }).fill('3');

    const assertDraft = async () => {
        await page.getByLabel('Deployment name', { exact: true }).waitFor({ state: 'visible' });
        const draft = await page.evaluate(() => ({
            modelName: document.querySelector('form h1').textContent,
            fields: Object.fromEntries([...document.querySelectorAll('form label')].map(label => [
                label.firstChild.textContent.trim(),
                label.querySelector('input, textarea, select')?.value,
            ])),
        }));
        if (draft.modelName !== modelName) throw new Error('Selected model lost');
        for (const [label, expected] of [
            ['Deployment name', 'storage-return-check'],
            ['Description', 'Preserve this model draft'],
            ['Replicas', '2'],
            ['Tensor parallelism', '3'],
            ['Cluster', 'test-cluster'],
        ]) {
            if (draft.fields[label] !== expected) {
                throw new Error(`${label} was not preserved`);
            }
        }
    };

    await page.getByText('This model is not downloaded in the selected storage. Clear it or download the model there first.', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Manage model cache', exact: true }).click();
    const destination = await page.evaluate(() => new URL(window.location.href).searchParams.get('view'));
    if (destination !== 'model-cache') throw new Error('Expected Model Cache destination');
    await page.getByRole('heading', { name: 'Model cache', exact: true }).waitFor();
    await page.getByRole('button', { name: 'Back to model', exact: true }).waitFor();
    if (!await page.getByLabel('Deployment name', { exact: true }).isHidden()) {
        throw new Error('Model form should be hidden in Model Cache');
    }
    const assertDownloadDefaults = async () => {
        await page.getByRole('heading', { name: 'Download a model', exact: true }).waitFor();
        await page.locator('#model-cache-cluster option[value="test-cluster"]').waitFor({ state: 'attached' });
        await page.locator('#model-cache-volume option[value="model-cache"]').waitFor({ state: 'attached' });
        for (const [selector, expected] of [
            ['#model-cache-cluster', 'test-cluster'],
            ['#model-cache-repo', 'meta-models/Muse-Glimmer-30B'],
            ['#model-cache-volume', 'model-cache'],
            ['#model-cache-source', 'huggingface'],
        ]) {
            if (await page.locator(selector).inputValue() !== expected) {
                throw new Error(`Incorrect download default: ${selector}`);
            }
        }
    };
    await assertDownloadDefaults();
    await page.locator('#model-cache-repo').fill('example/edited-model');
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.getByRole('button', { name: 'Download model', exact: true }).click();
    await page.getByRole('heading', { name: 'Download a model', exact: true }).waitFor();
    if (await page.locator('#model-cache-repo').inputValue() !== ''
        || await page.locator('#model-cache-cluster').inputValue() !== '') {
        throw new Error('Standalone download reused model-card defaults');
    }
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.evaluate(() => {
        window.__navigationTestDownloaded = true;
    });
    await page.getByRole('button', { name: 'Back to model', exact: true }).click();
    await assertDraft();
    await page.getByText('This model is ready in the selected cache.', { exact: true }).waitFor();

    await page.getByRole('button', { name: 'Manage model cache', exact: true }).click();
    await assertDownloadDefaults();
    await page.goBack({ waitUntil: 'domcontentloaded' });
    await assertDraft();
    await page.getByText('This model is ready in the selected cache.', { exact: true }).waitFor();
    await page.goForward({ waitUntil: 'domcontentloaded' });
    await assertDownloadDefaults();
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.getByRole('button', { name: 'Back to model', exact: true }).click();
    await assertDraft();
    return { modelName, modelCacheDestination: true, downloadDialog: true, downloadDefaults: true, standaloneDownload: true, buttonReturn: true, browserHistory: true, cacheStatusRefresh: true };
}