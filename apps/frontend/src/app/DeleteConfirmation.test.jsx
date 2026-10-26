import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { DeleteConfirmation } from '../shared/ui/DeleteConfirmation';
import { Modal } from '../shared/ui/Modal';
import { DeleteAIProviderModal } from '../pages/ai-providers';
import { DeleteModelCacheModal } from '../pages/model-cache';
import { DeleteStorageVolumeModal } from '../pages/storage';
import { DeleteDeploymentModal } from '../pages/deployment';

// Capture the composition under React's server renderer without mounting a portal.
function capture(Component, props) {
    let view;
    function Capture() { view = Component(props); return null; }
    renderToStaticMarkup(<Capture />);
    return view;
}

test('centered dialogs and drawers honor backdrop close opt-outs', () => {
    const previousDocument = globalThis.document;
    globalThis.document = { body: { nodeType: 1 } };
    try {
        for (const variant of ['center', 'drawer']) {
            let closes = 0;
            const props = { isOpen: true, variant, onClose: () => { closes += 1; } };
            const portal = capture(Modal, props);
            const [backdrop, panel] = portal.children.props.children;
            backdrop.props.onClick();
            assert.equal(closes, 1);
            assert.equal(panel.props.onClick, undefined);
            const locked = capture(Modal, { ...props, closeOnBackdrop: false });
            assert.equal(locked.children.props.children[0].props.onClick, undefined);
        }
        const noOverlay = capture(Modal, { isOpen: true, variant: 'drawer', overlay: false });
        assert.equal(noOverlay.children.props.children[0], false);
        const inline = capture(Modal, { isOpen: true, variant: 'drawer', inline: true });
        assert.equal(inline.props.children[0], false);
    } finally {
        if (previousDocument === undefined) delete globalThis.document;
        else globalThis.document = previousDocument;
    }
});

test('legacy consent retains cancel/delete decisions and small dialog', () => {
    const decisions = [];
    const view = capture(DeleteConfirmation, { description: 'Remove saved result?', onDecision: value => decisions.push(value) });
    assert.equal(view.props.size, 'sm');
    assert.equal(view.props.title, 'Confirm deletion');
    const [cancel, confirm] = view.props.footer.props.children;
    cancel.props.onClick(); confirm.props.onClick(); view.props.onClose();
    assert.deepEqual(decisions, [false, true, false]);
    assert.match(renderToStaticMarkup(view.props.children), /Remove saved result\?/);
});

test('controlled confirmation preserves form association, errors, and all pending close locks', () => {
    const onCancel = () => {};
    const onSubmit = () => {};
    const confirmRef = { current: null };
    for (const pending of [false, true]) {
        const view = capture(DeleteConfirmation, { title: 'Delete resource', formId: 'domain-delete', onCancel, onSubmit, pending, error: 'Try again', confirmRef, children: <p>Domain warning</p> });
        assert.equal(view.props.onClose, pending ? undefined : onCancel);
        assert.equal(view.props.closeOnEscape, !pending);
        assert.equal(view.props.closeOnBackdrop, !pending);
        const [cancel, confirm] = view.props.footer.props.children;
        assert.equal(cancel.props.disabled, pending);
        assert.equal(confirm.props.disabled, pending);
        assert.equal(confirm.props.isLoading, pending);
        assert.equal(confirm.props.form, 'domain-delete');
        assert.equal(confirm.props.type, 'submit');
        assert.equal(confirm.props.ref, confirmRef);
        assert.equal(view.props.children.props.onSubmit, onSubmit);
        const html = renderToStaticMarkup(view.props.children);
        assert.match(html, /id="domain-delete"/);
        assert.match(html, /Domain warning/);
        assert.match(html, /role="alert"[^>]*>.*Try again/);
    }
});

test('typed confirmation disabling and custom storage actions remain caller owned', () => {
    const view = capture(DeleteConfirmation, { confirmDisabled: true });
    assert.equal(view.props.footer.props.children[1].props.disabled, true);
    const footer = <button>Keep files</button>;
    assert.equal(capture(DeleteConfirmation, { footer }).props.footer, footer);
    const deployment = capture(DeleteDeploymentModal, { deployment: { execution_id: 'run-1', namespace: 'ns' } });
    assert.equal(deployment.props.confirmDisabled, true);
    assert.equal(deployment.props.formId, 'delete-deployment-form');
    assert.match(renderToStaticMarkup(deployment.props.children), /Also delete the Kubernetes namespace/);
});

test('provider deletion preserves warnings, retry after failure and success lock', async () => {
    let calls = 0;
    const view = capture(DeleteAIProviderModal, {
        provider: { name: 'Provider' },
        onDelete: async () => { if (++calls === 1) throw new Error('retry'); },
    });
    assert.equal(view.props.formId, 'delete-ai-provider-form');
    assert.match(renderToStaticMarkup(view.props.children), /default planning behavior/);
    const event = { preventDefault() {} };
    await view.props.onSubmit(event);
    await view.props.onSubmit(event);
    await view.props.onSubmit(event);
    assert.equal(calls, 2);
});

test('cache deletion retries failures and only closes after confirmed deletion', async () => {
    for (const status of ['deleted', 'deleting']) {
        let calls = 0;
        let completions = 0;
        const view = capture(DeleteModelCacheModal, {
            entry: { id: 'cache-1', cachePath: '/models/example' },
            onDelete: async () => {
                if (++calls === 1) throw new Error('retry');
                return { status };
            },
            onDeleted: async () => { completions += 1; },
        });
        assert.equal(view.props.formId, 'delete-model-cache-form');
        assert.match(renderToStaticMarkup(view.props.children), /Other cached models/);
        const event = { preventDefault() {} };
        await view.props.onSubmit(event);
        assert.equal(completions, 0);
        await view.props.onSubmit(event);
        assert.equal(calls, 2);
        assert.equal(completions, status === 'deleted' ? 1 : 0);
    }
});

test('storage with cache records requires the file choice before deleting', async () => {
    for (const count of [0, 2]) {
        const calls = [];
        const view = capture(DeleteStorageVolumeModal, { volume: { id: 'volume', kind: 'nfs', modelCacheCount: count }, onDelete: keep => calls.push(keep) });
        assert.equal(view.props.confirmLabel, count ? 'Continue' : 'Delete storage volume');
        await view.props.onSubmit({ preventDefault() {} });
        assert.deepEqual(calls, count ? [] : [false]);
    }
});


test('deployment deletion retains main cluster-name display with raw-ID fallback', () => {
    const deployment = { execution_id: 'run-1', cluster_id: 'cluster-1', namespace: 'ns' };
    for (const [clusterNameById, expected] of [[{ 'cluster-1': 'Production cluster' }, 'Production cluster'], [{}, 'cluster-1']]) {
        const view = capture(DeleteDeploymentModal, { deployment, clusterNameById });
        const html = renderToStaticMarkup(view.props.children);
        assert.match(html, new RegExp(expected));
        assert.equal((html.match(/Execution ID/g) || []).length, 1);
        assert.doesNotMatch(html, /<form/);
    }
});
