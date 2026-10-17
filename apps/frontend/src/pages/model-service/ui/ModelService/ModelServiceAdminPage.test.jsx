import test, { describe } from 'node:test';
import assert from 'node:assert/strict';
import React, { createElement, Children } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { AddModelForm, ModelServiceAdminPage, TrafficDistributionForm } from './ModelServiceAdminPage.jsx';
import { AuthContext } from '../../../auth/AuthContext.js';
import { availableModelProviders, filterServiceFlow, parseTrafficWeight, trafficDistributionError, trafficShare, trafficTotal } from '../../model/modelService.js';
import { ComponentLogAction, DataPlaneTopology, ManageDeploymentAction, PublicationToggleAction, ProvidersToggleAction, Flow as FlowConnector } from './DataPlaneTopology.jsx';
import { ComponentLogViewer } from './GatewayOperationLogDrawer.jsx';
import { addModel, deleteGroup, getModelAccess, grantModelAccess, revokeModelAccess, setProvidersStatus } from './modelServiceBackend.js';
import { DeploymentManagementPage } from '../../../deployment/ui/DeploymentManagement/DeploymentManagementPage.jsx';

describe("Add model", () => {
    const deployment = { executionId: 'exec-a', name: 'deployment-a', model: 'Qwen/Qwen3-8B' };
    const defaults = {
        form: { clusterId: 'cluster-a', name: 'public-chat', executionId: 'exec-a' },
        clusters: [{ id: 'cluster-a', name: 'Cluster A' }, { id: 'cluster-b', name: 'Cluster B' }],
        deployments: [deployment],
        selectedDeployment: deployment,
        onChange() {},
    };
    const render = (props = {}) => renderToStaticMarkup(createElement(AddModelForm, { ...defaults, ...props }));

    test('cluster and provider precede the read-only provider-to-public name mapping', () => {
        const html = render();
        assert.match(html, /Public model name/);
        assert.match(html, /value="public-chat"/);
        assert.match(html, /id="add-model-provider-name"[^>]*readOnly=""[^>]*value="Qwen\/Qwen3-8B"/);
        assert.ok(html.indexOf('id="add-model-cluster"') < html.indexOf('id="add-model-provider"'));
        assert.ok(html.indexOf('id="add-model-provider"') < html.indexOf('id="add-model-provider-name"'));
        assert.ok(html.indexOf('id="add-model-provider-name"') < html.indexOf('Exposed to users as'));
        assert.ok(html.indexOf('Exposed to users as') < html.indexOf('id="add-model-name"'));
        assert.doesNotMatch(html, /Deployment model name/);
        assert.match(html, /Chat Completions API/);
        assert.equal((html.match(/<label/g) || []).length, 4);
    });

    test('each control label has a focusable question-mark help trigger and its own description', () => {
        const html = render();
        const descriptions = [...html.matchAll(/aria-describedby="([^"]+)"[^>]*><svg/g)].map((match) => match[1]);
        assert.equal(descriptions.length, 4);
        assert.equal(new Set(descriptions).size, 4);
        for (const field of ['Cluster', 'Provider', 'Provider model name', 'Public model name']) {
            assert.ok(html.includes(`aria-label="${field} help"`));
        }
        for (const id of descriptions) {
            assert.ok(html.includes(`id="${id}" role="tooltip"`));
        }
        assert.equal((html.match(/group-hover:visible group-focus-within:visible/g) || []).length, 4);
    });

    test('an existing public model name explains that its route will be reused', () => {
        assert.match(render({ existingModel: { id: 'group-a', name: 'public-chat' } }), /sharing its HTTPRoute/);
        assert.match(render({ existingModel: { id: 'group-a', name: 'public-chat' } }), /Traffic distribution after adding/);
        assert.doesNotMatch(render(), /This model already exists in the selected cluster/);
        assert.doesNotMatch(render(), /must be unique within this cluster/);
    });

    test('public model name dropdown lists only models in the selected cluster', () => {
        const models = [
            { id: 'a', clusterId: 'cluster-a', name: 'existing-a' },
            { id: 'b', clusterId: 'cluster-b', name: 'existing-b' },
        ];
        const options = (props) => render(props).match(/<datalist id="add-model-name-options">([^]*?)<\/datalist>/)[1];
        assert.match(render({ models }), /id="add-model-name" list="add-model-name-options"/);
        assert.match(options({ models }), /value="existing-a"/);
        assert.doesNotMatch(options({ models }), /existing-b/);
        const switched = { models, form: { ...defaults.form, clusterId: 'cluster-b' } };
        assert.match(options(switched), /value="existing-b"/);
        assert.doesNotMatch(options(switched), /existing-a/);
        assert.equal(options({ models, form: { ...defaults.form, clusterId: '' } }), '');
        assert.equal(options({ models: [] }), '');
    });

    test('public model name accepts both dropdown selection and a new typed name', () => {
        let next;
        const tree = AddModelForm({ ...defaults, onChange(value) { next = value; } });
        const nameControl = tree.props.children[3].props.children[2].props.children[1];
        for (const name of ['existing-a', 'brand-new-model']) {
            nameControl.props.onChange({ target: { value: name } });
            assert.deepEqual(next, { ...defaults.form, name });
            assert.match(render({ form: next }), new RegExp(`value="${name}"`));
        }
    });

    test('changing cluster clears provider selection but preserves the public name', () => {
        let next;
        const tree = AddModelForm({ ...defaults, onChange(value) { next = value; } });
        const clusterControl = tree.props.children[1].props.children[1];
        clusterControl.props.onChange({ target: { value: 'cluster-b' } });
        assert.deepEqual(next, { clusterId: 'cluster-b', name: 'public-chat', executionId: '' });
        const html = render({ form: next, deployments: [], selectedDeployment: undefined });
        assert.match(html, /No available ready deployments in this cluster/);
        assert.doesNotMatch(html, /Qwen\/Qwen3-8B|value="exec-a"/);
    });

    test('stale provider selection clears display, and missing model names and submission failures are explicit', () => {
        const stale = render({ selectedDeployment: undefined, deployments: [] });
        assert.doesNotMatch(stale, /Qwen\/Qwen3-8B/);
        assert.match(render({ selectedDeployment: { ...deployment, model: null } }), /has no model name/);
        assert.match(render({ error: 'Provider is no longer ready' }), /role="alert"[^>]*>Provider is no longer ready/);
        assert.equal((render({ pending: true }).match(/disabled=""/g) || []).length, 3);
    });

    test('Add model excludes published deployments regardless of service, status or weight', () => {
        const deployments = ['active', 'disabled', 'staged', 'unhealthy', 'available', 'not-ready'].map((id) => ({
            ...deployment, executionId: id, clusterId: 'cluster-a', status: id === 'not-ready' ? 'running' : 'ready',
        }));
        deployments.push({ ...deployment, executionId: 'other-cluster', clusterId: 'cluster-b', status: 'ready' });
        const members = ['active', 'disabled', 'staged', 'unhealthy'].map((id) => ({
            executionId: id, groupId: `group-${id}`, clusterId: 'cluster-a',
            status: id === 'staged' ? 'active' : id, weight: id === 'staged' ? 0 : 1,
        }));
        assert.deepEqual(
            availableModelProviders(deployments, members, 'cluster-a').map((item) => item.executionId),
            ['available'],
        );
        assert.deepEqual(
            availableModelProviders(deployments, members, 'cluster-b').map((item) => item.executionId),
            ['other-cluster'],
        );
        assert.equal(availableModelProviders(deployments, members, '').length, 0);
    });

    test('a provider added while the drawer is open becomes unselectable and returns after removal', () => {
        const deployments = [{ ...deployment, clusterId: 'cluster-a', status: 'ready' }];
        const members = [{ executionId: deployment.executionId, clusterId: 'cluster-a', status: 'active' }];
        const available = availableModelProviders(deployments, members, defaults.form.clusterId);
        const selected = available.find((item) => item.executionId === defaults.form.executionId);
        assert.equal(selected, undefined);
        const html = render({ deployments: available, selectedDeployment: selected });
        assert.doesNotMatch(html, /value="exec-a"|value="Qwen\/Qwen3-8B"/);
        assert.match(html, /id="add-model-provider" disabled=""/);
        assert.match(html, /Deployments already added to a model are excluded/);
        assert.deepEqual(availableModelProviders(deployments, [], defaults.form.clusterId), deployments);
    });

    test('page header exposes sky Add model and Reconcile with their existing permissions', () => {
        const renderPage = (permissions) => renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: (permission) => permissions.includes(permission) } },
            createElement(ModelServiceAdminPage),
        ));
        const html = renderPage(['model-service:group:manage', 'model-service:member:manage', 'model-service:gateway:manage']);
        const header = html.match(/<header[^]*?<\/header>/)[0];
        assert.match(header, /<button[^>]*bg-sky-500[^>]*>[^]*?Add model<\/button>/);
        assert.match(header, /Reconcile<\/button>/);
        assert.equal((html.match(/Add model<\/button>/g) || []).length, 1);
        assert.equal((html.match(/Reconcile<\/button>/g) || []).length, 1);
        assert.doesNotMatch(html, /Add model service|Add model server/);
        assert.doesNotMatch(html, /cluster\(s\)|member\(s\)|model service\(s\)|route\(s\)/);
        assert.doesNotMatch(renderPage(['model-service:group:manage']), /Add model</);
        assert.doesNotMatch(renderPage(['model-service:member:manage']), /Add model</);
        assert.doesNotMatch(renderPage(['model-service:group:manage', 'model-service:member:manage']), /Reconcile<\/button>/);
    });
});

