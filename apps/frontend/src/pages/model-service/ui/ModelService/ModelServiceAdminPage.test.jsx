import test, { describe } from 'node:test';
import assert from 'node:assert/strict';
import React, { act, createElement, Children } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { ApiKeysPage } from './ApiKeysPage.jsx';
import {
    AddModelForm,
    applyGatewayConfigSaved,
    buildGatewayConfigPatch,
    fetchModelServiceAdminSnapshot,
    handoffModalSubtitle,
    ModelServiceAdminPage,
    ModelServiceHandoffBanner,
    ModelServiceHandoffPanel,
    resolveGatewayConfigFields,
    submitGatewayConfig,
    TrafficDistributionForm,
} from './ModelServiceAdminPage.jsx';
import { AuthContext } from '../../../auth/AuthContext.js';
import { availableModelProviders, buildHandoffModalState, buildModelCurlSamples, connectionAddressLabel, connectionEndpointUrl, effectiveTrafficShare, filterServiceFlow, gatewayAddressLabel, memberIsRoutable, parseTrafficWeight, posixSingleQuote, routedTrafficTotal, sanitizeHttpUrl, trafficDistributionError, trafficShare, trafficTotal } from '../../model/modelService.js';
import { clusterCapabilities, clusterDot, ComponentLogAction, DataPlaneTopology, gatewayDetail, ippDetailText, ippDot, ManageDeploymentAction, PublicationToggleAction, ProvidersToggleAction, TopologyMenuItems, Flow as FlowConnector } from './DataPlaneTopology.jsx';
import { ClusterConnectionCard } from './ModelServicePage.jsx';
import { ComponentLogViewer } from './GatewayOperationLogDrawer.jsx';
import { addModel, applyMutationHandoffResult, clusterHandoffMode, componentHandoffAction, deleteGroup, deleteMember, fetchClusterHandoff, generateGatewayRemediation, getGatewayRemediation, getModelAccess, grantModelAccess, performClusterBannerRecheck, performHandoffRecheck, performHandoffScriptRefresh, revokeModelAccess, setGroupStatus, setProvidersStatus, verifyGatewayRemediation } from './modelServiceBackend.js';
import { DeploymentManagementPage } from '../../../deployment/ui/DeploymentManagement/DeploymentManagementPage.jsx';