describe("Traffic distribution", () => {
    const draft = {
        groupId: 'g', name: 'public', clusterId: 'cluster', clusterName: 'Cluster',
        providerModel: 'provider', expectedWeights: { a: 1 },
        rows: [{ executionId: 'a', name: 'Stable deployment', status: 'active', weight: '1' }],
    };
    const deployments = [
        { executionId: 'a', name: 'Already added', model: 'provider', clusterId: 'cluster', endpointKind: 'llm-d-epp', status: 'ready' },
        { executionId: 'b', name: 'Canary deployment', model: 'provider', clusterId: 'cluster', endpointKind: 'llm-d-epp', status: 'ready' },
        { executionId: 'c', name: 'Wrong cluster', model: 'provider', clusterId: 'other', endpointKind: 'llm-d-epp', status: 'ready' },
        { executionId: 'd', name: 'Wrong model', model: 'other', clusterId: 'cluster', endpointKind: 'llm-d-epp', status: 'ready' },
        { executionId: 'e', name: 'Not ready', model: 'provider', clusterId: 'cluster', endpointKind: 'llm-d-epp', status: 'running' },
    ];

    function findElement(node, id) {
        if (node?.props?.id === id) return node;
        for (const child of Children.toArray(node?.props?.children)) {
            const result = findElement(child, id);
            if (result) return result;
        }
        return null;
    }

    test('traffic form shows weights, read-only shares, help and only eligible deployments', () => {
        const html = renderToStaticMarkup(createElement(TrafficDistributionForm, { draft, deployments }));
        assert.match(html, /max="1000000" step="1"/);
        assert.match(html, />100%<\/output>/);
        assert.match(html, /Canary deployment/);
        assert.doesNotMatch(html, /Already added|Wrong cluster|Wrong model|Not ready/);
        assert.match(html, /Gateway API weight specification/);
    });

    test('deployment status follows its name and weight controls share one row', () => {
        for (const row of [
            draft.rows[0],
            { ...draft.rows[0], status: 'disabled' },
            { ...draft.rows[0], isNew: true },
        ]) {
            const html = renderToStaticMarkup(createElement(TrafficDistributionForm, {
                draft: { ...draft, rows: [row] }, deployments,
            }));
            const status = row.isNew ? 'New provider \\(not saved\\)' : row.status;
            assert.match(html, new RegExp(`Stable deployment</p><span[^>]*>${status}</span></div>`));
            assert.match(html, /<div class="flex items-center gap-3"><label[^>]*for="traffic-weight-0">Weight<\/label><input[^>]*id="traffic-weight-0"[^>]*\/><output[^>]*>/);
            assert.equal(findElement(TrafficDistributionForm({
                draft: { ...draft, rows: [row] }, deployments,
            }), 'traffic-weight-0').props.disabled, row.status === 'disabled');
        }
    });

    test('adding a provider defaults to weight one and preserves the concurrency snapshot', () => {
        let changed;
        const tree = TrafficDistributionForm({ draft, deployments, onChange: (next) => { changed = next; } });
        findElement(tree, 'traffic-add-deployment').props.onChange({ target: { value: 'b' } });
        assert.equal(changed.rows[1].weight, '1');
        assert.equal(changed.rows[1].isNew, true);
        assert.deepEqual(changed.expectedWeights, { a: 1 });
        assert.equal(draft.rows.length, 1);
        const html = renderToStaticMarkup(createElement(TrafficDistributionForm, {
            draft: changed, deployments, pending: true, error: 'Configuration saved, but reconciliation failed.',
        }));
        assert.match(html, /Configuration saved, but reconciliation failed/);
        assert.equal((html.match(/>50%<\/output>/g) || []).length, 2);
        assert.equal(findElement(TrafficDistributionForm({ draft: changed, deployments, pending: true }), 'traffic-weight-1').props.disabled, true);
    });
});