describe("Add model", () => {
    const deployment = { executionId: 'exec-a', name: 'deployment-a', model: 'Qwen/Qwen3-8B', endpointKind: 'llm-d-epp' };
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
        assert.match(html, /No available ready llm-d deployments in this cluster/);
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
        deployments.push({ ...deployment, executionId: 'plain-vllm', clusterId: 'cluster-a', status: 'ready', endpointKind: 'vllm' });
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

    test('plain vLLM deployments are never offered as Model Service providers', () => {
        const plain = { ...deployment, executionId: 'plain-vllm', clusterId: 'cluster-a', status: 'ready', endpointKind: 'vllm' };
        const epp = { ...deployment, clusterId: 'cluster-a', status: 'ready' };
        assert.deepEqual(
            availableModelProviders([plain, epp], [], 'cluster-a').map((item) => item.executionId),
            ['exec-a'],
        );
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
        assert.match(html, /deployments already added to a model are excluded/);
        assert.deepEqual(availableModelProviders(deployments, [], defaults.form.clusterId), deployments);
    });

    test('Service Flow exposes Add model and Reconcile with their existing permissions', () => {
        const renderPage = (permissions) => renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: (permission) => permissions.includes(permission) } },
            createElement(ModelServiceAdminPage),
        ));
        const html = renderPage(['model-service:member:manage', 'model-service:gateway:reconcile']);
        const header = html.match(/<header[^]*?<\/header>/)[0];
        assert.doesNotMatch(header, /Add model<\/button>|Reconcile<\/button>/);
        assert.match(header, /Publish models and manage their serving routes\./);
        const flow = html.slice(html.indexOf('>Service Flow</'));
        assert.match(flow, /<button[^>]*bg-sky-500[^>]*>[^]*?Add model<\/button>/);
        assert.match(flow, /Reconcile<\/button>/);
        assert.ok(flow.indexOf('Add model</button>') < flow.indexOf('aria-label="Filter service flow"'));
        assert.equal((html.match(/Add model<\/button>/g) || []).length, 1);
        assert.equal((html.match(/Reconcile<\/button>/g) || []).length, 1);
        assert.doesNotMatch(html, /Add model service|Add model server/);
        assert.doesNotMatch(html, /cluster\(s\)|member\(s\)|model service\(s\)|route\(s\)/);
        assert.doesNotMatch(renderPage(['model-service:group:manage']), /Add model</);
        assert.match(renderPage(['model-service:member:manage']), /Add model<\/button>/);
        assert.doesNotMatch(renderPage(['model-service:gateway:reconcile']), /Add model<\/button>/);
        assert.doesNotMatch(renderPage(['model-service:gateway:manage']), /Reconcile<\/button>/);
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

    test('traffic form excludes deployments already attached to another public model', () => {
        const html = renderToStaticMarkup(createElement(TrafficDistributionForm, {
            draft, deployments, members: [{ executionId: 'b', groupId: 'other-model' }],
        }));
        assert.doesNotMatch(html, /Canary deployment/);
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
                            components: { httpRoute: 'ready', inferencePool: 'ready', epp: 'ready', modelServer: 'ready' },
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
        assert.match(html, /Effective request share after unavailable providers leave the route; not measured traffic/);
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
        assert.match(html, /class="flex h-full min-h-0 w-full flex-col gap-3"/);
        assert.match(html, /class="min-h-0 w-full flex-1 cursor-grab/);
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
        assert.match(html, /0%<\/text>/);
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
            assert.match(frame, /title="EPP: active"/);
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
                assert.match(box, /group-hover:visible group-focus-within:visible group-hover:pointer-events-auto group-focus-within:pointer-events-auto right-0 w-80 bottom-full pb-2/);
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
        assert.match(html, /First line\\nSecond line\tindented/);
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
            url: `${admin}/groups/publish`, method: 'POST',
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

    test('unavailable providers leave the effective route but keep their configured weight', () => {
        const members = [
            { executionId: 'healthy', status: 'active', weight: 1, endpointKind: 'llm-d-epp', components: { modelServer: 'ready', epp: 'ready', inferencePool: 'ready' } },
            { executionId: 'failed', status: 'unhealthy', weight: 1, endpointKind: 'llm-d-epp', components: { modelServer: 'missing', epp: 'missing', inferencePool: 'degraded' } },
        ];
        assert.equal(memberIsRoutable(members[0]), true);
        assert.equal(memberIsRoutable(members[1]), false);
        assert.equal(routedTrafficTotal(members), 1);
        assert.deepEqual(members.map((member) => effectiveTrafficShare(member, 1)), ['100%', '0%']);
        assert.equal(members[1].weight, 1);
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

    test('model service status filters active and disabled groups', () => {
        const disabled = filterServiceFlow(clusters, memberGroups, { status: 'disabled' });
        assert.deepEqual(disabled.clusters, clusters.slice(0, 2));
        assert.deepEqual(disabled.memberGroups.map((group) => group.id), ['chat']);

        const active = filterServiceFlow(clusters, memberGroups, { status: 'active' });
        assert.deepEqual(active.clusters, [clusters[1]]);
        assert.deepEqual(active.memberGroups.map((group) => group.id), ['code']);

        assert.deepEqual(filterServiceFlow(clusters, memberGroups, { status: 'unknown' }), { clusters: [], memberGroups: [] });
    });

    test('status combines with cluster and public model filters', () => {
        const result = filterServiceFlow(clusters, memberGroups, {
            clusterId: 'a',
            status: 'disabled',
            modelQuery: 'chat',
        });
        assert.deepEqual(result.clusters, [clusters[0]]);
        assert.deepEqual(result.memberGroups.map((group) => group.id), ['chat']);

        assert.deepEqual(
            filterServiceFlow(clusters, memberGroups, { clusterId: 'b', status: 'disabled', modelQuery: 'code' }),
            { clusters: [], memberGroups: [] },
        );
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

describe("Non-Admin K8s capability & handoff UX", () => {
    const statusDot = { active: 'bg-emerald-400', stopped: 'bg-slate-500', standby: 'bg-slate-500', unhealthy: 'bg-rose-500', unknown: 'bg-amber-400' };

    test('capability helpers default to Admin when capability fields are absent', () => {
        assert.deepEqual(clusterCapabilities(), {
            canManageInfra: true,
            canManageConfig: true,
            canReconcile: true,
            isNonAdmin: false,
        });
        assert.deepEqual(clusterCapabilities({ clusterId: 'c1' }), {
            canManageInfra: true,
            canManageConfig: true,
            canReconcile: true,
            isNonAdmin: false,
        });
        assert.deepEqual(clusterCapabilities({
            accessMode: 'non_admin',
            canManageGatewayInfra: false,
            canManageGatewayConfig: false,
            canReconcileRoutes: false,
        }), {
            canManageInfra: false,
            canManageConfig: false,
            canReconcile: false,
            isNonAdmin: true,
        });
    });

    test('Non-Admin topology renders badges, restricted status tooltips and explicit IPP/HTTPRoute permissions', () => {
        const nonAdminCluster = {
            clusterId: 'c-nonadmin',
            clusterName: 'Non-Admin Cluster',
            accessMode: 'non_admin',
            canManageGatewayInfra: false,
            canManageGatewayConfig: false,
            canReconcileRoutes: false,
            reconcileStatus: 'admin_handoff_required',
            gatewayState: 'unknown',
            ippState: 'unknown',
        };
        const html = renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: (perm) => perm === 'model-service:member:manage' || perm === 'model-service:gateway:reconcile' } },
            createElement(DataPlaneTopology, {
                clusters: [nonAdminCluster],
                memberGroups: [{
                    id: 'g1',
                    name: 'chat',
                    status: 'active',
                    clusters: [{
                        clusterId: 'c-nonadmin',
                        deployments: [{
                            memberId: 'm1',
                            clusterId: 'c-nonadmin',
                            name: 'dep-1',
                            status: 'active',
                            weight: 1,
                            components: { httpRoute: 'unknown', inferencePool: 'ready', epp: 'ready', modelServer: 'ready' },
                        }],
                    }],
                }],
                statusDot,
                onComponentAction: () => {},
            }),
        ));
        assert.match(html, />Non-Admin</);
        assert.match(html, />Orchestration administrator sync required</);
        assert.match(html, /title="Gateway: status restricted \(non-admin\) \(unknown\)"/);
        assert.match(html, /title="IPP: status restricted \(non-admin\) \(unknown\)"/);
        assert.match(html, /title="HTTPRoute: status restricted \(non-admin\)"/);
        assert.doesNotMatch(html, /not installed/);
        assert.match(html, /aria-label="IPP actions"/);
        assert.match(html, /aria-label="HTTPRoute actions"/);

        const memberOnlyHtml = renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: (perm) => perm === 'model-service:member:manage' } },
            createElement(DataPlaneTopology, {
                clusters: [nonAdminCluster],
                memberGroups: [{
                    id: 'g1',
                    name: 'chat',
                    status: 'active',
                    clusters: [{
                        clusterId: 'c-nonadmin',
                        deployments: [{
                            memberId: 'm1',
                            clusterId: 'c-nonadmin',
                            name: 'dep-1',
                            status: 'active',
                            weight: 1,
                            components: { httpRoute: 'unknown', inferencePool: 'ready', epp: 'ready', modelServer: 'ready' },
                        }],
                    }],
                }],
                statusDot,
                onComponentAction: () => {},
            }),
        ));
        assert.match(memberOnlyHtml, /aria-label="HTTPRoute actions"/);
        assert.doesNotMatch(memberOnlyHtml, /aria-label="IPP actions"/);

        const authErrCluster = { ...nonAdminCluster, authError: '401 Unauthorized' };
        assert.equal(clusterDot(authErrCluster), 'unhealthy');
        assert.equal(gatewayDetail(authErrCluster), 'authentication failed');
        assert.equal(ippDot(authErrCluster), 'unhealthy');
        assert.equal(ippDetailText(authErrCluster), 'authentication failed');

        const authErrHtml = renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: () => true } },
            createElement(DataPlaneTopology, { clusters: [authErrCluster], memberGroups: [], statusDot }),
        ));
        assert.match(authErrHtml, />Auth error</);

        assert.equal(gatewayDetail({ gatewayState: 'installed', gatewayReady: true, gatewayProbeSource: 'http' }), 'ready (HTTP probe)');
        assert.equal(gatewayDetail({ gatewayState: 'installed', gatewayReady: true, gatewayProbeSource: 'prometheus' }), 'ready (Prometheus)');
        assert.equal(gatewayDetail({ gatewayState: 'installed', gatewayReady: true }), 'ready');
    });

    test('Non-Admin topology menus hide infra/log actions and expose handoff script actions while Admin stays unchanged', () => {
        const adminCluster = {
            clusterId: 'c-admin',
            clusterName: 'Admin',
            gatewayProvider: 'istio',
            gatewayState: 'installed',
            gatewayRunning: true,
            gatewayReady: true,
            ippState: 'installed',
            ippReady: true,
        };
        const nonAdminCluster = {
            ...adminCluster,
            clusterId: 'c-nonadmin',
            clusterName: 'NonAdmin',
            accessMode: 'non_admin',
            canManageGatewayInfra: false,
            canManageGatewayConfig: false,
            canReconcileRoutes: false,
        };
        const layout = [{
            cluster: nonAdminCluster,
            rows: [{ serviceId: 'g1', serviceName: 'chat', serviceStatus: 'active', members: [] }],
        }];
        const adminLayout = [{
            cluster: adminCluster,
            rows: [{ serviceId: 'g1', serviceName: 'chat', serviceStatus: 'active', members: [] }],
        }];

        const renderMenu = (menuKey, clusters, menuLayout) => renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: () => true } },
            createElement(TopologyMenuItems, { menuKey, clusters, layout: menuLayout, onComponentAction: () => {} }),
        ));

        const nonAdminGw = renderMenu('gw:c-nonadmin', [nonAdminCluster], layout);
        assert.match(nonAdminGw, / Configure<\/button>/);
        assert.match(nonAdminGw, / Orchestration administrator sync script<\/button>/);
        assert.match(nonAdminGw, / Gateway repair script<\/button>/);
        assert.doesNotMatch(nonAdminGw, /Install<\/button>|Uninstall<\/button>|Start<\/button>|Stop<\/button>|View logs<\/button>|Reconcile<\/button>/);

        const adminGw = renderMenu('gw:c-admin', [adminCluster], adminLayout);
        assert.match(adminGw, / Stop<\/button>/);
        assert.match(adminGw, / Reconcile<\/button>/);
        assert.match(adminGw, / Uninstall<\/button>/);
        assert.match(adminGw, / View logs<\/button>/);
        assert.doesNotMatch(adminGw, /Orchestration administrator sync script|Gateway repair script/);

        const nonAdminIpp = renderMenu('ipp:c-nonadmin', [nonAdminCluster], layout);
        assert.match(nonAdminIpp, / Configure<\/button>/);
        assert.match(nonAdminIpp, / IPP config script<\/button>/);
        assert.doesNotMatch(nonAdminIpp, /Install<\/button>|Uninstall<\/button>|Start<\/button>|Stop<\/button>|View logs<\/button>/);

        const adminIpp = renderMenu('ipp:c-admin', [adminCluster], adminLayout);
        assert.match(adminIpp, / Stop<\/button>/);
        assert.match(adminIpp, / Uninstall<\/button>/);
        assert.match(adminIpp, / View logs<\/button>/);
        assert.doesNotMatch(adminIpp, /IPP config script/);

        const nonAdminRoute = renderMenu('route:c-nonadmin:g1', [nonAdminCluster], layout);
        assert.match(nonAdminRoute, / Orchestration administrator sync script<\/button>/);
        assert.doesNotMatch(nonAdminRoute, /Reconcile route/);

        const adminRoute = renderMenu('route:c-admin:g1', [adminCluster], adminLayout);
        assert.match(adminRoute, / Reconcile route<\/button>/);
    });

    test('modelServiceBackend remediation, verify, delete handoff, setProvidersStatus, and addModel helpers', async (t) => {
        const requests = [];
        const sampleHandoff = {
            clusterId: 'c1',
            reconcileStatus: 'admin_handoff_required',
            desiredHash: 'hash123',
            adminScript: '#!/usr/bin/env bash\necho ok\n',
        };
        t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
            requests.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
            if (url.includes('/gateway/remediation/verify')) {
                return Response.json({ clusterId: 'c1', verified: true, reconcileStatus: 'synced' });
            }
            if (url.includes('/gateway/remediation')) {
                return Response.json(sampleHandoff);
            }
            if (url.endsWith('/groups/g1') && options.method === 'DELETE') {
                return Response.json({ deleted: true, groupId: 'g1', reconcileStatus: 'admin_handoff_required', handoff: sampleHandoff });
            }
            if (url.endsWith('/members/m1') && options.method === 'DELETE') {
                return new Response(null, { status: 204 });
            }
            if (url.endsWith('/groups/g1/providers/status')) {
                return Response.json({
                    items: [{ id: 'm1', status: 'disabled', weight: 1 }],
                    reconcileStatus: 'admin_handoff_required',
                    handoff: sampleHandoff,
                });
            }
            if (url.endsWith('/admin/groups')) {
                return Response.json({ items: [{ id: 'g1', name: 'existing-model', clusterId: 'c1' }] });
            }
            if (url.endsWith('/admin/members')) {
                return Response.json({ items: [] });
            }
            if (url.endsWith('/admin/groups/publish')) {
                return Response.json({ id: 'g2', name: 'new-model', reconcileStatus: 'admin_handoff_required', handoff: sampleHandoff }, { status: 201 });
            }
            if (url.endsWith('/admin/groups/g1/traffic')) {
                return Response.json({ items: [], operation: { status: 'succeeded' }, reconcileStatus: 'admin_handoff_required', handoff: sampleHandoff });
            }
            throw new Error(`Unexpected URL ${url}`);
        });

        const getRes = await getGatewayRemediation('c1', { mode: 'routes', markerNamespace: 'ns-a' });
        assert.equal(getRes.desiredHash, 'hash123');
        assert.match(requests[0].url, /\/api\/v1\/model-service\/admin\/gateway\/remediation\?clusterId=c1&mode=routes&markerNamespace=ns-a$/);

        const postRes = await generateGatewayRemediation({ clusterId: 'c1', mode: 'ipp_config', ippConfig: 'key: val' });
        assert.equal(postRes.reconcileStatus, 'admin_handoff_required');
        assert.equal(requests[1].method, 'POST');
        assert.deepEqual(requests[1].body, { clusterId: 'c1', mode: 'ipp_config', ippConfig: 'key: val' });

        const verifyRes = await verifyGatewayRemediation({ clusterId: 'c1', expectedHash: 'hash123' });
        assert.equal(verifyRes.verified, true);
        assert.equal(requests[2].method, 'POST');
        assert.match(requests[2].url, /\/gateway\/remediation\/verify$/);

        const delGroupRes = await deleteGroup('g1');
        assert.equal(delGroupRes.reconcileStatus, 'admin_handoff_required');
        assert.equal(delGroupRes.handoff.desiredHash, 'hash123');

        const delMemberRes = await deleteMember('m1');
        assert.deepEqual(delMemberRes, {});

        const statusItems = await setProvidersStatus('g1', 'disabled');
        assert.equal(statusItems.length, 1);
        assert.equal(statusItems.reconcileStatus, 'admin_handoff_required');
        assert.equal(statusItems.handoff.desiredHash, 'hash123');
        assert.deepEqual(Object.keys(statusItems), ['0']);

        const createdRes = await addModel({ name: 'new-model', clusterId: 'c1', executionId: 'e1' });
        assert.equal(createdRes.created, true);
        assert.equal(createdRes.reconcileStatus, 'admin_handoff_required');
        assert.equal(createdRes.handoff.desiredHash, 'hash123');

        const updatedRes = await addModel({ name: 'existing-model', clusterId: 'c1', executionId: 'e2' });
        assert.equal(updatedRes.created, false);
        assert.equal(updatedRes.reconcileStatus, 'admin_handoff_required');
        assert.equal(updatedRes.handoff.desiredHash, 'hash123');
    });

    test('ModelServiceHandoffPanel renders script actions, details, and verification banners without (s) copy', () => {
        const handoff = {
            clusterId: 'c1',
            accessMode: 'non_admin',
            reconcileStatus: 'admin_handoff_required',
            desiredHash: 'deadbeef12345678',
            markerNamespace: 'lens-edit',
            markerName: 'lens-ms-handoff-deadbeef12345678',
            filename: 'lens-ms-c1-routes.sh',
            adminScript: '#!/usr/bin/env bash\nset -euo pipefail\nkubectl apply -f -\n',
            instructions: 'Run this script once with Orchestration administrator kubeconfig.',
            pendingOperations: [
                { id: 'op-1', action: 'apply', kind: 'HTTPRoute', namespace: 'lens-gateway', name: 'chat-route', summary: 'Apply HTTPRoute' },
                { id: 'op-2', action: 'delete', kind: 'InferenceModelRewrite', namespace: 'ns-a', name: 'old-rw', summary: 'Delete stale rewrite' },
            ],
            warnings: ['Gateway namespace reads are restricted.'],
            mode: 'routes',
        };

        const baseHtml = renderToStaticMarkup(createElement(ModelServiceHandoffPanel, {
            handoff,
            clusterName: 'Cluster One',
            onRecheck: () => {},
        }));
        assert.match(baseHtml, /Orchestration administrator route sync — Cluster One/);
        assert.match(baseHtml, />Awaiting Orchestration administrator</);
        assert.match(baseHtml, /Download<\/button>/);
        assert.match(baseHtml, /Copy<\/button>/);
        assert.match(baseHtml, /Re-check<\/button>/);
        assert.match(baseHtml, /View operation details/);
        assert.match(baseHtml, /<details class="group\/details[^"]*" open="">/);
        assert.match(baseHtml, /2 pending/);
        assert.match(baseHtml, /HTTPRoute lens-gateway\/chat-route/);
        assert.match(baseHtml, /InferenceModelRewrite ns-a\/old-rw/);
        assert.match(baseHtml, /deadbeef12345678/);
        assert.match(baseHtml, /lens-edit\/lens-ms-handoff-deadbeef12345678/);
        assert.match(baseHtml, /<pre[^>]*>[^]*kubectl apply -f -/);
        assert.match(baseHtml, /Gateway namespace reads are restricted\./);
        assert.doesNotMatch(baseHtml, /\(s\)/);

        for (const [verifyResult, expectedText] of [
            [{ verified: true, markerStatus: 'complete', message: 'All routes synced.', routeChecks: [{ name: 'chat', status: 'ready', httpStatus: 200 }] }, /Verified.*All routes synced\./],
            [{ verified: false, markerStatus: 'partial', message: '' }, /Partial.*Orchestration administrator handoff is partial/],
            [{ verified: false, markerStatus: 'stale', message: '' }, /Stale.*belongs to an older route state/],
            [{ verified: false, markerStatus: 'missing', message: '' }, /Missing marker.*Completion marker was not found/],
            [{ verified: false, markerStatus: 'forbidden', message: '' }, /Unconfirmed.*403 Forbidden/],
        ]) {
            const html = renderToStaticMarkup(createElement(ModelServiceHandoffPanel, {
                handoff,
                verifyResult,
                onRecheck: () => {},
            }));
            assert.match(html, expectedText);
            if (verifyResult.verified) {
                assert.match(html, /<details class="group\/details/);
                assert.doesNotMatch(html, /<details[^>]*open/);
            } else {
                assert.match(html, /<details class="group\/details[^"]*" open="">/);
            }
            assert.doesNotMatch(html, /\(s\)/);
        }

        const syncedHtml = renderToStaticMarkup(createElement(ModelServiceHandoffPanel, {
            handoff: { ...handoff, reconcileStatus: 'synced' },
            onRecheck: () => {},
        }));
        assert.match(syncedHtml, /Verified/);
        assert.match(syncedHtml, /0 pending/);
        assert.match(syncedHtml, /<details class="group\/details/);
        assert.doesNotMatch(syncedHtml, /<details[^>]*open/);
    });

    test('recheckHandoff marks verified state cleanly, clears cluster banner, and renders verified feedback', async (t) => {
        const calls = [];
        t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
            calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
            if (url.endsWith('/gateway/remediation/verify')) {
                return Response.json({
                    clusterId: 'c1',
                    verified: true,
                    reconcileStatus: 'synced',
                    desiredHash: 'aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff66660000111122223333',
                    markerStatus: 'complete',
                    markerNamespace: 'lens-edit',
                    markerName: 'lens-ms-handoff-aaaa1111bbbb2222',
                    routeChecks: [{ groupId: 'g1', groupName: 'qwen3', status: 'ready', httpStatus: 200 }],
                    message: 'Gateway handoff verified and synced.',
                });
            }
            throw new Error(`Unexpected request: ${url}`);
        });

        let statusState = {
            clusters: [{
                clusterId: 'c1',
                clusterName: 'Non-Admin Cluster',
                reconcileStatus: 'admin_handoff_required',
                pendingOperationsCount: 2,
            }],
        };
        let modalState = buildHandoffModalState({
            clusterId: 'c1',
            reconcileStatus: 'admin_handoff_required',
            desiredHash: 'aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff66660000111122223333',
            markerNamespace: 'lens-edit',
            markerName: 'lens-ms-handoff-aaaa1111bbbb2222',
            adminScript: '#!/usr/bin/env bash\nkubectl apply -f -\n',
            pendingOperations: [{ id: 'op-1', action: 'apply', kind: 'HTTPRoute', namespace: 'lens-gw', name: 'qwen3' }],
            mode: 'routes',
        }, { clusters: statusState.clusters });
        let notice = '';
        let loadCalls = 0;

        const beforeBannerHtml = renderToStaticMarkup(createElement(ModelServiceHandoffBanner, {
            clusters: statusState.clusters.filter((cluster) => cluster.reconcileStatus === 'admin_handoff_required'),
        }));
        assert.match(beforeBannerHtml, /Orchestration administrator route sync required/);

        await performHandoffRecheck({
            handoffModal: modalState,
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            setStatus: (updater) => { statusState = typeof updater === 'function' ? updater(statusState) : updater; },
            setNotice: (msg) => { notice = msg; },
            load: async () => { loadCalls += 1; },
        });

        assert.equal(calls.length, 1);
        assert.deepEqual(calls[0].body, {
            clusterId: 'c1',
            expectedHash: 'aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff66660000111122223333',
            markerNamespace: 'lens-edit',
            mode: 'routes',
        });
        assert.equal(modalState.busy, false);
        assert.equal(modalState.error, '');
        assert.equal(modalState.verifyResult.verified, true);
        assert.equal(modalState.handoff.reconcileStatus, 'synced');
        assert.deepEqual(modalState.handoff.pendingOperations, []);
        assert.equal(notice, 'Orchestration administrator route sync verified.');
        assert.equal(loadCalls, 1);
        assert.equal(statusState.clusters[0].reconcileStatus, 'synced');
        assert.equal(statusState.clusters[0].pendingOperationsCount, 0);

        const afterBannerHtml = renderToStaticMarkup(createElement(ModelServiceHandoffBanner, {
            clusters: statusState.clusters.filter((cluster) => cluster.reconcileStatus === 'admin_handoff_required'),
        }));
        assert.equal(afterBannerHtml, '');

        const panelHtml = renderToStaticMarkup(createElement(ModelServiceHandoffPanel, {
            handoff: modalState.handoff,
            verifyResult: modalState.verifyResult,
            clusterName: modalState.clusterName,
            onRecheck: () => {},
            onRefreshScript: () => {},
        }));
        assert.match(panelHtml, />Verified</);
        assert.match(panelHtml, /Gateway handoff verified and synced\./);
        assert.match(panelHtml, /ready \(200\)/);
        assert.match(panelHtml, /0 pending/);
        assert.doesNotMatch(panelHtml, /Orchestration administrator script/);
        assert.doesNotMatch(panelHtml, /Refresh script/);
        assert.doesNotMatch(panelHtml, /<details[^>]*open/);
    });

    test('recheckHandoff refreshes stale handoff script and sends the new expectedHash on subsequent Re-check', async (t) => {
        const oldHash = '1111111111111111222222222222222233333333333333334444444444444444';
        const newHash = '9999999999999999888888888888888877777777777777776666666666666666';
        const calls = [];
        let verifyAttempt = 0;

        t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
            calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
            if (url.endsWith('/gateway/remediation/verify')) {
                verifyAttempt += 1;
                if (verifyAttempt === 1) {
                    return Response.json({
                        clusterId: 'c1',
                        verified: false,
                        reconcileStatus: 'admin_handoff_required',
                        desiredHash: newHash,
                        observedHash: oldHash,
                        markerStatus: 'stale',
                        markerNamespace: 'lens-edit',
                        markerName: 'lens-ms-handoff-9999999999999999',
                        message: '',
                    });
                }
                return Response.json({
                    clusterId: 'c1',
                    verified: true,
                    reconcileStatus: 'synced',
                    desiredHash: newHash,
                    observedHash: newHash,
                    markerStatus: 'complete',
                    markerNamespace: 'lens-edit',
                    markerName: 'lens-ms-handoff-9999999999999999',
                    message: 'Gateway handoff verified and synced.',
                });
            }
            if (url.includes('/gateway/remediation?clusterId=c1&mode=routes')) {
                return Response.json({
                    clusterId: 'c1',
                    accessMode: 'non_admin',
                    reconcileStatus: 'admin_handoff_required',
                    desiredHash: newHash,
                    markerNamespace: 'lens-edit',
                    markerName: 'lens-ms-handoff-9999999999999999',
                    filename: 'lens-model-service-c1-routes.sh',
                    adminScript: '#!/usr/bin/env bash\n# UPDATED ROUTE SCRIPT\nkubectl apply -f updated-routes.yaml\n',
                    pendingOperations: [
                        { id: 'op-new', action: 'apply', kind: 'HTTPRoute', namespace: 'lens-gw', name: 'qwen3-v2', summary: 'Apply updated route' },
                    ],
                    mode: 'routes',
                });
            }
            throw new Error(`Unexpected request: ${url}`);
        });

        let modalState = buildHandoffModalState({
            clusterId: 'c1',
            reconcileStatus: 'admin_handoff_required',
            desiredHash: oldHash,
            markerNamespace: 'lens-edit',
            markerName: 'lens-ms-handoff-1111111111111111',
            adminScript: '#!/usr/bin/env bash\n# OUTDATED SCRIPT\nkubectl apply -f old-routes.yaml\n',
            pendingOperations: [
                { id: 'op-old', action: 'apply', kind: 'HTTPRoute', namespace: 'lens-gw', name: 'qwen3-v1', summary: 'Apply old route' },
            ],
            mode: 'routes',
        }, { clusters: [{ clusterId: 'c1', clusterName: 'Non-Admin Cluster' }] });

        // 1st Re-check: verify returns markerStatus === 'stale' -> auto-fetches fresh script
        await performHandoffRecheck({
            handoffModal: modalState,
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            load: async () => {},
        });

        assert.equal(calls.length, 2);
        assert.equal(calls[0].method, 'POST');
        assert.equal(calls[0].body.expectedHash, oldHash);
        assert.equal(calls[1].method, 'GET');
        assert.match(calls[1].url, /\/gateway\/remediation\?clusterId=c1&mode=routes$/);

        assert.equal(modalState.busy, false);
        assert.equal(modalState.error, '');
        assert.equal(modalState.verifyResult.markerStatus, 'stale');
        assert.equal(modalState.handoff.desiredHash, newHash);
        assert.equal(modalState.handoff.markerName, 'lens-ms-handoff-9999999999999999');
        assert.match(modalState.handoff.adminScript, /UPDATED ROUTE SCRIPT/);

        const staleHtml = renderToStaticMarkup(createElement(ModelServiceHandoffPanel, {
            handoff: modalState.handoff,
            verifyResult: modalState.verifyResult,
            clusterName: modalState.clusterName,
            onRecheck: () => {},
            onRefreshScript: () => {},
        }));
        assert.match(staleHtml, />Stale</);
        assert.match(staleHtml, /belongs to an older route state/);
        assert.match(staleHtml, /UPDATED ROUTE SCRIPT/);
        assert.doesNotMatch(staleHtml, /OUTDATED SCRIPT/);
        assert.match(staleHtml, new RegExp(newHash));
        assert.doesNotMatch(staleHtml, new RegExp(oldHash));
        assert.match(staleHtml, /lens-edit\/lens-ms-handoff-9999999999999999/);
        assert.match(staleHtml, /qwen3-v2/);
        assert.match(staleHtml, /Refresh script<\/button>/);

        // 2nd Re-check: sends the NEW expectedHash and succeeds!
        await performHandoffRecheck({
            handoffModal: modalState,
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            load: async () => {},
        });

        assert.equal(calls.length, 3);
        assert.equal(calls[2].method, 'POST');
        assert.equal(calls[2].body.expectedHash, newHash);
        assert.equal(modalState.verifyResult.verified, true);
        assert.equal(modalState.handoff.reconcileStatus, 'synced');
    });

    test('stale refresh preserves verifyResult on fetch failure and manual Refresh script recovers cleanly', async (t) => {
        const oldHash = 'aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000aaaa0000';
        const freshHash = 'bbbb1111bbbb1111bbbb1111bbbb1111bbbb1111bbbb1111bbbb1111bbbb1111';
        let failGet = true;

        t.mock.method(globalThis, 'fetch', async (url) => {
            if (url.endsWith('/gateway/remediation/verify')) {
                return Response.json({
                    clusterId: 'c1',
                    verified: false,
                    reconcileStatus: 'admin_handoff_required',
                    desiredHash: freshHash,
                    markerStatus: 'missing',
                    message: 'Routes changed concurrently.',
                });
            }
            if (url.includes('/gateway/remediation?clusterId=c1&mode=gateway_repair')) {
                if (failGet) return Response.json({ detail: 'Temporary gateway read timeout' }, { status: 503 });
                return Response.json({
                    clusterId: 'c1',
                    accessMode: 'non_admin',
                    reconcileStatus: 'admin_handoff_required',
                    desiredHash: freshHash,
                    markerNamespace: 'lens-edit',
                    markerName: 'lens-ms-handoff-bbbb1111bbbb1111',
                    adminScript: '#!/usr/bin/env bash\n# RECOVERED REPAIR SCRIPT\n',
                    pendingOperations: [],
                    mode: 'gateway_repair',
                });
            }
            throw new Error(`Unexpected URL: ${url}`);
        });

        let modalState = buildHandoffModalState({
            clusterId: 'c1',
            reconcileStatus: 'admin_handoff_required',
            desiredHash: oldHash,
            markerNamespace: 'lens-edit',
            markerName: 'lens-ms-handoff-aaaa0000aaaa0000',
            adminScript: '#!/usr/bin/env bash\n# INITIAL REPAIR SCRIPT\n',
            mode: 'gateway_repair',
        });

        // desiredHash changed even with markerStatus === 'missing' -> attempts refresh, preserves verifyResult on 503
        await performHandoffRecheck({
            handoffModal: modalState,
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
        });
        assert.equal(modalState.verifyResult.desiredHash, freshHash);
        assert.match(modalState.error, /Temporary gateway read timeout/);

        // Manual Refresh script clears outdated error/verifyResult and updates script + hash
        failGet = false;
        await performHandoffScriptRefresh({
            handoffModal: modalState,
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
        });
        assert.equal(modalState.error, '');
        assert.equal(modalState.verifyResult, null);
        assert.equal(modalState.handoff.desiredHash, freshHash);
        assert.match(modalState.handoff.adminScript, /RECOVERED REPAIR SCRIPT/);
    });

    test('member removal and group mutations open fresh delete handoff script or clear stuck handoff/banner when synced', async (t) => {
        const bindHash = '1010101010101010101010101010101010101010101010101010101010101010';
        const deleteHash = '2020202020202020202020202020202020202020202020202020202020202020';
        let deleteNeedsHandoff = true;

        t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
            if (url.endsWith('/members/m1') && options.method === 'DELETE') {
                if (deleteNeedsHandoff) {
                    return Response.json({
                        deleted: true,
                        memberId: 'm1',
                        reconcileStatus: 'admin_handoff_required',
                        handoff: {
                            clusterId: 'c1',
                            accessMode: 'non_admin',
                            reconcileStatus: 'admin_handoff_required',
                            desiredHash: deleteHash,
                            markerNamespace: 'lens-edit',
                            markerName: 'lens-ms-handoff-2020202020202020',
                            adminScript: '#!/usr/bin/env bash\nkubectl delete httproute qwen3 -n lens-gateway --ignore-not-found=true\n',
                            pendingOperations: [
                                { id: 'del-route', action: 'delete', kind: 'httproute', namespace: 'lens-gateway', name: 'qwen3', summary: 'Delete stale httproute' },
                            ],
                            mode: 'routes',
                        },
                    });
                }
                return new Response(null, { status: 204 });
            }
            if (url.endsWith('/groups/g1/status') && options.method === 'POST') {
                return Response.json({ id: 'g1', clusterId: 'c1', status: 'disabled', reconcileStatus: 'synced' });
            }
            if (url.endsWith('/gateway/remediation/verify') && options.method === 'POST') {
                const body = JSON.parse(options.body);
                assert.equal(body.expectedHash, undefined, 'cluster banner recheck must not fail on stale poll hash');
                return Response.json({
                    clusterId: 'c1',
                    verified: true,
                    reconcileStatus: 'synced',
                    desiredHash: deleteHash,
                    markerStatus: 'complete',
                });
            }
            throw new Error(`Unexpected request: ${url}`);
        });

        const clusters = [
            { clusterId: 'c1', clusterName: 'Cluster One', reconcileStatus: 'admin_handoff_required', pendingOperationsCount: 1, desiredHash: bindHash },
            { clusterId: 'c2', clusterName: 'Cluster Two', reconcileStatus: 'synced', pendingOperationsCount: 0 },
        ];
        let statusState = { clusters };
        // Start with an outdated bind handoff modal open
        let modalState = buildHandoffModalState({
            clusterId: 'c1',
            reconcileStatus: 'admin_handoff_required',
            desiredHash: bindHash,
            markerNamespace: 'lens-edit',
            markerName: 'lens-ms-handoff-1010101010101010',
            adminScript: '#!/usr/bin/env bash\nkubectl apply -f bind-qwen3.yaml\n',
            mode: 'routes',
        }, {
            clusters,
            verifyResult: { verified: false, markerStatus: 'partial', message: 'Earlier bind check failed' },
        });

        // 1. Removing member when earlier script ran -> returns admin_handoff_required with fresh delete script
        const deleteResult = await deleteMember('m1');
        const outcome1 = await applyMutationHandoffResult(deleteResult, {
            clusterId: 'c1',
            clusters: statusState.clusters,
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            setStatus: (updater) => { statusState = typeof updater === 'function' ? updater(statusState) : updater; },
        });
        assert.equal(outcome1, 'admin_handoff_required');
        assert.equal(modalState.handoff.desiredHash, deleteHash);
        assert.equal(modalState.handoff.markerName, 'lens-ms-handoff-2020202020202020');
        assert.equal(modalState.verifyResult, null, 'previous bind verifyResult cleared for new delete script');
        assert.match(modalState.handoff.adminScript, /kubectl delete httproute qwen3/);

        const deleteModalHtml = renderToStaticMarkup(createElement(ModelServiceHandoffPanel, {
            handoff: modalState.handoff,
            verifyResult: modalState.verifyResult,
            clusterName: modalState.clusterName,
            onRecheck: () => {},
        }));
        assert.match(deleteModalHtml, /Delete stale httproute/);
        assert.match(deleteModalHtml, /kubectl delete httproute qwen3/);
        assert.doesNotMatch(deleteModalHtml, /bind-qwen3\.yaml/);

        // 2. Removing member when NO script ever ran (204 / synced) -> closes stale modal for c1 and clears banner immediately
        deleteNeedsHandoff = false;
        const noOpDeleteResult = await deleteMember('m1');
        const outcome2 = await applyMutationHandoffResult(noOpDeleteResult, {
            clusterId: 'c1',
            clusters: statusState.clusters,
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            setStatus: (updater) => { statusState = typeof updater === 'function' ? updater(statusState) : updater; },
        });
        assert.equal(outcome2, 'synced');
        assert.equal(modalState, null);
        assert.equal(statusState.clusters[0].reconcileStatus, 'synced');
        assert.equal(
            renderToStaticMarkup(createElement(ModelServiceHandoffBanner, {
                clusters: statusState.clusters.filter((c) => c.reconcileStatus === 'admin_handoff_required'),
            })),
            '',
        );

        // 3. Toggling group when synced keeps unrelated cluster's modal open while clearing target cluster's banner
        modalState = buildHandoffModalState({ clusterId: 'c2', desiredHash: 'other', adminScript: 'echo c2' }, { clusters });
        statusState = {
            clusters: [{ clusterId: 'c1', clusterName: 'Cluster One', reconcileStatus: 'admin_handoff_required', pendingOperationsCount: 1 }, clusters[1]],
        };
        const toggleRes = await setGroupStatus('g1', 'disabled');
        const outcome3 = await applyMutationHandoffResult(toggleRes, {
            clusterId: 'c1',
            clusters: statusState.clusters,
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            setStatus: (updater) => { statusState = typeof updater === 'function' ? updater(statusState) : updater; },
        });
        assert.equal(outcome3, 'synced');
        assert.equal(modalState.handoff.clusterId, 'c2');
        assert.equal(statusState.clusters[0].reconcileStatus, 'synced');

        // 4. Cluster banner Re-check clears banner and closes any stale c1 modal when verified
        modalState = buildHandoffModalState({ clusterId: 'c1', desiredHash: bindHash, adminScript: 'echo stale' }, { clusters });
        statusState = {
            clusters: [{ clusterId: 'c1', clusterName: 'Cluster One', reconcileStatus: 'admin_handoff_required', pendingOperationsCount: 1, desiredHash: bindHash }],
        };
        let recheckingId = '';
        let bannerNotice = '';
        await performClusterBannerRecheck(statusState.clusters[0], {
            bannerRecheckingId: recheckingId,
            setBannerRecheckingId: (id) => { recheckingId = id; },
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            setStatus: (updater) => { statusState = typeof updater === 'function' ? updater(statusState) : updater; },
            setNotice: (msg) => { bannerNotice = msg; },
            load: async () => {},
        });
        assert.equal(recheckingId, '');
        assert.equal(modalState, null);
        assert.equal(statusState.clusters[0].reconcileStatus, 'synced');
        assert.match(bannerNotice, /verified for Cluster One/);
    });

    test('ModelServiceHandoffPanel renders emerald Verified state plus non-blocking advisory note for advisory routeChecks', () => {
        const handoff = {
            clusterId: 'c1',
            accessMode: 'non_admin',
            reconcileStatus: 'synced',
            desiredHash: 'deadbeef12345678',
            markerNamespace: 'lens-edit',
            markerName: 'lens-ms-handoff-deadbeef12345678',
            adminScript: '#!/usr/bin/env bash\necho ok\n',
            pendingOperations: [],
            mode: 'routes',
        };
        const verifyResult = {
            verified: true,
            markerStatus: 'complete',
            reconcileStatus: 'synced',
            message: 'Gateway handoff verified and synced. External route probe reported advisory backend warnings.',
            routeChecks: [
                {
                    groupId: 'g1',
                    name: 'qwen3-loading',
                    status: 'loading',
                    httpStatus: 503,
                    advisory: true,
                    detail: 'Gateway route matched, but backend model is still loading or starting.',
                },
                {
                    groupId: 'g2',
                    name: 'qwen3-unreachable',
                    status: 'unreachable',
                    advisory: true,
                    detail: 'Gateway URL is unreachable from Lens; handoff verified via cluster completion marker.',
                },
                {
                    groupId: 'g3',
                    name: 'qwen3-degraded',
                    status: 'degraded',
                    httpStatus: 500,
                    advisory: true,
                    detail: 'Gateway route matched, but backend returned a server error.',
                },
            ],
        };

        const html = renderToStaticMarkup(createElement(ModelServiceHandoffPanel, {
            handoff,
            verifyResult,
            clusterName: 'sprocean-min',
            onRecheck: () => {},
        }));

        // Emerald Verified state is preserved (never treated as failed handoff)
        assert.match(html, /border-l-emerald-500/);
        assert.match(html, />Verified</);
        assert.match(html, /Gateway handoff verified and synced\. External route probe reported advisory backend warnings\./);
        assert.doesNotMatch(html, />Stale<\/|>Partial<\/|>Missing marker<\/|>Unconfirmed<\//);

        // Non-blocking advisory note is rendered above routeChecks
        assert.match(html, /Advisory: handoff is verified and synced/);
        assert.ok(html.indexOf('Advisory: handoff is verified and synced') < html.indexOf('Route verification checks'));

        // StatusChips show exact labels, detail titles, and neutral/amber mapping for unreachable/loading
        assert.match(html, /title="Gateway route matched, but backend model is still loading or starting\."[^>]*>loading \(503\)</);
        assert.match(html, /bg-amber-500\/10[^"]*text-amber-700[^>]*title="Gateway route matched, but backend model is still loading or starting\."[^>]*>loading \(503\)</);
        assert.match(html, /bg-slate-200\/70[^"]*text-slate-600[^>]*title="Gateway URL is unreachable from Lens; handoff verified via cluster completion marker\."[^>]*>unreachable</);
        assert.match(html, /title="Gateway route matched, but backend returned a server error\."[^>]*>degraded \(500\)</);

        // Modal subtitle reflects advisory verification vs clean verification
        assert.match(
            handoffModalSubtitle({ handoff, verifyResult }),
            /verified\. Backend route probes reported advisory warnings\./,
        );
        assert.equal(
            handoffModalSubtitle({ handoff, verifyResult: { verified: true, routeChecks: [{ status: 'ready', httpStatus: 200 }] } }),
            'Orchestration administrator route sync verified. Routes are ready.',
        );
    });

    test('performHandoffScriptRefresh marks cluster synced and clears pending banner when pendingOperations is empty', async (t) => {
        t.mock.method(globalThis, 'fetch', async (url) => {
            if (url.includes('/gateway/remediation?clusterId=c1&mode=routes')) {
                return Response.json({
                    clusterId: 'c1',
                    accessMode: 'non_admin',
                    reconcileStatus: 'synced',
                    desiredHash: 'cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333',
                    markerNamespace: 'lens-edit',
                    markerName: 'lens-ms-handoff-cccc3333cccc3333',
                    adminScript: '#!/usr/bin/env bash\n# no-op\n',
                    pendingOperations: [],
                    mode: 'routes',
                });
            }
            throw new Error(`Unexpected URL: ${url}`);
        });

        let statusState = {
            clusters: [
                { clusterId: 'c1', clusterName: 'Cluster One', reconcileStatus: 'admin_handoff_required', pendingOperationsCount: 2 },
                { clusterId: 'c2', clusterName: 'Cluster Two', reconcileStatus: 'admin_handoff_required', pendingOperationsCount: 1 },
            ],
        };
        let modalState = buildHandoffModalState({
            clusterId: 'c1',
            reconcileStatus: 'admin_handoff_required',
            desiredHash: 'aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111',
            pendingOperations: [{ id: 'op-1', action: 'apply', kind: 'HTTPRoute', namespace: 'lens-gw', name: 'qwen3' }],
            mode: 'routes',
        }, {
            clusters: statusState.clusters,
            verifyResult: { verified: false, markerStatus: 'missing' },
        });

        await performHandoffScriptRefresh({
            handoffModal: modalState,
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            setStatus: (updater) => { statusState = typeof updater === 'function' ? updater(statusState) : updater; },
        });

        assert.equal(modalState.handoff.reconcileStatus, 'synced');
        assert.deepEqual(modalState.handoff.pendingOperations, []);
        assert.equal(modalState.verifyResult, null);
        assert.equal(statusState.clusters[0].reconcileStatus, 'synced');
        assert.equal(statusState.clusters[0].pendingOperationsCount, 0);
        assert.equal(statusState.clusters[1].reconcileStatus, 'admin_handoff_required');

        const bannerHtml = renderToStaticMarkup(createElement(ModelServiceHandoffBanner, {
            clusters: statusState.clusters.filter((c) => c.reconcileStatus === 'admin_handoff_required'),
        }));
        assert.doesNotMatch(bannerHtml, /Cluster One/);
        assert.match(bannerHtml, /Cluster Two/);
    });

    test('GatewayConfigModal diffs submit against loadedCluster so prefilled discoveredGateway gatewayPort is preserved on save', async (t) => {
        const statusCluster = {
            clusterId: 'c1',
            clusterName: 'sprocean-min',
            gatewayProvider: 'istio',
            gatewayPort: 32592,
            gatewayPublicUrl: '',
        };
        const loadedCluster = {
            id: 'c1',
            name: 'sprocean-min',
            gatewayProvider: 'istio',
            gatewayPort: null,
            gatewayPublicUrl: null,
            gatewayAuthzHost: null,
        };

        // Prefill preserves discoveredGateway port 32592 even when DB fullCluster.gatewayPort is null
        const resolved = resolveGatewayConfigFields(statusCluster, loadedCluster, ['10.112.228.10']);
        assert.deepEqual(resolved, {
            gatewayProvider: 'istio',
            gatewayPort: '32592',
            gatewayPublicUrl: '',
            gatewayAuthzHost: '10.112.228.10',
        });

        // Diffing against loadedCluster preserves gatewayPort: 32592 (which diffing against statusCluster would drop)
        const patchVsLoaded = buildGatewayConfigPatch(resolved, loadedCluster);
        assert.deepEqual(patchVsLoaded, {
            gatewayPort: 32592,
            gatewayAuthzHost: '10.112.228.10',
        });

        // When DB already stores 32592, unchanged port is omitted from PATCH
        const patchAlreadyPersisted = buildGatewayConfigPatch(resolved, {
            ...loadedCluster,
            gatewayPort: 32592,
            gatewayAuthzHost: '10.112.228.10',
        });
        assert.deepEqual(patchAlreadyPersisted, {});

        // submitGatewayConfig sends PATCH with preserved gatewayPort
        const patchCalls = [];
        t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
            patchCalls.push({ url, method: options.method, body: JSON.parse(options.body) });
            return Response.json({ cluster: { ...loadedCluster, gatewayPort: 32592, gatewayAuthzHost: '10.112.228.10' } });
        });

        await submitGatewayConfig({
            clusterId: 'c1',
            fields: resolved,
            baselineCluster: loadedCluster,
        });
        assert.equal(patchCalls.length, 1);
        assert.equal(patchCalls[0].method, 'PATCH');
        assert.match(patchCalls[0].url, /\/api\/cluster\/clusters\/c1$/);
        assert.deepEqual(patchCalls[0].body, {
            gatewayPort: 32592,
            gatewayAuthzHost: '10.112.228.10',
        });

        // Editing only Gateway host/IP sends both gatewayPublicUrl and preserved gatewayPort
        const patchWithPublicUrl = buildGatewayConfigPatch(
            { ...resolved, gatewayPublicUrl: '10.112.228.229' },
            loadedCluster,
        );
        assert.deepEqual(patchWithPublicUrl, {
            gatewayPort: 32592,
            gatewayPublicUrl: '10.112.228.229',
            gatewayAuthzHost: '10.112.228.10',
        });

        // Clearing Gateway port when DB had null omits gatewayPort; clearing when DB had 32592 sends null
        const clearedWhenNull = buildGatewayConfigPatch(
            { ...resolved, gatewayPort: '', gatewayPublicUrl: '10.112.228.229' },
            loadedCluster,
        );
        assert.deepEqual(clearedWhenNull, {
            gatewayPublicUrl: '10.112.228.229',
            gatewayAuthzHost: '10.112.228.10',
        });
        const clearedWhenPersisted = buildGatewayConfigPatch(
            { ...resolved, gatewayPort: '' },
            { ...loadedCluster, gatewayPort: 32592, gatewayAuthzHost: '10.112.228.10' },
        );
        assert.deepEqual(clearedWhenPersisted, {
            gatewayPort: null,
        });
    });

    test('GatewayConfigModal onSaved immediately reloads both gateway status and members snapshot', async (t) => {
        const fetchedUrls = [];
        t.mock.method(globalThis, 'fetch', async (url) => {
            fetchedUrls.push(String(url));
            if (String(url).includes('/admin/groups')) return Response.json({ items: [{ id: 'g1', name: 'qwen3' }] });
            if (String(url).includes('/admin/members')) return Response.json({ items: [{ id: 'm1', status: 'active', healthJson: { components: { serving: 'ready' } } }] });
            if (String(url).includes('/admin/deployments')) return Response.json({ items: [] });
            if (String(url).includes('/admin/gateway/status')) return Response.json({ clusters: [{ clusterId: 'e66f4591', gatewayPublicUrl: '10.112.228.229', gatewayPort: 32592, gatewayReady: true }] });
            throw new Error(`Unexpected URL: ${url}`);
        });

        let modalCluster = { clusterId: 'e66f4591' };
        let notice = '';
        let loadArg = null;
        let reloadedSnapshot = null;

        await applyGatewayConfigSaved({
            setGatewayConfigCluster: (value) => { modalCluster = value; },
            setNotice: (value) => { notice = value; },
            load: async (arg) => {
                loadArg = arg;
                reloadedSnapshot = await fetchModelServiceAdminSnapshot();
            },
        });

        assert.equal(modalCluster, null);
        assert.equal(notice, 'Gateway configuration saved');
        assert.deepEqual(loadArg, { quiet: true });
        assert.ok(fetchedUrls.some((u) => u.includes('/admin/gateway/status')));
        assert.ok(fetchedUrls.some((u) => u.includes('/admin/members')));
        assert.equal(reloadedSnapshot.status.clusters[0].gatewayPublicUrl, '10.112.228.229');
        assert.equal(reloadedSnapshot.status.clusters[0].gatewayPort, 32592);
        assert.equal(reloadedSnapshot.members[0].healthJson.components.serving, 'ready');
    });

    test('Non-Admin topology renders missing IPP as not installed while Gateway is ready via Prometheus', () => {
        const missingIppCluster = {
            clusterId: 'c-prom',
            clusterName: 'sprocean-min',
            accessMode: 'non_admin',
            canManageGatewayInfra: false,
            canManageGatewayConfig: false,
            canReconcileRoutes: false,
            reconcileStatus: 'synced',
            gatewayState: 'installed',
            gatewayReady: true,
            gatewayRunning: true,
            gatewayProbeSource: 'prometheus',
            ippState: 'missing',
            ippReady: false,
        };

        assert.equal(clusterDot(missingIppCluster), 'active');
        assert.equal(gatewayDetail(missingIppCluster), 'ready (Prometheus)');
        assert.equal(ippDot(missingIppCluster), 'missing');
        assert.equal(ippDetailText(missingIppCluster), 'not installed');

        assert.equal(ippDot({ ...missingIppCluster, ippState: 'unknown', ippReady: null }), 'unknown');
        assert.equal(ippDetailText({ ...missingIppCluster, ippState: 'unknown', ippReady: null }), 'status restricted (non-admin)');
        assert.equal(ippDot({ ...missingIppCluster, ippState: 'installed', ippReady: true }), 'active');
        assert.equal(ippDetailText({ ...missingIppCluster, ippState: 'installed', ippReady: true }), 'ready (Prometheus)');
        assert.equal(ippDot({ ...missingIppCluster, ippState: 'installed', ippReady: false }), 'stopped');
        assert.equal(ippDetailText({ ...missingIppCluster, ippState: 'installed', ippReady: false }), 'installed, stopped');

        const fullStatusDot = {
            ...statusDot,
            installed: 'bg-emerald-400',
            missing: 'bg-amber-400',
        };
        const html = renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: () => true } },
            createElement(DataPlaneTopology, {
                clusters: [missingIppCluster],
                memberGroups: [],
                statusDot: fullStatusDot,
                onComponentAction: () => {},
            }),
        ));

        assert.match(html, /title="Gateway: ready \(Prometheus\) \(active\)"/);
        assert.match(html, /title="IPP: not installed \(not installed\)"/);
        assert.match(html, /bg-emerald-400[^"]*" title="Gateway: ready \(Prometheus\) \(active\)"/);
        assert.match(html, /bg-amber-400[^"]*" title="IPP: not installed \(not installed\)"/);
        assert.doesNotMatch(html, /Orchestration administrator sync required/);
    });

    test('handoff-repair and handoff-ipp preview scripts via non-mutating GET without POST', async (t) => {
        const requests = [];
        t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
            requests.push({ url, method: options.method || 'GET' });
            return Response.json({
                clusterId: 'c-nonadmin',
                reconcileStatus: 'admin_handoff_required',
                desiredHash: 'hash-preview',
                adminScript: '#!/usr/bin/env bash\necho preview\n',
            });
        });

        assert.deepEqual(componentHandoffAction('handoff-routes'), { mode: 'routes', persist: false });
        assert.deepEqual(componentHandoffAction('handoff-repair'), { mode: 'gateway_repair', persist: false });
        assert.deepEqual(componentHandoffAction('handoff-ipp'), { mode: 'ipp_config', persist: false });
        assert.equal(componentHandoffAction('configure'), null);

        const nonAdminCluster = {
            clusterId: 'c-nonadmin',
            clusterName: 'NonAdmin',
            gatewayProvider: 'istio',
            gatewayState: 'installed',
            gatewayRunning: true,
            gatewayReady: true,
            ippState: 'missing',
            ippReady: false,
            accessMode: 'non_admin',
            canManageGatewayInfra: false,
            canManageGatewayConfig: false,
            canReconcileRoutes: false,
        };
        const layout = [{ cluster: nonAdminCluster, rows: [] }];

        const findMenuActions = (node, acc = []) => {
            if (!node || typeof node !== 'object') return acc;
            if (Array.isArray(node)) {
                node.forEach((child) => findMenuActions(child, acc));
                return acc;
            }
            if (typeof node.props?.label === 'string' && typeof node.props?.onClick === 'function') {
                acc.push(node);
            }
            if (node.props?.children) findMenuActions(node.props.children, acc);
            return acc;
        };

        const triggered = [];
        const onComponentAction = async (cluster, _kind, action) => {
            const spec = componentHandoffAction(action);
            assert.ok(spec, `Expected handoff action spec for ${action}`);
            triggered.push(action);
            await fetchClusterHandoff(cluster, spec.mode, { persist: spec.persist });
        };

        const gwMenuTree = TopologyMenuItems({
            menuKey: 'gw:c-nonadmin',
            clusters: [nonAdminCluster],
            layout,
            onComponentAction,
        });
        const gwRepairBtn = findMenuActions(gwMenuTree).find((action) => action.props.label === 'Gateway repair script');
        assert.ok(gwRepairBtn, 'Expected Gateway repair script menu item');
        await gwRepairBtn.props.onClick();

        const ippMenuTree = TopologyMenuItems({
            menuKey: 'ipp:c-nonadmin',
            clusters: [nonAdminCluster],
            layout,
            onComponentAction,
        });
        const ippConfigBtn = findMenuActions(ippMenuTree).find((action) => action.props.label === 'IPP config script');
        assert.ok(ippConfigBtn, 'Expected IPP config script menu item');
        await ippConfigBtn.props.onClick();

        assert.deepEqual(triggered, ['handoff-repair', 'handoff-ipp']);
        assert.equal(requests.length, 2);
        assert.equal(requests[0].method, 'GET');
        assert.match(requests[0].url, /\/api\/v1\/model-service\/admin\/gateway\/remediation\?clusterId=c-nonadmin&mode=gateway_repair$/);
        assert.equal(requests[1].method, 'GET');
        assert.match(requests[1].url, /\/api\/v1\/model-service\/admin\/gateway\/remediation\?clusterId=c-nonadmin&mode=ipp_config$/);
    });

    test('performHandoffRecheck forwards mode, expectedHash, and markerNamespace and preserves unrelated pending routes', async (t) => {
        const requests = [];
        t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
            requests.push({ url, method: options.method || 'GET', rawBody: options.body, body: options.body ? JSON.parse(options.body) : null });
            if (url.endsWith('/gateway/remediation/verify')) {
                const parsed = JSON.parse(options.body);
                return Response.json({
                    clusterId: 'c1',
                    verified: true,
                    reconcileStatus: parsed.mode === 'ipp_config' ? 'admin_handoff_required' : 'synced',
                    markerStatus: 'complete',
                    desiredHash: parsed.expectedHash,
                    markerNamespace: parsed.markerNamespace,
                });
            }
            throw new Error(`Unexpected URL: ${url}`);
        });

        await verifyGatewayRemediation({ clusterId: 'c1', expectedHash: 'h-undef', mode: undefined });
        assert.equal('mode' in requests[0].body, false);

        for (const mode of ['gateway_repair', 'ipp_config']) {
            let statusState = {
                clusters: [{ clusterId: 'c1', clusterName: 'Cluster One', reconcileStatus: 'admin_handoff_required', pendingOperationsCount: 1 }],
            };
            let modalState = buildHandoffModalState({
                clusterId: 'c1',
                reconcileStatus: 'admin_handoff_required',
                desiredHash: `hash-${mode}`,
                markerNamespace: 'lens-edit',
                mode,
            }, { clusters: statusState.clusters });

            await performHandoffRecheck({
                handoffModal: modalState,
                setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
                setStatus: (updater) => { statusState = typeof updater === 'function' ? updater(statusState) : updater; },
                load: async () => {},
            });

            const sent = requests.at(-1).body;
            assert.deepEqual(sent, {
                clusterId: 'c1',
                expectedHash: `hash-${mode}`,
                markerNamespace: 'lens-edit',
                mode,
            });
            assert.equal(modalState.verifyResult.verified, true);
            if (mode === 'ipp_config') {
                assert.equal(statusState.clusters[0].reconcileStatus, 'admin_handoff_required', 'pending routes banner stays intact until routes verify');
            } else {
                assert.equal(statusState.clusters[0].reconcileStatus, 'synced');
            }
        }
    });

    test('performClusterBannerRecheck and clusterHandoffMode forward cluster.handoffMode when present', async (t) => {
        assert.equal(clusterHandoffMode({ handoffMode: 'ipp_config' }), 'ipp_config');
        assert.equal(clusterHandoffMode({ mode: 'gateway_repair' }), 'gateway_repair');
        assert.equal(clusterHandoffMode({}), 'routes');

        const requests = [];
        let verified = false;
        t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
            requests.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
            if (url.endsWith('/gateway/remediation/verify')) {
                return Response.json(verified
                    ? { clusterId: 'c1', verified: true, reconcileStatus: 'synced', markerStatus: 'complete' }
                    : { clusterId: 'c1', verified: false, reconcileStatus: 'admin_handoff_required', markerStatus: 'missing' });
            }
            if (url.includes('/gateway/remediation?clusterId=c1&mode=ipp_config')) {
                return Response.json({
                    clusterId: 'c1',
                    reconcileStatus: 'admin_handoff_required',
                    desiredHash: 'ipp-hash-1',
                    markerNamespace: 'lens-edit',
                    mode: 'ipp_config',
                    adminScript: '#!/usr/bin/env bash\necho ipp\n',
                });
            }
            throw new Error(`Unexpected URL: ${url}`);
        });

        const clusterWithMode = {
            clusterId: 'c1',
            clusterName: 'Cluster One',
            reconcileStatus: 'admin_handoff_required',
            handoffMode: 'ipp_config',
            handoffNamespace: 'lens-edit',
        };
        let modalState = null;
        let statusState = { clusters: [clusterWithMode] };
        let recheckingId = '';

        // 1. Unverified: verify sends handoffMode, then opens modal by fetching mode=ipp_config
        await performClusterBannerRecheck(clusterWithMode, {
            bannerRecheckingId: recheckingId,
            setBannerRecheckingId: (id) => { recheckingId = id; },
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            setStatus: (updater) => { statusState = typeof updater === 'function' ? updater(statusState) : updater; },
            load: async () => {},
        });
        assert.deepEqual(requests[0].body, {
            clusterId: 'c1',
            markerNamespace: 'lens-edit',
            mode: 'ipp_config',
        });
        assert.equal(requests[1].method, 'GET');
        assert.match(requests[1].url, /\/gateway\/remediation\?clusterId=c1&mode=ipp_config$/);
        assert.equal(modalState.handoff.mode, 'ipp_config');

        // 2. Verified: verify sends handoffMode and clears banner + modal
        verified = true;
        await performClusterBannerRecheck(clusterWithMode, {
            bannerRecheckingId: recheckingId,
            setBannerRecheckingId: (id) => { recheckingId = id; },
            setHandoffModal: (updater) => { modalState = typeof updater === 'function' ? updater(modalState) : updater; },
            setStatus: (updater) => { statusState = typeof updater === 'function' ? updater(statusState) : updater; },
            load: async () => {},
        });
        assert.deepEqual(requests[2].body, {
            clusterId: 'c1',
            markerNamespace: 'lens-edit',
            mode: 'ipp_config',
        });
        assert.equal(modalState, null);
        assert.equal(statusState.clusters[0].reconcileStatus, 'synced');
    });

    test('Non-Admin ready qwen3 topology renders green dots, no restricted text, and discovered gatewayNodeAddress:gatewayPort', () => {
        const sproceanCluster = {
            clusterId: 'e66f4591',
            clusterName: 'sprocean-min',
            accessMode: 'non_admin',
            canManageGatewayInfra: false,
            canManageGatewayConfig: false,
            canReconcileRoutes: false,
            reconcileStatus: 'synced',
            gatewayProvider: 'istio',
            gatewayState: 'installed',
            gatewayReady: true,
            gatewayRunning: true,
            gatewayProbeSource: 'http',
            gatewayAddress: null,
            gatewayNodeAddress: '10.239.11.206',
            gatewayPort: 32592,
            ippState: 'installed',
            ippReady: true,
        };
        assert.equal(gatewayAddressLabel(sproceanCluster), '10.239.11.206:32592');
        assert.equal(clusterDot(sproceanCluster), 'active');
        assert.equal(gatewayDetail(sproceanCluster), 'ready (HTTP probe) · 10.239.11.206:32592');
        assert.equal(ippDot(sproceanCluster), 'active');
        assert.equal(ippDetailText(sproceanCluster), 'running');

        const html = renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: () => true } },
            createElement(DataPlaneTopology, {
                clusters: [sproceanCluster],
                memberGroups: [{
                    id: 'msg-0bd1a6d4ded3',
                    name: 'qwen3',
                    status: 'active',
                    clusters: [{
                        clusterId: 'e66f4591',
                        deployments: [{
                            memberId: 'msm-5828c881f2d9',
                            clusterId: 'e66f4591',
                            name: 'optimized-baseline',
                            status: 'active',
                            weight: 1,
                            routable: true,
                            components: {
                                httpRoute: 'ready',
                                ipp: 'ready',
                                serving: 'ready',
                                inferencePool: 'ready',
                                epp: 'ready',
                                modelServer: 'ready',
                            },
                        }],
                    }],
                }],
                statusDot: { ...statusDot, pending: 'bg-amber-400' },
                onComponentAction: () => {},
            }),
        ));

        assert.doesNotMatch(html, /status restricted \(non-admin\)/);
        assert.doesNotMatch(html, /address pending/);
        assert.match(html, /10\.239\.11\.206:32592/);
        assert.match(html, /title="Gateway endpoint: 10\.239\.11\.206:32592"/);
        assert.match(html, /bg-emerald-400[^"]*" title="Gateway: ready \(HTTP probe\) · 10\.239\.11\.206:32592 \(active\)"/);
        assert.match(html, /bg-emerald-400[^"]*" title="IPP: running \(active\)"/);
        assert.match(html, /bg-emerald-400[^"]*" title="HTTPRoute: active"/);
        assert.match(html, /bg-emerald-400[^"]*" title="InferencePool: active"/);
        assert.match(html, /bg-emerald-400[^"]*" title="EPP: active"/);
        assert.match(html, /bg-emerald-400[^"]*" title="model server: active"/);
    });

    test('Non-Admin topology renders httpRoute pending as amber pending, never grey unknown or red unhealthy', () => {
        const pendingCluster = {
            clusterId: 'e66f4591',
            clusterName: 'sprocean-min',
            accessMode: 'non_admin',
            canManageGatewayInfra: false,
            canManageGatewayConfig: false,
            canReconcileRoutes: false,
            reconcileStatus: 'admin_handoff_required',
            gatewayState: 'installed',
            gatewayReady: true,
            gatewayRunning: true,
            ippState: 'installed',
            ippReady: true,
        };
        const html = renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: () => true } },
            createElement(DataPlaneTopology, {
                clusters: [pendingCluster],
                memberGroups: [{
                    id: 'msg-0bd1a6d4ded3',
                    name: 'qwen3',
                    status: 'active',
                    clusters: [{
                        clusterId: 'e66f4591',
                        deployments: [{
                            memberId: 'msm-5828c881f2d9',
                            clusterId: 'e66f4591',
                            name: 'optimized-baseline',
                            status: 'active',
                            weight: 1,
                            routable: true,
                            components: {
                                httpRoute: 'pending',
                                inferencePool: 'ready',
                                epp: 'ready',
                                modelServer: 'ready',
                            },
                        }],
                    }],
                }],
                // Even if caller supplies a grey unknown dot without an explicit pending key,
                // pending must still render amber rather than grey or red.
                statusDot: { ...statusDot, unknown: 'bg-slate-500' },
                onComponentAction: () => {},
            }),
        ));

        assert.match(html, /bg-amber-400[^"]*" title="HTTPRoute: pending \(handoff required\)"/);
        assert.doesNotMatch(html, /bg-slate-500[^"]*" title="HTTPRoute:/);
        assert.doesNotMatch(html, /bg-rose-500[^"]*" title="HTTPRoute:/);
        assert.doesNotMatch(html, /status restricted \(non-admin\)/);
    });

    test('My services ClusterConnectionCard displays discovered baseUrl, address, port, and ready status without address pending', () => {
        const syncedNonAdmin = {
            clusterId: 'e66f4591',
            clusterName: 'sprocean-min',
            provider: 'istio',
            baseUrl: 'http://10.239.11.206:32592/v1',
            address: '10.239.11.206',
            port: 32592,
            ready: true,
            reconcileStatus: 'synced',
            accessMode: 'non_admin',
        };
        const cardHtml = renderToStaticMarkup(createElement(ClusterConnectionCard, { cluster: syncedNonAdmin }));
        assert.match(cardHtml, /http:\/\/10\.239\.11\.206:32592\/v1/);
        assert.match(cardHtml, />Port 32592</);
        assert.match(cardHtml, />10\.239\.11\.206</);
        assert.match(cardHtml, />Ready</);
        assert.doesNotMatch(cardHtml, /address pending|Gateway not ready|Route sync pending/);

        // Fallback when baseUrl is omitted but address/nodeAddress + port are present
        const derived = {
            clusterId: 'e66f4591',
            clusterName: 'sprocean-min',
            provider: 'istio',
            baseUrl: '',
            address: null,
            gatewayNodeAddress: '10.239.11.206',
            port: 32592,
            ready: true,
            reconcileStatus: 'synced',
        };
        assert.equal(connectionEndpointUrl(derived), 'http://10.239.11.206:32592/v1');
        assert.equal(connectionAddressLabel(derived), '10.239.11.206');
        const derivedHtml = renderToStaticMarkup(createElement(ClusterConnectionCard, { cluster: derived }));
        assert.match(derivedHtml, /http:\/\/10\.239\.11\.206:32592\/v1/);
        assert.match(derivedHtml, />10\.239\.11\.206</);
        assert.doesNotMatch(derivedHtml, /address pending/);

        // Host extraction when only baseUrl + port are populated
        assert.equal(connectionAddressLabel({ baseUrl: 'http://10.239.11.206:32592/v1', address: null }), '10.239.11.206');

        // Unready gateway with no endpoint renders Not ready + Gateway not ready
        const unreadyHtml = renderToStaticMarkup(createElement(ClusterConnectionCard, {
            cluster: { clusterId: 'c2', clusterName: 'Down', provider: 'istio', ready: false },
        }));
        assert.match(unreadyHtml, />Not ready</);
        assert.match(unreadyHtml, /Gateway not ready/);
    });

    test('gateway/connection helpers handle IPv6, schemeless host:port, trailing slashes, and reject unsafe URLs', () => {
        // Schemeless host:port and paths are never bracketed as IPv6 and normalize trailing slashes
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: '10.239.11.206:32592' }), '10.239.11.206:32592');
        assert.equal(connectionEndpointUrl({ publicUrl: '10.239.11.206:32592' }), 'http://10.239.11.206:32592/v1');
        assert.equal(connectionEndpointUrl({ publicUrl: 'gw.example.com:8443/v1' }), 'http://gw.example.com:8443/v1');
        assert.equal(connectionEndpointUrl({ publicUrl: 'gw.example.com/v1/' }), 'http://gw.example.com/v1');
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: 'gw.example.com/v1/' }), 'gw.example.com/v1');

        // Bare IPv6 is bracketed cleanly with or without port
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: '2001:db8::1', gatewayPort: 32592 }), '[2001:db8::1]:32592');
        assert.equal(connectionEndpointUrl({ publicUrl: '2001:db8::1', port: 32592 }), 'http://[2001:db8::1]:32592/v1');
        assert.equal(connectionEndpointUrl({ address: '2001:db8::1' }), 'http://[2001:db8::1]/v1');
        assert.equal(gatewayAddressLabel({ gatewayAddress: '2001:db8::1' }), '[2001:db8::1]');
        assert.equal(connectionEndpointUrl({ baseUrl: 'http://[2001:db8::1]:32592/v1' }), 'http://[2001:db8::1]:32592/v1');

        // Bare gatewayPublicUrl + discovered gatewayPort=32592 appends :32592;
        // explicit schemes, paths, explicit ports, and https:// URLs stay authoritative without duplicating :32592
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: '10.112.228.229', gatewayPort: 32592 }), '10.112.228.229:32592');
        assert.equal(connectionEndpointUrl({ gatewayPublicUrl: '10.112.228.229', gatewayPort: 32592 }), 'http://10.112.228.229:32592/v1');
        assert.equal(connectionEndpointUrl({ publicUrl: '10.112.228.229', port: 32592 }), 'http://10.112.228.229:32592/v1');
        assert.equal(connectionAddressLabel({ publicUrl: '10.112.228.229', port: 32592 }), '10.112.228.229');
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: '[2001:db8::1]', gatewayPort: 32592 }), '[2001:db8::1]:32592');
        assert.equal(connectionEndpointUrl({ publicUrl: '[2001:db8::1]', port: 32592 }), 'http://[2001:db8::1]:32592/v1');
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: 'http://10.112.228.229', gatewayPort: 32592 }), 'http://10.112.228.229');
        assert.equal(connectionEndpointUrl({ gatewayPublicUrl: 'http://10.112.228.229', gatewayPort: 32592 }), 'http://10.112.228.229/v1');
        assert.equal(connectionEndpointUrl({ publicUrl: 'http://10.112.228.229', port: 32592 }), 'http://10.112.228.229/v1');
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: '10.112.228.229/v1', gatewayPort: 32592 }), '10.112.228.229/v1');
        assert.equal(connectionEndpointUrl({ publicUrl: '10.112.228.229/v1', port: 32592 }), 'http://10.112.228.229/v1');
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: 'gw.example.com', gatewayPort: 32592 }), 'gw.example.com:32592');
        assert.equal(connectionEndpointUrl({ publicUrl: 'gw.example.com', port: 32592 }), 'http://gw.example.com:32592/v1');
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: '10.112.228.229:8443', gatewayPort: 32592 }), '10.112.228.229:8443');
        assert.equal(connectionEndpointUrl({ publicUrl: '10.112.228.229:8443', port: 32592 }), 'http://10.112.228.229:8443/v1');
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: 'http://10.112.228.229:8443/v1', gatewayPort: 32592 }), 'http://10.112.228.229:8443/v1');
        assert.equal(connectionEndpointUrl({ publicUrl: 'http://10.112.228.229:8443/v1', port: 32592 }), 'http://10.112.228.229:8443/v1');
        assert.equal(gatewayAddressLabel({ gatewayPublicUrl: 'https://gw.example.com', gatewayPort: 32592 }), 'https://gw.example.com');
        assert.equal(connectionEndpointUrl({ publicUrl: 'https://gw.example.com', port: 32592 }), 'https://gw.example.com/v1');

        // Topology and ClusterConnectionCard consistently render bare gatewayPublicUrl + discovered gatewayPort=32592
        const barePublicCluster = {
            clusterId: 'e66f4591',
            clusterName: 'sprocean-min',
            provider: 'istio',
            gatewayProvider: 'istio',
            gatewayState: 'installed',
            gatewayReady: true,
            gatewayRunning: true,
            gatewayProbeSource: 'http',
            gatewayPublicUrl: '10.112.228.229',
            gatewayPort: 32592,
            ippState: 'installed',
            ippReady: true,
        };
        const topologyHtml = renderToStaticMarkup(createElement(
            AuthContext.Provider,
            { value: { can: () => true } },
            createElement(DataPlaneTopology, {
                clusters: [barePublicCluster],
                memberGroups: [],
                statusDot: { ...statusDot, pending: 'bg-amber-400' },
                onComponentAction: () => {},
            }),
        ));
        assert.match(topologyHtml, /title="Gateway endpoint: 10\.112\.228\.229:32592"/);
        const connHtml = renderToStaticMarkup(createElement(ClusterConnectionCard, {
            cluster: {
                clusterId: 'e66f4591',
                clusterName: 'sprocean-min',
                provider: 'istio',
                publicUrl: '10.112.228.229',
                port: 32592,
                ready: true,
            },
        }));
        assert.match(connHtml, /http:\/\/10\.112\.228\.229:32592\/v1/);
        assert.match(connHtml, />Port 32592</);
        assert.match(connHtml, />10\.112\.228\.229</);

        // Reject unsafe schemes, credentials, whitespace, and control characters
        for (const bad of [
            'javascript:alert(1)',
            'javascript:alert(1)//',
            'data:text/html,<script>alert(1)</script>',
            'file:///etc/passwd',
            'http://user:pass@10.239.11.206:32592/v1',
            'http://user@10.239.11.206:32592/v1',
            'user:pass@10.239.11.206',
            '10.239.11.206:32592; id',
            'http://10.239.11.206:32592/v1\nrm -rf /',
        ]) {
            assert.equal(sanitizeHttpUrl(bad), '', `sanitizeHttpUrl rejects ${bad}`);
            assert.equal(connectionEndpointUrl({ baseUrl: bad }), '', `connectionEndpointUrl(baseUrl) rejects ${bad}`);
            assert.equal(connectionEndpointUrl({ publicUrl: bad }), '', `connectionEndpointUrl(publicUrl) rejects ${bad}`);
            assert.equal(connectionEndpointUrl({ address: bad }), '', `connectionEndpointUrl(address) rejects ${bad}`);
            assert.equal(gatewayAddressLabel({ gatewayPublicUrl: bad }), '', `gatewayAddressLabel(publicUrl) rejects ${bad}`);
            assert.equal(gatewayAddressLabel({ gatewayAddress: bad }), '', `gatewayAddressLabel(address) rejects ${bad}`);
            assert.equal(connectionAddressLabel({ address: bad }), '', `connectionAddressLabel(address) rejects ${bad}`);
            assert.equal(connectionAddressLabel({ baseUrl: bad }), '', `connectionAddressLabel(baseUrl) rejects ${bad}`);
        }
    });

    test('curl snippet builder enforces http(s) URLs and POSIX single-quote escaping against shell injection', () => {
        assert.equal(posixSingleQuote("a'b$(id)`whoami`;"), "'a'\\''b$(id)`whoami`;'");

        const crafted = {
            name: `qwen3'$(touch /tmp/pwn)\`id\`;`,
            baseModel: `base'$(id)\nX-Evil: 1`,
        };
        const samples = buildModelCurlSamples({
            baseUrl: 'http://10.239.11.206:32592/v1',
            model: crafted,
            autoInjectsBaseModel: false,
        });
        assert.equal(samples.length, 3);
        for (const sample of samples) {
            assert.match(sample.command, /^curl 'http:\/\/10\.239\.11\.206:32592\/v1\/(?:chat\/completions|embeddings|messages)' /);
            assert.match(sample.command, /-H 'X-Gateway-Base-Model-Name: base'\\''\$\(id\)X-Evil: 1'/);
            // Payload is JSON-serialized and POSIX single-quoted with no unquoted shell expansion
            assert.match(sample.command, /-d '\{"model":"qwen3'\\''\$\(touch \/tmp\/pwn\)`id`;"/);
        }

        // Unsafe URLs are stripped before interpolation
        const unsafeUrlSamples = buildModelCurlSamples({
            baseUrl: 'javascript:alert(1)',
            model: { name: 'qwen3' },
            autoInjectsBaseModel: true,
        });
        assert.doesNotMatch(unsafeUrlSamples[0].command, /javascript:/);
        assert.match(unsafeUrlSamples[0].command, /^curl '\/chat\/completions' /);
    });
});