describe("Service flow", () => {
    function renderTopology(count, deploymentsPerModel = 1, memberOverrides = {}, { status = 'active', can = () => true, clusters } = {}) {
        const services = Array.from({ length: count }, (_, index) => ({ id: `model-${index}`, name: `chat-${index}` }));
        return renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can } },
            createElement(DataPlaneTopology, {
                clusters: clusters || [{
                    clusterId: 'cluster-a', clusterName: 'Cluster A',
                    gatewayState: 'installed', gatewayReady: true,
                    ippState: 'installed', ippReady: true,
                }],
                memberGroups: services.map((service) => ({
                    ...service,
                    status,
                    clusters: [{
                        clusterId: 'cluster-a',
                        deployments: Array.from({ length: deploymentsPerModel }, (_, index) => ({
                            memberId: `member-${service.id}-${index}`, clusterId: 'cluster-a',
                            name: `deployment-${service.id}-${index}`, poolName: `pool-${service.id}-${index}`,
                            eppRef: `epp-${service.id}-${index}`, status: 'active',
                            weight: index === 0 ? 90 : 10,
                            components: { httpRoute: 'ready', inferencePool: 'ready', epp: 'missing', modelServer: 'ready' },
                            ...memberOverrides,
                        })),
                    }],
                })),
                statusDot: { active: 'bg-emerald-400', stopped: 'bg-slate-500', standby: 'bg-slate-500', unhealthy: 'bg-rose-500', unknown: 'bg-amber-400' },
                onEditTraffic: () => {},
                onToggleService: () => {},
                onToggleDeployment: () => {},
                onNavigate: () => {},
            }),
        ));
    }

    test('one route exposes a model-name action menu and configured weighted branch shares', () => {
        const html = renderTopology(1, 2);
        assert.equal((html.match(/>HTTPRoute<\/span>/g) || []).length, 1);
        assert.match(html, /title="chat-0">chat-0<\/span><span[^>]*>Serving<\/span><button[^>]*aria-label="chat-0 actions"[^>]*aria-haspopup="menu"[^>]*aria-expanded="false"/);
        assert.doesNotMatch(html, />Traffic distribution<\/button>/);
        assert.match(html, /90%<\/text>/);
        assert.match(html, /10%<\/text>/);
        assert.match(html, /Configured request share; not measured traffic/);
        assert.match(html, /aria-label="chat-0 traffic distribution"/);
        assert.match(html, /title="Configure relative traffic weights[^"]*Weight 0[^"]*offline providers receive no traffic/);
        assert.match(html, /bg-sky-500[^"]*rounded-full/);
        assert.match(html, /style="left:276px;top:168px"/);
    });

    test('empty models remain visible and searchable after detaching the last provider', () => {
        const html = renderTopology(1, 0);
        assert.match(html, /aria-label="Model chat-0"/);
        assert.match(html, /aria-label="chat-0 actions"/);
        assert.match(html, /No providers/);
        assert.doesNotMatch(html, /aria-label="chat-0 traffic distribution"/);
        assert.doesNotMatch(html, /NaN|height:-/);
        const filtered = filterServiceFlow(
            [{ clusterId: 'cluster-a' }],
            [{ id: 'empty', name: 'chat', clusters: [{ clusterId: 'cluster-a', deployments: [] }] }],
            { modelQuery: 'chat' },
        );
        assert.equal(filtered.memberGroups[0].id, 'empty');
        assert.equal(filtered.clusters.length, 1);
    });

    test('bulk providers action occupies one slot and derives online/offline from all providers', () => {
        for (const [statuses, expectedOnline] of [
            [['disabled', 'disabled'], true],
            [['active', 'active'], false],
            [['active', 'disabled'], false],
            [['unhealthy', 'disabled'], false],
        ]) {
            let online;
            const action = ProvidersToggleAction({
                members: statuses.map((status) => ({ status })),
                pending: false, onClick: (value) => { online = value; },
            });
            assert.equal(action.props.label, expectedOnline ? 'Bring providers online' : 'Take providers offline');
            assert.equal(action.props.disabled, false);
            assert.equal((renderToStaticMarkup(action).match(/role="menuitem"/g) || []).length, 1);
            action.props.onClick();
            assert.equal(online, expectedOnline);
        }
        assert.equal(ProvidersToggleAction({ members: [], pending: false }).props.disabled, true);
        assert.equal(ProvidersToggleAction({ members: [{ status: 'disabled' }], pending: true }).props.disabled, true);
    });

    test('model lifecycle client preserves cleanup failures and uses a single batch status request', async () => {
        const original = globalThis.fetch;
        const calls = [];
        try {
            globalThis.fetch = async (url, options) => {
                calls.push({ url, options });
                return new Response(JSON.stringify({ items: [{ id: 'member-a', status: 'disabled', weight: 7 }] }), { status: 200 });
            };
            const items = await setProvidersStatus('model/a', 'disabled');
            assert.equal(items[0].weight, 7);
            assert.equal(calls.length, 1);
            assert.match(calls[0].url, /groups\/model%2Fa\/providers\/status$/);
            assert.equal(calls[0].options.method, 'PUT');
            assert.deepEqual(JSON.parse(calls[0].options.body), { status: 'disabled' });
            globalThis.fetch = async () => new Response(JSON.stringify({
                code: 'model_removal_failed', detail: 'Cleanup failed; model retained.',
            }), { status: 502 });
            await assert.rejects(deleteGroup('model/a'), /Cleanup failed; model retained/);
        } finally {
            globalThis.fetch = original;
        }
    });

    test('the flow has a centered, bounded, keyboard-focusable scrolling viewport', () => {
        const html = renderTopology(2);
        assert.match(html, /role="region" aria-label="Service flow canvas" tabindex="0"/);
        assert.match(html, /cursor-grab select-none overflow-auto/);
        assert.match(html, /active:cursor-grabbing/);
        assert.match(html, /max-h-\[max\(16rem,calc\(100vh-18rem\)\)\]/);
        assert.match(html, /class="mx-auto w-max"><div class="relative"/);
    });

    test('standby pool is neutral and does not hide an unhealthy deployment component', () => {
        const html = renderTopology(1, 1, {
            weight: 0,
            status: 'unhealthy',
            components: { httpRoute: 'ready', inferencePool: 'standby', epp: 'missing', modelServer: 'ready' },
        });
        assert.match(html, /bg-slate-500" title="InferencePool: standby \(0 weight; not routed\)"/);
        assert.match(html, /title="EPP: unhealthy"/);
        assert.match(html, /0%<\/text>/);
        assert.doesNotMatch(html, /title="InferencePool: (?:unhealthy|degraded|active|unknown)"/);
    });

    test('publication switches show distinct stop/resume and online/offline actions, preserving pending state', () => {
        for (const [kind, stopped, label] of [
            ['model', false, 'Stop serving'], ['model', true, 'Resume serving'],
            ['deployment', false, 'Take offline'], ['deployment', true, 'Bring online'],
        ]) {
            let clicks = 0;
            const action = PublicationToggleAction({ kind, stopped, pending: false, onClick: () => { clicks += 1; } });
            assert.equal(action.props.label, label);
            action.props.onClick();
            assert.equal(clicks, 1);
            assert.match(renderToStaticMarkup(action), /role="menuitem"/);
            const pending = PublicationToggleAction({ kind, stopped, pending: true, onClick: () => {} });
            assert.match(renderToStaticMarkup(pending), /disabled=""/);
            assert.match(renderToStaticMarkup(pending), /Pods/);
        }
    });

    test('stopped models keep provider state and weights but show a stopped route with no animated model traffic', () => {
        const html = renderTopology(1, 2, {}, { status: 'disabled' });
        assert.match(html, />Stopped<\/span>/);
        assert.match(html, />Online<\/span>/);
        assert.match(html, /HTTPRoute: stopped/);
        assert.match(html, /90%<\/text>/);
        const frame = html.split('role="group" aria-label="Model chat-0"')[1];
        assert.doesNotMatch(frame, /lens-chain-flow/);
        assert.match(frame, /stroke-slate-600/);
    });

    test('offline deployments stay visible with their own menu and no routed share', () => {
        const html = renderTopology(1, 1, { status: 'disabled' });
        assert.match(html, /aria-label="deployment-model-0-0 deployment actions"/);
        assert.match(html, />Offline<\/span>/);
        assert.match(html, />No traffic<\/span>/);
        assert.match(html, /0%<\/text>/);
        assert.match(html, /HTTPRoute: stopped/);
        assert.match(html, /model server: active/);
    });

    test('publication labels use compact soft badges without dots or heavy typography', () => {
        for (const [html, labels] of [
            [renderTopology(1), ['Serving', 'Online']],
            [renderTopology(1, 1, { status: 'disabled' }, { status: 'disabled' }), ['Stopped', 'Offline']],
            [renderTopology(1, 1, { weight: 0 }), ['No traffic', 'Standby']],
        ]) {
            for (const label of labels) {
                const match = html.match(new RegExp(`<span class="([^"]+)">${label}<\\/span>`));
                assert.ok(match, `${label} contains only text`);
                assert.match(match[1], /h-4/);
                if (['Serving', 'Stopped', 'No traffic'].includes(label)) {
                    assert.doesNotMatch(match[1], /ml-auto/);
                    assert.match(match[1], /shrink-0/);
                } else {
                    assert.match(match[1], /ml-auto/);
                }
                assert.match(match[1], /text-\[10px\]/);
                assert.match(match[1], /font-normal/);
                assert.match(match[1], /border-0/);
                assert.match(match[1], /bg-/);
                assert.doesNotMatch(match[1], /font-black|uppercase|tracking-wider|rounded-full/);
            }
        }
    });

    test('model and deployment action menus follow their separate permissions', () => {
        const groupOnly = renderTopology(1, 1, {}, { can: (code) => code === 'model-service:group:manage' });
        assert.match(groupOnly, /aria-label="chat-0 actions"/);
        assert.doesNotMatch(groupOnly, /deployment actions/);
        const memberOnly = renderTopology(1, 1, {}, { can: (code) => code === 'model-service:member:manage' });
        assert.match(memberOnly, /aria-label="chat-0 actions"/);
        assert.match(memberOnly, /deployment actions/);
        const readOnly = renderTopology(1, 1, {}, { can: () => false });
        assert.doesNotMatch(readOnly, /aria-label="chat-0 actions"|deployment actions/);
        assert.doesNotMatch(readOnly, /aria-label="chat-0 traffic distribution"/);
        assert.match(memberOnly, /aria-label="chat-0 traffic distribution"/);
        assert.doesNotMatch(groupOnly, /aria-label="chat-0 traffic distribution"/);
        const deploymentReader = renderTopology(1, 1, {}, { can: (code) => code === 'deployment:run:read' });
        assert.match(deploymentReader, /deployment actions/);
        assert.doesNotMatch(deploymentReader, /aria-label="chat-0 actions"/);
    });

    test('Manage filters deployments by namespace rather than name and requires deployment read access', () => {
        let navigation;
        const action = ManageDeploymentAction({
            deployment: { name: 'serving-qwen-8b', namespace: 'llm-d-qwen-a1b2', id: 'exec-a', memberId: 'member-a', status: 'disabled' },
            onNavigate: (view, params) => { navigation = { view, params }; },
        });
        const renderAction = (permission) => renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: (code) => code === permission } },
            action,
        ));
        assert.match(renderAction('deployment:run:read'), /role="menuitem"[^]*Manage/);
        assert.equal(renderAction('model-service:member:manage'), '');
        action.props.children.props.onClick();
        assert.deepEqual(navigation, {
            view: 'optimization-deployments',
            params: { deploymentSearch: 'llm-d-qwen-a1b2' },
        });
    });

    test('Manage is disabled with an explanation when deployment namespace is unavailable', () => {
        const action = ManageDeploymentAction({ deployment: { name: 'serving-qwen-8b' }, onNavigate: () => {} });
        const html = renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: () => true } },
            action,
        ));
        assert.match(html, /disabled=""/);
        assert.match(html, /Deployment namespace is unavailable/);
    });

    test('each model frame connects HTTPRoute to a named deployment containing three components', () => {
        const html = renderTopology(2);
        assert.doesNotMatch(html, /cluster\(s\)|model service\(s\)|route\(s\)/);
        const frames = html.split(/(?=role="group" aria-label="Model )/).slice(1);
        assert.equal(frames.length, 2);
        frames.forEach((frame, index) => {
            assert.ok(frame.includes(`aria-label="Model chat-${index}"`));
            for (const component of ['HTTPRoute', 'InferencePool', 'EPP', 'model server']) {
                assert.ok(frame.includes(`>${component}</span>`));
            }
            assert.match(frame, /title="HTTPRoute: active"/);
            assert.match(frame, /title="EPP: unhealthy"/);
            assert.match(frame, /aria-label="HTTPRoute actions"/);
            assert.match(frame, /aria-label="EPP actions"/);
            const [route, deployment] = frame.split('role="group" aria-label="Deployment ');
            assert.match(route, />HTTPRoute<\/span>/);
            assert.match(deployment, new RegExp(`^deployment-model-${index}-0"`));
            assert.match(deployment, new RegExp(`title="deployment-model-${index}-0">deployment-model-${index}-0<`));
            assert.doesNotMatch(deployment, />HTTPRoute<\/span>|<path d="M /);
            assert.match(route, /d="M 216 104 H 276"/);
            assert.match(route, /d="M 276 104 H 336"/);
        });
        const flowSvg = html.match(/<svg class="pointer-events-none[^]*?<\/svg>/)[0];
        const boundaries = [...html.matchAll(/aria-label="Model [^"]+" style="left:(\d+)px/g)].map((match) => Number(match[1]));
        assert.equal(boundaries.length, 2);
        // Each gateway branch has a base, glow, three fading tail segments and a head.
        assert.equal((flowSvg.match(new RegExp(`H ${boundaries[0]}"`, 'g')) || []).length, 12);
        assert.doesNotMatch(flowSvg, /H (?:770|980|1220)|M (?:760|950|860) /);
    });

    test('model and deployment frames use the Evaluation setup gradients', () => {
        const html = renderTopology(1);
        for (const [kind, classes] of [
            ['Model', ['border-cyan-500/15', 'from-slate-100/80', 'via-slate-50/70', 'to-cyan-50/15', 'dark:from-slate-900/80', 'dark:via-slate-950/70', 'dark:to-cyan-950/15']],
            ['Deployment', ['border-theme-border', 'from-slate-50/70', 'to-cyan-50/10', 'dark:from-slate-950/70', 'dark:to-cyan-950/10']],
        ]) {
            const frame = html.match(new RegExp(`<div class="([^"]+)" role="group" aria-label="${kind} `));
            assert.ok(frame);
            for (const className of ['bg-theme-card', 'bg-linear-to-br', ...classes]) {
                assert.ok(frame[1].split(' ').includes(className), `${kind} frame uses ${className}`);
            }
            assert.doesNotMatch(frame[1], /violet|teal|graphite/);
        }
    });

    test('topology canvas contains the final model frame even with many models', () => {
        const html = renderTopology(5);
        const height = Number(html.match(/class="relative" style="width:\d+px;height:(\d+)px/)[1]);
        const frames = [...html.matchAll(/aria-label="Model [^"]+" style="left:\d+px;top:(\d+)px;width:\d+px;height:(\d+)px/g)];
        assert.equal(frames.length, 5);
        assert.ok(height >= Number(frames.at(-1)[1]) + Number(frames.at(-1)[2]));
        for (let index = 1; index < frames.length; index += 1) {
            assert.ok(Number(frames[index][1]) > Number(frames[index - 1][1]) + Number(frames[index - 1][2]));
        }
    });

    test('cluster sections enclose their components with clear headers and non-overlapping boundaries', () => {
        const html = renderTopology(2, 2, {}, { clusters: [
            { clusterId: 'cluster-a', clusterName: 'Production', gatewayState: 'installed', gatewayReady: true },
            { clusterId: 'cluster-b', clusterName: 'Staging', gatewayState: 'not-installed' },
        ] });
        const backgrounds = [...html.matchAll(/aria-hidden="true" style="left:(\d+)px;top:(\d+)px;width:(\d+)px;height:(\d+)px/g)];
        assert.equal(backgrounds.length, 2);
        assert.ok(Number(backgrounds[0][1]) > 270, 'shared control-plane trunk remains outside cluster frames');
        assert.equal(Number(backgrounds[1][2]) - Number(backgrounds[0][2]) - Number(backgrounds[0][4]), 40);
        assert.ok(backgrounds[1].index < html.indexOf('<svg class="pointer-events-none'), 'backgrounds render behind the flow layer');
        const groups = html.split(/(?=<div role="group" aria-label="Cluster )/).slice(1);
        assert.equal(groups.length, 2);
        groups.forEach((group, index) => {
            const [, left, top, width, height] = backgrounds[index].map(Number);
            const name = index === 0 ? 'Production' : 'Staging';
            assert.ok(group.includes(`class="truncate text-sm font-semibold text-theme-text" title="${name}"`));
            assert.doesNotMatch(group, />Clients \/ SDK<\/span>|>Lens control plane<\/span>/);
            const nodes = [...group.matchAll(/class="absolute" style="left:300px;top:(\d+)px;width:200px;height:64px/g)];
            assert.equal(nodes.length, 2);
            for (const node of nodes) {
                assert.ok(Number(node[1]) >= top + 48);
                assert.ok(Number(node[1]) + 64 <= top + height - 16);
            }
            const models = [...group.matchAll(/aria-label="Model [^"]+" style="left:(\d+)px;top:(\d+)px;width:(\d+)px;height:(\d+)px/g)];
            assert.equal(models.length, index === 0 ? 2 : 0);
            for (const model of models) {
                assert.ok(Number(model[1]) >= left && Number(model[1]) + Number(model[3]) <= left + width);
                assert.ok(Number(model[2]) >= top + 48 && Number(model[2]) + Number(model[4]) <= top + height - 16);
            }
        });
        const canvasHeight = Number(html.match(/class="relative" style="width:\d+px;height:(\d+)px/)[1]);
        assert.ok(canvasHeight > Number(backgrounds[1][2]) + Number(backgrounds[1][4]));
    });

    test('Gateway has no outgoing blue line without models and restores it when models exist', () => {
        const empty = renderTopology(0);
        const populated = renderTopology(1);
        assert.doesNotMatch(empty, /d="M 500 \d+ H 520"/);
        assert.match(populated, /d="M 500 \d+ H 520"/);
        assert.match(empty, /d="M 400 \d+ V \d+"[^>]*class="stroke-emerald-500"/);
        assert.match(empty, /d="M 136 44 H 228 Q 240 44 240 56 V [^"]+ H 300"[^>]*class="stroke-blue-500"/);
    });

    test('IPP help exposes the official definition and source on hover or keyboard focus', () => {
        const html = renderTopology(1);
        const helpId = html.match(/aria-label="IPP help" aria-describedby="([^"]+)"/)[1];
        assert.ok(html.includes(`id="${helpId}" role="tooltip"`));
        assert.match(html, />IPP<\/span><span class="group inline-flex relative">/);
        assert.match(html, /group-hover:visible group-focus-within:visible/);
        assert.match(html, /pluggable framework for processing inference request and response payloads before and after routing decisions/);
        assert.match(html, /EPP selects the pod within the chosen pool/);
        assert.match(html, /href="https:\/\/github.com\/llm-d\/llm-d\/blob\/[a-f0-9]+\/docs\/architecture\/router\/ipp.md"/);
    });

    test('Gateway and Lens control plane help explain their roles with accessible tooltips', () => {
        const html = renderTopology(1);
        const helpIds = [];
        for (const label of ['Lens control plane', 'Gateway']) {
            const trigger = html.match(new RegExp(`aria-label="${label} help" aria-describedby="([^"]+)"`));
            assert.ok(trigger);
            helpIds.push(trigger[1]);
            const tooltip = html.slice(trigger.index).match(/class="invisible[^"]+"[^]*?role="tooltip"[^]*?<\/span>/)[0];
            assert.ok(tooltip.includes(label === 'Lens control plane' ? 'top-full pt-2' : 'bottom-full pb-2'));
            assert.match(tooltip, /group-hover:visible group-focus-within:visible/);
            assert.ok(tooltip.includes(`id="${trigger[1]}"`));
        }
        assert.equal(new Set(helpIds).size, 2);
        assert.match(html, /management layer, not the inference proxy/);
        assert.match(html, /routes requests to InferencePools using HTTPRoute rules/);
        assert.match(html, /href="https:\/\/github.com\/llm-d\/llm-d\/blob\/[a-f0-9]+\/docs\/infrastructure\/gateway\/README.md"/);
        assert.match(renderTopology(0), /aria-label="Lens control plane help"/);
    });

    test('deployment components have equal dimensions, aligned tops and evenly spaced gaps', () => {
        const html = renderTopology(1);
        const frame = html.split('role="group" aria-label="Deployment ')[1];
        const boxes = [...frame.matchAll(/class="absolute" style="left:(\d+)px;top:(\d+)px;width:(\d+)px;height:(\d+)px/g)];
        assert.equal(boxes.length, 3);
        assert.equal(new Set(boxes.map((box) => `${box[2]}:${box[3]}:${box[4]}`)).size, 1);
        const gaps = boxes.slice(1).map((box, index) => Number(box[1]) - Number(boxes[index][1]) - Number(boxes[index][3]));
        assert.ok(gaps[0] > 0);
        assert.equal(new Set(gaps).size, 1);
        const frameWidth = Number(frame.match(/style="left:\d+px;top:\d+px;width:(\d+)px/)[1]);
        assert.equal(Number(boxes[0][1]), frameWidth - Number(boxes[2][1]) - Number(boxes[2][3]));
        const route = html.split('role="group" aria-label="Model ')[1].split('role="group" aria-label="Deployment ')[0];
        const routeBox = route.match(/class="absolute" style="left:\d+px;top:(\d+)px;width:(\d+)px;height:(\d+)px/);
        const deploymentTop = Number(frame.match(/style="left:\d+px;top:(\d+)px/)[1]);
        assert.equal(Number(routeBox[1]), deploymentTop + Number(boxes[0][2]));
        assert.equal(routeBox[2], boxes[0][3]);
        assert.equal(routeBox[3], boxes[0][4]);
    });

    test('control plane, Gateway and IPP boxes have equal sizes and no subtitle row', () => {
        const html = renderTopology(1);
        const boxes = html.split('<div class="absolute" style=').slice(1);
        for (const label of ['Lens control plane', 'Gateway', 'IPP']) {
            const box = boxes.find((item) => item.includes(`>${label}</span>`));
            assert.ok(box);
            assert.match(box, /^"left:300px;top:\d+px;width:200px;height:64px"/);
            assert.doesNotMatch(box, /class="mt-0.5 truncate/);
            assert.ok(box.includes(`aria-label="${label} help"`));
        }
        assert.match(html, /title="Gateway URL \+ token"/);
        const clients = boxes.find((item) => item.includes('>Clients / SDK</span>'));
        assert.match(clients, /^"left:8px;top:12px;width:128px;height:64px"/);
        assert.match(html, /d="M 136 44 H 228 Q 240 44 240 56 /);
    });

    test('model component boxes replace subtitles with sourced one-sentence help', () => {
        const frames = renderTopology(2).split(/(?=role="group" aria-label="Model )/).slice(1);
        const helpIds = [];
        const crdTags = [...renderTopology(1).matchAll(/<span class="([^"]+)">CRD<\/span>/g)];
        assert.equal(crdTags.length, 2);
        for (const [, classes] of crdTags) {
            assert.match(classes, /h-3\.5/);
            assert.match(classes, /font-normal/);
            assert.doesNotMatch(classes, /font-black|tracking-wider/);
        }
        const descriptions = [
            ['HTTPRoute', 'routing HTTP requests', 'api-types/httproute.md'],
            ['InferencePool', 'group of Pods (containers)', 'api-types/inferencepool.md'],
            ['EPP', 'pod within an InferencePool', 'router/epp.md'],
            ['model server', 'execute prefill and decode forward passes', 'model-servers/README.md'],
        ];
        for (const frame of frames) {
            assert.doesNotMatch(frame, /class="mt-0.5 truncate/);
            for (const [label, description, source] of descriptions) {
                const box = frame.split('<div class="absolute" style=').find((item) => item.includes(`>${label}</span>`));
                const helpId = box.match(new RegExp(`aria-label="${label} help" aria-describedby="([^"]+)"`))[1];
                helpIds.push(helpId);
                assert.ok(box.includes(`id="${helpId}" role="tooltip"`));
                assert.ok(box.includes(description));
                assert.ok(box.includes(`${source}" target="_blank" rel="noopener noreferrer"`));
                assert.match(box, /group-hover:visible group-focus-within:visible right-0 w-80 bottom-full pb-2/);
            }
        }
        assert.equal(new Set(helpIds).size, 8);
    });

    test('one HTTPRoute fans out to multiple deployment frames without duplicating the route', () => {
        const html = renderTopology(2, 3);
        const models = html.split(/(?=role="group" aria-label="Model )/).slice(1);
        assert.equal(models.length, 2);
        for (const model of models) {
            assert.equal((model.match(/>HTTPRoute<\/span>/g) || []).length, 1);
            assert.equal((model.match(/aria-label="HTTPRoute actions"/g) || []).length, 1);
            assert.equal((model.match(/aria-label="EPP actions"/g) || []).length, 3);
            const deployments = [...model.matchAll(/aria-label="Deployment [^"]+" style="left:(\d+)px;top:(\d+)px;width:\d+px;height:(\d+)px/g)];
            assert.equal(deployments.length, 3);
            const modelHeight = Number(model.match(/style="left:\d+px;top:\d+px;width:\d+px;height:(\d+)px/)[1]);
            const route = model.match(/class="absolute" style="left:(\d+)px;top:(\d+)px;width:(\d+)px;height:(\d+)px/);
            const routeMid = Number(route[2]) + Number(route[4]) / 2;
            deployments.forEach((deployment, index) => {
                const top = Number(deployment[2]);
                assert.ok(top + Number(deployment[3]) < modelHeight);
                if (index > 0) assert.ok(top > Number(deployments[index - 1][2]) + Number(deployments[index - 1][3]));
                assert.equal(Number(deployment[1]) - Number(route[1]) - Number(route[3]), 120);
                const endY = top + 68;
                const corner = endY === routeMid ? '' : `V ${endY + (endY > routeMid ? -12 : 12)} Q 276 ${endY} 288 ${endY} `;
                assert.ok(model.includes(`d="M 276 ${routeMid} ${corner}H ${deployment[1]}"`));
            });
        }
        const modelBounds = [...html.matchAll(/aria-label="Model [^"]+" style="left:\d+px;top:(\d+)px;width:\d+px;height:(\d+)px/g)];
        assert.ok(Number(modelBounds[1][1]) > Number(modelBounds[0][1]) + Number(modelBounds[0][2]));
        const canvasHeight = Number(html.match(/class="relative" style="width:\d+px;height:(\d+)px/)[1]);
        assert.ok(canvasHeight > Number(modelBounds[1][1]) + Number(modelBounds[1][2]));
    });

    test('wider route spacing keeps deployment contents and share labels inside the canvas', () => {
        for (const count of [1, 3]) {
            const html = renderTopology(1, count);
            const canvasWidth = Number(html.match(/class="relative" style="width:(\d+)px/)[1]);
            const model = html.match(/aria-label="Model [^"]+" style="left:(\d+)px;top:\d+px;width:(\d+)px/);
            assert.ok(Number(model[1]) + Number(model[2]) < canvasWidth);
            const deployments = [...html.matchAll(/aria-label="Deployment [^"]+" style="left:(\d+)px;top:\d+px;width:(\d+)px/g)];
            for (const deployment of deployments) {
                assert.equal(Number(deployment[2]), 656);
                assert.ok(Number(deployment[1]) + Number(deployment[2]) < Number(model[2]));
            }
            assert.equal((html.match(/<text x="306"/g) || []).length, count);
        }
    });

    test('Gateway and IPP log actions are disabled when stopped or uninstalled and enabled when running', () => {
        for (const component of ['gateway', 'ipp']) {
            const name = component === 'gateway' ? 'Gateway' : 'IPP';
            const stateKey = component === 'gateway' ? 'gatewayState' : 'ippState';
            const runningKey = component === 'gateway' ? 'gatewayRunning' : 'ippReady';
            const cases = [
                { state: 'installed', running: true, disabled: false },
                { state: 'installed', running: false, disabled: true, reason: `Start ${name}` },
                { state: 'missing', running: false, disabled: true, reason: `Install ${name}` },
                { state: 'missing', running: true, disabled: true, reason: `Install ${name}` },
                { state: 'unknown', running: true, disabled: true, reason: `Install ${name}` },
            ];
            for (const { state, running, disabled, reason } of cases) {
                let calls = 0;
                const props = {
                    component,
                    cluster: { [stateKey]: state, [runningKey]: running, gatewayReady: true },
                    onClick: () => { calls += 1; },
                };
                const html = renderToStaticMarkup(createElement(ComponentLogAction, props));
                const action = ComponentLogAction(props);
                assert.equal(action.props.disabled, disabled);
                assert.equal(html.includes('disabled=""'), disabled);
                assert.match(html, /role="menuitem"/);
                if (disabled) {
                    assert.ok(html.includes(`title="${reason} before viewing logs."`));
                    assert.equal(action.props.onClick, undefined);
                } else {
                    action.props.onClick();
                    assert.equal(calls, 1);
                }
            }
        }
    });
});

describe("Component logs", () => {
    const render = (lines) => renderToStaticMarkup(<ComponentLogViewer entries={lines.map((raw, id) => ({ id, raw }))} />);

    test('kubectl-prefixed JSON separates source, time, level and message without losing fields', () => {
        const raw = '[pod/lens-ipp-abc/ipp] {"ts":1792158800.125,"level":"warning","msg":"Request queued","attempt":0,"ready":false,"metadata":{"route":"chat"},"error":null}';
        const html = render([raw]);
        assert.match(html, /source=lens-ipp-abc\/ipp/);
        assert.ok(html.includes(new Date(1792158800125).toISOString()));
        assert.match(html, />WARN<\/span>/);
        assert.match(html, />Request queued<\/span>/);
        assert.match(html, /attempt=<\/span>0/);
        assert.match(html, /ready=<\/span>false/);
        assert.match(html, /metadata=<\/span>\{&quot;route&quot;:&quot;chat&quot;\}/);
        assert.match(html, /error=<\/span>null/);
    });

    test('ISO prefixes and millisecond timestamps are supported without fabricating missing times', () => {
        const html = render(['[pod/epp-abc/epp] 2026-10-16T13:00:00.123Z {"level":"info","message":"Ready"}']);
        assert.match(html, /title="2026-10-16T13:00:00.123Z"/);
        assert.match(html, />Ready<\/span>/);
        const pino = render(['{"time":1792158800125,"level":50,"msg":"Failed"}']);
        assert.ok(pino.includes(new Date(1792158800125).toISOString()));
        assert.match(pino, />ERROR<\/span>/);
        assert.doesNotMatch(render(['{"msg":"Ready"}']), /<time/);
    });

    test('EPP OpenTelemetry logs render severity_text and body as level and message', () => {
        const html = render(['[pod/optimized-baseline-epp-abc/epp] {"severity_text":"DEBUG","timestamp":"2026-10-16T08:57:24.144939124Z","caller":"handlers/server.go:493","body":"EPP received request","service.name":"llm-d-epp","x-request-id":"request-1","severity_number":5}']);
        assert.match(html, />DEBUG<\/span>/);
        assert.match(html, />EPP received request<\/span>/);
        assert.match(html, /title="2026-10-16T08:57:24.144939124Z"/);
        assert.match(html, /caller=<\/span>handlers\/server.go:493/);
        assert.match(html, /service.name=<\/span>llm-d-epp/);
        assert.match(html, /x-request-id=<\/span>request-1/);
        assert.match(html, /severity_number=<\/span>5/);
        assert.match(html, /source=optimized-baseline-epp-abc\/epp/);
        assert.doesNotMatch(html, /severity_text=|body=/);
    });

    test('EPP ERROR bodies and stacktraces preserve their multiline formatting', () => {
        const html = render([JSON.stringify({
            severity_text: 'ERROR',
            body: 'Request failed\n  upstream unavailable',
            stacktrace: 'Error: unavailable\n    at handleRequest',
        })]);
        assert.match(html, />ERROR<\/span>/);
        assert.match(html, />Request failed<\/span>/);
        assert.match(html, /<pre[^>]*> {2}upstream unavailable<\/pre>/);
        assert.ok(html.includes('Error: unavailable\n    at handleRequest'));
    });

    test('access records get readable request summaries while keeping every request field', () => {
        const raw = '{"start_time":"2026-10-16T13:00:00Z","method":"POST","path":"/v1/chat/completions","response_code":200,"duration":43}';
        const html = render([raw]);
        assert.match(html, /POST \/v1\/chat\/completions -&gt; 200/);
        assert.match(html, /method=<\/span>POST/);
        assert.match(html, /path=<\/span>\/v1\/chat\/completions/);
        assert.match(html, /response_code=<\/span>200/);
        assert.match(html, /duration=<\/span>43/);
    });

    test('structured records without a recognized message retain their fields', () => {
        const html = render(['{"component":"router","counts":[1,2],"message":{"nested":true}}']);
        assert.match(html, /component=<\/span>router/);
        assert.match(html, /counts=<\/span>\[1,2\]/);
        assert.match(html, /message=<\/span>\{&quot;nested&quot;:true\}/);
    });

    test('plain, malformed and multi-line logs remain readable and lossless', () => {
        for (const raw of ['Starting server', '[unrecognized] message', '[1,2]']) {
            assert.ok(render([raw]).includes(raw));
        }
        const raw = '[pod/ipp-abc/ipp] {"msg":broken}';
        const malformed = render([raw]);
        assert.match(malformed, /\{&quot;msg&quot;:broken\}/);
        assert.match(malformed, /Could not parse/);
        assert.match(render(['[ERROR] stream failed']), />ERROR<\/span>/);
        assert.match(render(['[error] stream failed']), />stream failed<\/span>/);
    });

    test('unknown severity values cannot resolve inherited object properties', () => {
        for (const level of ['constructor', '__proto__', 'custom']) {
            assert.ok(render([JSON.stringify({ level, msg: 'Unusual level' })]).includes(level.toUpperCase()));
        }
    });

    test('readable logs place metadata, message, context and source on one row', () => {
        const html = render(['[pod/epp-abc/epp] {"level":"warn","time":"2026-10-16T13:00:00Z","msg":"Pool unavailable","pool":"chat","details":{"ready":false}}']);
        assert.match(html, /role="log" aria-label="Component logs"/);
        assert.match(html, />WARN<\/span>/);
        assert.match(html, />Pool unavailable<\/span>/);
        assert.match(html, /source=epp-abc\/epp/);
        assert.match(html, /pool=<\/span>chat/);
        assert.match(html, /&quot;ready&quot;:false/);
        assert.match(html, /<li[^>]*whitespace-nowrap/);
        assert.doesNotMatch(html, /<details|<dl|<br/);
        assert.match(html, /Readable/);
        assert.match(html, />Raw<\/button>/);
        assert.match(html, /Follow latest/);
    });

    test('nested fields and embedded line breaks stay inline without losing content', () => {
        const html = render([JSON.stringify({ msg: 'First line\nSecond line\tindented', fields: { items: [1, 2] } })]);
        assert.match(html, /First line\\nSecond line\\tindented/);
        assert.match(html, /\{&quot;items&quot;:\[1,2\]\}/);
        assert.doesNotMatch(html, /\n/);
    });

    test('ERROR and FATAL stack fields preserve line breaks and indentation', () => {
        for (const level of ['error', 'fatal']) {
            for (const key of ['stacktrace', 'stack', 'stack_trace', 'exception.stacktrace']) {
                const html = render([JSON.stringify({ level, msg: 'Request failed', [key]: 'Error: unavailable\n    at route (router.go:42)\n\tat handler' })]);
                assert.match(html, />Request failed<\/span>/);
                assert.match(html, /<pre[^>]*whitespace-pre/);
                assert.ok(html.includes('Error: unavailable\n    at route (router.go:42)\n\tat handler'));
                assert.doesNotMatch(html, /\\n/);
            }
        }
    });

    test('multiline error messages retain their traceback but non-error stacks remain inline', () => {
        const error = render([JSON.stringify({ level: 'error', msg: 'Request failed\nTraceback:\n  at handler' })]);
        assert.match(error, />Request failed<\/span>/);
        assert.match(error, /<pre[^>]*>Traceback:\n {2}at handler<\/pre>/);
        const info = render([JSON.stringify({ level: 'info', stacktrace: 'First\n  second' })]);
        assert.match(info, /First\\n {2}second/);
        assert.doesNotMatch(info, /<pre/);
    });

    test('records without a message show fields inline and malformed JSON is marked', () => {
        const html = render(['{"phase":"initializing","items":[1,2]}', '{"msg":broken}']);
        assert.match(html, /phase=<\/span>initializing/);
        assert.match(html, /items=<\/span>\[1,2\]/);
        assert.match(html, /Unparsed JSON/);
        assert.match(html, /Could not parse structured log/);
    });

    test('log content is escaped and unknown levels stay neutral', () => {
        const html = render(['{"level":"constructor","msg":"<script>alert(1)</script>","field":"<img src=x>"}']);
        assert.match(html, /CONSTRUCTOR/);
        assert.match(html, /&lt;script&gt;/);
        assert.doesNotMatch(html, /<script|<img/);
    });

    test('empty viewer has an explicit waiting state', () => {
        assert.match(render([]), /Waiting for log output/);
    });

    test('following latest is enabled by default before and after logs arrive', () => {
        for (const lines of [[], ['Ready']]) {
            assert.match(render(lines), /<input[^>]*type="checkbox"[^>]*checked=""/);
        }
    });
});

describe("Model inference access client", () => {
    test('loads grants and only the purpose-limited recipient catalog', async (t) => {
        const urls = [];
        t.mock.method(globalThis, 'fetch', async (url, options) => {
            urls.push(url);
            assert.equal(options.credentials, 'include');
            return Response.json(url.endsWith('/options') ? { users: [{ id: 'u' }], groups: [] } : { items: [{ id: 'binding' }] });
        });
        assert.deepEqual(await getModelAccess('model/id'), { grants: [{ id: 'binding' }], users: [{ id: 'u' }], groups: [] });
        assert.deepEqual(urls.sort(), [
            '/api/v1/model-service/admin/groups/model%2Fid/access',
            '/api/v1/model-service/admin/groups/model%2Fid/access/options',
        ]);
    });

    test('grant and revoke use authenticated transport without caller-selected roles or scope', async (t) => {
        const calls = [];
        t.mock.method(globalThis, 'fetch', async (url, options) => {
            calls.push({ url, ...options });
            return options.method === 'DELETE' ? new Response(null, { status: 204 }) : Response.json({ id: 'binding' });
        });
        await grantModelAccess('m', { subjectType: 'group', subjectId: 'g', roleId: 'admin', clusterId: 'other' });
        await revokeModelAccess('m', 'binding/id');
        assert.deepEqual(JSON.parse(calls[0].body), { subjectType: 'group', subjectId: 'g' });
        assert.equal(calls[0].method, 'POST');
        assert.equal(calls[0].credentials, 'include');
        assert.equal(calls[1].method, 'DELETE');
        assert.equal(calls[1].credentials, 'include');
        assert.match(calls[1].url, /\/access\/binding%2Fid$/);
    });

    test('invalid catalogs and authorization errors are not presented as empty success', async (t) => {
        t.mock.method(globalThis, 'fetch', async () => Response.json({}));
        await assert.rejects(getModelAccess('m'), /Model access response is invalid/);
        t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Grant denied' }, { status: 403 }));
        await assert.rejects(grantModelAccess('m', { subjectType: 'user', subjectId: 'u' }), /Grant denied/);
    });
});

describe("Model API client", () => {
    const admin = '/api/v1/model-service/admin';
    const group = { id: 'existing', name: 'public-model', clusterId: 'cluster-a' };
    const input = { name: ' public-model ', clusterId: 'cluster-a', executionId: 'new-provider' };

    function mockApi(t, { groups = [group], members = [], mutation } = {}) {
        const writes = [];
        t.mock.method(globalThis, 'fetch', async (url, options) => {
            if (options.method && options.method !== 'GET') {
                writes.push({ url, method: options.method, body: JSON.parse(options.body) });
                assert.equal(options.credentials, 'include');
                return mutation(writes.at(-1));
            }
            if (url === `${admin}/groups`) return Response.json({ items: groups });
            if (url === `${admin}/members`) return Response.json({ items: members });
            throw new Error(`Unexpected API request: ${url}`);
        });
        return writes;
    }

    test('Add model reuses the same-cluster group, preserves weights and gives the new deployment weight one', async (t) => {
        const members = [
            { groupId: group.id, clusterId: group.clusterId, executionId: 'stable', weight: 90 },
            { groupId: group.id, clusterId: group.clusterId, executionId: 'canary', weight: 10 },
            { groupId: 'other', clusterId: group.clusterId, executionId: 'unrelated', weight: 1 },
        ];
        const writes = mockApi(t, {
            members,
            mutation: () => Response.json({ items: [], operation: { status: 'succeeded' } }),
        });
        const result = await addModel(input);
        assert.equal(result.created, false);
        assert.equal(result.group.id, group.id);
        assert.equal(result.weight, 1);
        assert.deepEqual(writes, [{
            url: `${admin}/groups/existing/traffic`, method: 'PUT',
            body: {
                expectedWeights: { stable: 90, canary: 10 },
                members: [
                    { executionId: 'stable', weight: 90 },
                    { executionId: 'canary', weight: 10 },
                    { executionId: 'new-provider', weight: 1 },
                ],
            },
        }]);
    });

    test('the same public name in another cluster still creates a new model', async (t) => {
        const writes = mockApi(t, {
            groups: [{ ...group, clusterId: 'cluster-b' }],
            mutation: () => Response.json({ id: 'new-model' }, { status: 201 }),
        });
        const result = await addModel(input);
        assert.equal(result.created, true);
        assert.deepEqual(writes, [{
            url: `${admin}/groups`, method: 'POST',
            body: { ...input, name: 'public-model' },
        }]);
    });

    test('the first provider of an empty existing model receives weight one', async (t) => {
        const writes = mockApi(t, {
            mutation: () => Response.json({ items: [], operation: { status: 'succeeded' } }),
        });
        assert.equal((await addModel(input)).weight, 1);
        assert.deepEqual(writes[0].body, {
            expectedWeights: {}, members: [{ executionId: 'new-provider', weight: 1 }],
        });
    });

    test('a deployment published after the picker loaded is not added again', async (t) => {
        const writes = mockApi(t, {
            members: [{ groupId: 'other', clusterId: input.clusterId, executionId: input.executionId, weight: 0 }],
        });
        await assert.rejects(addModel(input), /already been added/);
        assert.deepEqual(writes, []);
    });

    test('incompatible providers or concurrent traffic edits surface the API conflict without retrying creation', async (t) => {
        const writes = mockApi(t, {
            mutation: () => Response.json({ detail: 'Provider model mismatch', code: 'model_traffic_conflict' }, { status: 409 }),
        });
        await assert.rejects(addModel(input), (error) => error.status === 409 && error.message === 'Provider model mismatch');
        assert.equal(writes.length, 1);
        assert.equal(writes[0].method, 'PUT');
    });

    test('a saved-but-failed reconcile stays distinguishable from a successful addition', async (t) => {
        const operation = { status: 'failed', message: 'Gateway unavailable' };
        mockApi(t, { mutation: () => Response.json({ items: [], operation }) });
        assert.deepEqual((await addModel(input)).operation, operation);
    });
});

describe("Model state", () => {
    test('weights accept only whole numbers in the official Gateway API range', () => {
        for (const value of ['', ' ', '-1', '1.5', '1e3', '1000001', true, null, undefined]) {
            assert.equal(parseTrafficWeight(value), null);
        }
        for (const value of [0, 1, 1000000, '0', '1000000']) assert.equal(parseTrafficWeight(value), Number(value));
    });

    test('relative shares exclude disabled providers and handle zero and tiny allocations', () => {
        const rows = [
            { executionId: 'a', weight: '90', status: 'active' },
            { executionId: 'b', weight: '10', status: 'unhealthy' },
            { executionId: 'c', weight: '100', status: 'disabled' },
            { executionId: 'd', weight: '0', status: 'active' },
        ];
        assert.equal(trafficTotal(rows), 100);
        assert.deepEqual(rows.map((row) => trafficShare(row, 100)), ['90%', '10%', '0%', '0%']);
        assert.equal(trafficShare({ weight: 1 }, 16000000), '<0.01%');
        assert.equal(trafficDistributionError(rows), '');
        assert.equal(trafficTotal([{ weight: '' }]), null);
        assert.equal(trafficShare({ weight: '' }, null), 'Unknown');
    });

    test('allocations reject all-zero, duplicate, empty and oversized provider lists', () => {
        for (const rows of [
            [],
            [{ executionId: 'a', weight: 0 }],
            [{ executionId: 'a', weight: 1, status: 'disabled' }],
            [{ executionId: 'a', weight: 1 }, { executionId: 'a', weight: 1 }],
            Array.from({ length: 17 }, (_, index) => ({ executionId: String(index), weight: 1 })),
        ]) assert.ok(trafficDistributionError(rows));
    });

    const clusters = ['a', 'b', 'empty'].map((clusterId) => ({ clusterId, clusterName: `Cluster ${clusterId}` }));
    const providers = [
        { memberId: 'primary', name: 'provider-name', status: 'active', weight: 90 },
        { memberId: 'standby', name: 'standby-name', status: 'active', weight: 0 },
        { memberId: 'offline', name: 'offline-name', status: 'disabled', weight: 10 },
    ];
    const memberGroups = [
        { id: 'chat', name: 'Public-Chat', status: 'disabled', clusters: [
            { clusterId: 'a', deployments: providers },
            { clusterId: 'b', deployments: [{ memberId: 'other', weight: 1 }] },
        ] },
        { id: 'code', name: 'Public-Code', status: 'active', clusters: [
            { clusterId: 'b', deployments: [{ memberId: 'code-provider', weight: 1 }] },
        ] },
    ];

    test('unfiltered flow preserves empty clusters, all models, statuses and weights', () => {
        assert.deepEqual(filterServiceFlow(clusters, memberGroups), { clusters, memberGroups });
        assert.deepEqual(filterServiceFlow(clusters, memberGroups, { modelQuery: '  ' }), { clusters, memberGroups });
    });

    test('cluster and case-insensitive partial public model filters intersect without filtering providers', () => {
        const result = filterServiceFlow(clusters, memberGroups, { clusterId: 'a', modelQuery: '  CHAT  ' });
        assert.deepEqual(result.clusters, [clusters[0]]);
        assert.equal(result.memberGroups.length, 1);
        assert.equal(result.memberGroups[0].id, 'chat');
        assert.equal(result.memberGroups[0].status, 'disabled');
        assert.equal(result.memberGroups[0].clusters.length, 1);
        assert.equal(result.memberGroups[0].clusters[0].deployments, providers);
        assert.equal(memberGroups[0].clusters.length, 2, 'filtering does not mutate source data');
    });

    test('public model search excludes unrelated clusters and does not search provider names', () => {
        const result = filterServiceFlow(clusters, memberGroups, { modelQuery: 'code' });
        assert.deepEqual(result.clusters, [clusters[1]]);
        assert.deepEqual(result.memberGroups.map((group) => group.id), ['code']);
        assert.deepEqual(filterServiceFlow(clusters, memberGroups, { modelQuery: 'provider-name' }), { clusters: [], memberGroups: [] });
    });

    test('empty clusters remain manageable while unmatched combinations and deleted clusters are empty', () => {
        assert.deepEqual(filterServiceFlow(clusters, memberGroups, { clusterId: 'empty' }), { clusters: [clusters[2]], memberGroups: [] });
        for (const filters of [{ clusterId: 'a', modelQuery: 'code' }, { clusterId: 'deleted' }]) {
            assert.deepEqual(filterServiceFlow(clusters, memberGroups, filters), { clusters: [], memberGroups: [] });
        }
    });

    test('the same public name remains visible in every matching cluster', () => {
        const result = filterServiceFlow(clusters, memberGroups, { modelQuery: 'chat' });
        assert.deepEqual(result.clusters, clusters.slice(0, 2));
        assert.equal(result.memberGroups[0].clusters.length, 2);
    });
});

describe("Flow connectors", () => {
    test('active connectors keep fine lines and fade the sparse tail toward its rear', () => {
        for (const [tone, color] of [['blue', 'sky'], ['green', 'emerald']]) {
            const html = renderToStaticMarkup(<FlowConnector d="M 10 20 H 180" tone={tone} width={1.4} />);
            assert.equal((html.match(/d="M 10 20 H 180"/g) || []).length, 6);
            assert.match(html, /stroke-linecap="round" stroke-linejoin="round"/);
            assert.match(html, /blur-\[2px\]/);
            assert.match(html, /stroke-width="1.4"/);
            assert.match(html, /stroke-width="3.9"/);
            assert.match(html, /lens-chain-flow-head/);
            assert.ok(html.includes(`dark:stroke-${color}-100`));
            assert.ok(html.includes(`stroke-${color}-600`));
            assert.equal((html.match(/motion-reduce:hidden/g) || []).length, 5);
            const tail = [...html.matchAll(/<path[^>]*stroke-dasharray="6 154"[^>]*opacity="([^"]+)"/g)];
            assert.deepEqual(tail.map((match) => Number(match[1])), [0.1, 0.25, 0.5]);
            assert.match(html, /--flow-offset:-6px/);
            assert.match(html, /--flow-offset:-12px/);
            assert.match(html, /stroke-dasharray="3 157"/);
        }
    });

    test('rounded corners retain endpoints, clamp short bends and tolerate repeated vertices', () => {
        for (const [points, path] of [
            [[[10, 20], [100, 20], [100, 80], [180, 80]], 'M 10 20 H 88 Q 100 20 100 32 V 68 Q 100 80 112 80 H 180'],
            [[[10, 20], [16, 20], [16, 30]], 'M 10 20 H 13 Q 16 20 16 23 V 30'],
            [[[10, 80], [10, 20], [100, 20]], 'M 10 80 V 32 Q 10 20 22 20 H 100'],
            [[[10, 20], [10, 20], [180, 20]], 'M 10 20 H 180'],
        ]) {
            const html = renderToStaticMarkup(<FlowConnector points={points} />);
            assert.ok(html.includes(`d="${path}"`));
            assert.doesNotMatch(html, /NaN|Infinity/);
        }
    });

    test('inactive connectors retain only their static base without glow or traveling highlights', () => {
        for (const props of [{ dotted: true }, { dotted: true, tone: 'slate' }, { tone: 'slate' }]) {
            const html = renderToStaticMarkup(<FlowConnector d="M 10 20 H 180" {...props} />);
            assert.equal((html.match(/<path /g) || []).length, 1);
            assert.doesNotMatch(html, /lens-chain-flow|blur-/);
            if (props.dotted) assert.match(html, /stroke-dasharray="1 4"/);
        }
    });
});

describe("Page navigation", () => {
    test('model-service cluster navigation initializes an editable filter before gateway data loads', () => {
        for (const initialClusterId of [undefined, 'cluster-a']) {
            const html = renderToStaticMarkup(createElement(
                AuthContext.Provider,
                { value: { can: () => true } },
                createElement(ModelServiceAdminPage, { initialClusterId }),
            ));
            const filter = html.match(/<select[^>]*id="service-flow-cluster"[^>]*>[^]*?<\/select>/)?.[0];
            assert.ok(filter);
            assert.ok(filter.includes(`<option value="${initialClusterId || ''}" selected="">`));
            assert.doesNotMatch(filter, /\sdisabled=/);
        }
    });

    test('cluster navigation initializes an editable filter even before deployments load', () => {
        for (const initialClusterId of [undefined, 'cluster-a']) {
            const html = renderToStaticMarkup(createElement(
                AuthContext.Provider,
                { value: { can: () => true } },
                createElement(DeploymentManagementPage, { initialClusterId }),
            ));
            const filter = html.match(/<select[^>]*aria-label="Filter by cluster"[^>]*>[^]*?<\/select>/)?.[0];
            assert.ok(filter);
            assert.ok(filter.includes(`<option value="${initialClusterId || ''}" selected="">`));
            assert.doesNotMatch(filter, /\sdisabled=/);
        }
    });

    test('deployment search starts with the navigation filter and is empty on ordinary entry', () => {
        for (const initialSearch of [undefined, 'llm-d-qwen-a1b2']) {
            const html = renderToStaticMarkup(createElement(
                AuthContext.Provider,
                { value: { can: () => true } },
                createElement(DeploymentManagementPage, { initialSearch }),
            ));
            const input = html.match(/<input[^>]*aria-label="Search deployments"[^>]*>/);
            assert.ok(input);
            assert.ok(input[0].includes(`value="${initialSearch || ''}"`));
            assert.doesNotMatch(input[0], /\s(?:readOnly|disabled)=/);
        }
    });
});