describe("API keys page", () => {
    function collectDom(root, predicate, out = []) {
        if (!root || root.nodeType !== 1) return out;
        if (predicate(root)) out.push(root);
        for (const child of root.childNodes) collectDom(child, predicate, out);
        return out;
    }

    function getProps(node) {
        const key = Object.keys(node || {}).find((entry) => entry.startsWith('__reactProps$'));
        return key ? node[key] : {};
    }

    async function withApiKeysDom(t, fetchHandler, run) {
        const prevAct = globalThis.IS_REACT_ACT_ENVIRONMENT;
        const prevDoc = globalThis.document;
        const prevWin = globalThis.window;
        const prevHtml = globalThis.HTMLElement;
        const prevSvg = globalThis.SVGElement;
        const prevFetch = globalThis.fetch;

        const makeNode = (tag, doc) => ({
            nodeType: tag === '#text' ? 3 : tag === '#comment' ? 8 : 1,
            nodeName: tag.toUpperCase(),
            tagName: tag.toUpperCase(),
            namespaceURI: 'http://www.w3.org/1999/xhtml',
            ownerDocument: doc,
            parentNode: null,
            childNodes: [],
            attributes: {},
            style: {},
            dataset: {},
            nodeValue: '',
            get textContent() {
                if (this.nodeType === 3) return this.nodeValue || '';
                if (this.nodeType === 8) return '';
                return this.childNodes.map((child) => child.textContent).join('');
            },
            set textContent(value) {
                const text = String(value);
                if (this.nodeType === 3) {
                    this.nodeValue = text;
                    return;
                }
                this.childNodes = text ? [doc.createTextNode(text)] : [];
                for (const child of this.childNodes) child.parentNode = this;
            },
            addEventListener() {},
            removeEventListener() {},
            setAttribute(key, value) { this.attributes[key] = String(value); },
            removeAttribute(key) { delete this.attributes[key]; },
            getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; },
            hasAttribute(key) { return key in this.attributes; },
            appendChild(child) {
                if (child.parentNode) child.parentNode.removeChild(child);
                child.parentNode = this;
                this.childNodes.push(child);
                return child;
            },
            insertBefore(child, reference) {
                if (child.parentNode) child.parentNode.removeChild(child);
                child.parentNode = this;
                const index = reference ? this.childNodes.indexOf(reference) : -1;
                if (index === -1) this.childNodes.push(child);
                else this.childNodes.splice(index, 0, child);
                return child;
            },
            removeChild(child) {
                const index = this.childNodes.indexOf(child);
                if (index !== -1) {
                    this.childNodes.splice(index, 1);
                    child.parentNode = null;
                }
                return child;
            },
            get firstChild() { return this.childNodes[0] || null; },
            get nextSibling() {
                if (!this.parentNode) return null;
                const index = this.parentNode.childNodes.indexOf(this);
                return index >= 0 ? this.parentNode.childNodes[index + 1] || null : null;
            },
            focus() {},
            blur() {},
            remove() { this.parentNode?.removeChild(this); },
        });

        const win = {
            addEventListener() {},
            removeEventListener() {},
            setTimeout,
            clearTimeout,
            setInterval: () => 0,
            clearInterval() {},
            HTMLIFrameElement: function HTMLIFrameElement() {},
            HTMLElement: function HTMLElement() {},
            SVGElement: function SVGElement() {},
            Node: function Node() {},
        };
        const doc = {
            nodeType: 9,
            hidden: true,
            cookie: '',
            defaultView: win,
            documentElement: null,
            body: null,
            activeElement: null,
            createElement(tag) { return makeNode(tag, doc); },
            createElementNS(ns, tag) {
                const node = makeNode(tag, doc);
                node.namespaceURI = ns;
                return node;
            },
            createTextNode(text) {
                const node = makeNode('#text', doc);
                node.nodeValue = String(text);
                return node;
            },
            createComment(text) {
                const node = makeNode('#comment', doc);
                node.nodeValue = String(text);
                return node;
            },
            addEventListener() {},
            removeEventListener() {},
        };
        win.document = doc;
        doc.documentElement = makeNode('html', doc);
        doc.body = makeNode('body', doc);
        doc.documentElement.appendChild(doc.body);

        globalThis.IS_REACT_ACT_ENVIRONMENT = true;
        globalThis.document = doc;
        globalThis.window = win;
        globalThis.HTMLElement = win.HTMLElement;
        globalThis.SVGElement = win.SVGElement;
        globalThis.fetch = fetchHandler;

        const container = doc.createElement('div');
        doc.body.appendChild(container);
        const root = createRoot(container);
        const flush = async () => {
            await act(async () => {
                await Promise.resolve();
                await Promise.resolve();
            });
        };

        try {
            await run({ root, doc, flush });
        } finally {
            await act(async () => {
                root.unmount();
            });
            globalThis.IS_REACT_ACT_ENVIRONMENT = prevAct;
            globalThis.document = prevDoc;
            globalThis.window = prevWin;
            globalThis.HTMLElement = prevHtml;
            globalThis.SVGElement = prevSvg;
            globalThis.fetch = prevFetch;
        }
    }

    test('initialAction="create" opens the Create API key modal once and stays closed after dismissing', async (t) => {
        const fetchedUrls = [];
        await withApiKeysDom(
            t,
            async (url) => {
                fetchedUrls.push(url);
                return Response.json({ items: [] });
            },
            async ({ root, doc, flush }) => {
                await act(async () => {
                    root.render(createElement(
                        AuthContext.Provider,
                        { value: { principal: { userId: 'u-1' }, can: () => true } },
                        createElement(ApiKeysPage, { initialAction: 'create' }),
                    ));
                });
                await flush();

                assert.ok(fetchedUrls.includes('/api/v1/model-service/tokens'));
                assert.match(doc.body.textContent, /Create API key/);

                const cancelButton = collectDom(doc.body, (el) => el.tagName === 'BUTTON' && el.textContent.trim() === 'Cancel')[0];
                assert.ok(cancelButton);
                await act(async () => {
                    getProps(cancelButton).onClick?.();
                });
                await flush();
                assert.doesNotMatch(doc.body.textContent, /Create API key/);

                // Re-rendering with initialAction still "create" must not re-open the modal
                await act(async () => {
                    root.render(createElement(
                        AuthContext.Provider,
                        { value: { principal: { userId: 'u-1' }, can: () => true } },
                        createElement(ApiKeysPage, { initialAction: 'create' }),
                    ));
                });
                await flush();
                assert.doesNotMatch(doc.body.textContent, /Create API key/);
            },
        );
    });

    test('auth-disabled mode skips token fetch, suppresses auto-open modal, and shows notice', async (t) => {
        const fetchedUrls = [];
        await withApiKeysDom(
            t,
            async (url) => {
                fetchedUrls.push(url);
                return Response.json({ items: [] });
            },
            async ({ root, doc, flush }) => {
                await act(async () => {
                    root.render(createElement(
                        AuthContext.Provider,
                        { value: { principal: { userId: 'system' }, can: () => true } },
                        createElement(ApiKeysPage, { initialAction: 'create' }),
                    ));
                });
                await flush();
                assert.equal(fetchedUrls.length, 0);
                assert.doesNotMatch(doc.body.textContent, /Create API key/);
                assert.match(doc.body.textContent, /PRISM_AUTH_MODE=local/);
            },
        );
    });
});
