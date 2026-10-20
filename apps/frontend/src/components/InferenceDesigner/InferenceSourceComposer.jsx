import yaml from 'js-yaml';
import { useEffect, useRef, useState } from 'react';
import { Select, LoadingState } from '../../shared/ui/index';
import { MultiSelectDropdown } from '../../shared/ui/MultiSelectDropdown';
import { previewSourceComponent } from './sourceCompositionBackend';
import { applyEngineCapabilities, applyRouterCapabilities, capabilityPolicyDefaults } from '../../features/inference-designer/capabilityRendering';
import { nodeSourceFields, nodeRouterFields } from '../../features/inference-designer/nodeParameters';
import { sourceParameterEdit } from '../../features/inference-designer/sourceParameters';
import { routerParameterDocument } from '../../features/inference-designer/routerParameters';
import { InferenceParameterField } from './InferenceParameterField';
import { InferenceParameterInput } from './InferenceParameterInput';
import { InferenceRouterParameters } from './InferenceRouterParameters';

/** Edit only the selected node's source-owned fields, without a source browser. */
export function InferenceSourceComposer({ draft, change, catalog, assetPaths = [], entity, loading, error: catalogError }) {
    const [previews, setPreviews] = useState([]), [busy, setBusy] = useState(false), [error, setError] = useState('');
    const latest = useRef({ draft, change, catalog });
    useEffect(() => { latest.current = { draft, change, catalog }; });
    const paths = JSON.stringify(assetPaths.filter(path => catalog?.assets.some(asset => asset.path === path && asset.kind !== 'values')));
    const overrides = JSON.stringify(draft.sourcePlan?.capabilityOverrides);
    const variables = JSON.stringify(draft.sourcePlan?.variables || draft.sourceVariables);
    const choices = (draft.sourcePlan?.choices || []).filter(choice => choice.guide === entity?.guide || entity?.guide?.startsWith(`${choice.guide}/`));
    const sourceGuide = catalog?.guides.find(guide => guide.id === entity?.guide);
    const selectors = Object.entries(sourceGuide?.variables || {}).filter(([key, spec]) => spec?.values && !choices.some(choice => choice.key === key) && !['ACCELERATOR_TYPE', 'MONITORING', 'MODEL', 'MODEL_NAME', 'NAMESPACE'].includes(key));
    const missing = choices.some(choice => !choice.value?.length);
    useEffect(() => {
        let active = true;
        const { draft, catalog } = latest.current;
        setPreviews([]); setError('');
        if (!catalog || missing) { setBusy(false); return; }
        setBusy(true);
        Promise.all(JSON.parse(paths).map(async path => {
            const unit = draft.sourceUnits.find(item => item.path === path);
            const asset = catalog.assets.find(item => item.path === path);
            const preview = await previewSourceComponent(draft.clusterId, catalog.commit, { ...unit, namespace: draft.sourceNamespace, runtimeImage: draft.image, variables: draft.sourcePlan?.variables?.[unit?.guide || asset.guide] || {} });
            return { path, ...preview, documents: applyEngineCapabilities(preview.documents, draft.sourcePlan?.capabilityOverrides) };
        })).then(value => { if (active) { setPreviews(value); const current = latest.current.draft; const charts = value.filter(item => item.chartVersion); if (charts.some(item => current.sourceUnits.find(unit => unit.path === item.path)?.chartVersion !== item.chartVersion || current.sourceUnits.find(unit => unit.path === item.path)?.chartCommit !== item.chartCommit)) latest.current.change({ sourceUnits: current.sourceUnits.map(unit => { const chart = charts.find(item => item.path === unit.path); return chart ? { ...unit, chartVersion: chart.chartVersion, chartCommit: chart.chartCommit, values: chart.values } : unit; }) }); } }).catch(reason => { if (active) setError(reason.message); }).finally(() => { if (active) setBusy(false); });
        return () => { active = false; };
    }, [paths, variables, overrides, missing, entity?.id, draft.clusterId, draft.sourceNamespace, draft.image, catalog?.commit]);
    const updateVariable = (choice, value) => change({ sourceVariables: { ...draft.sourceVariables, [choice.guide]: { ...draft.sourceVariables?.[choice.guide], [choice.key]: value } } });
    const routerLayers = [];
    try {
        for (const layer of draft.sourceRouter.values) {
            if (!assetPaths.includes(layer.path)) continue;
            const asset = catalog?.assets.find(item => item.path === layer.path);
            if (!asset) continue;
            const content = layer.content ?? applyRouterCapabilities(asset.content, draft.sourcePlan?.capabilityOverrides, capabilityPolicyDefaults(catalog));
            if (nodeRouterFields(routerParameterDocument(content), entity).length) routerLayers.push({ ...layer, content });
        }
    } catch (reason) { if (!error) return <p role="alert">{reason.message}</p>; }
    return <section className="space-y-4" aria-label="Node parameters">
        {entity?.id === 'transfer' && <p className="text-sm text-theme-muted">Configure both Prefill and Decode here. The serving-component tier shows each runtime role; the Kubernetes tier shows where those workloads run.</p>}
        {choices.map(choice => <label className="block text-sm" key={choice.key}>{choice.label}{choice.multiple ? <MultiSelectDropdown label={choice.label} options={choice.options.map(option => option.value)} selected={new Set(choice.value || [])} formatLabel={id => choice.options.find(option => option.value === id)?.label || id} inlineMenu onChange={id => change({ guideChoices: { ...draft.guideChoices, [choice.guide]: !id ? [] : choice.value.includes(id) ? choice.value.filter(value => value !== id) : [...choice.value, id] } })} /> : <Select aria-label={choice.label} value={choice.value} onChange={event => updateVariable(choice, event.target.value)}><option value="">Select {choice.label.toLowerCase()}</option>{choice.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</Select>}</label>)}
        {selectors.map(([key, spec]) => <label className="block text-sm" key={key}>{key.replace(/_/g, ' ')}<Select aria-label={key.replace(/_/g, ' ')} value={draft.sourcePlan?.variables?.[entity.guide]?.[key] ?? draft.sourceVariables?.[entity.guide]?.[key] ?? spec.default ?? ''} onChange={event => updateVariable({ guide: entity.guide, key }, event.target.value)}>{spec.values.map(value => <option key={value} value={value}>{value}</option>)}</Select></label>)}
        {['prefix', 'chunked'].includes(entity?.id) && <label className="block text-sm">{entity.label}<Select aria-label={entity.label} value={draft.capabilityOverrides?.[entity.id] === undefined ? '' : String(draft.capabilityOverrides[entity.id])} onChange={event => { const overrides = { ...draft.capabilityOverrides }; if (event.target.value === '') delete overrides[entity.id]; else overrides[entity.id] = event.target.value === 'true'; change({ capabilityOverrides: overrides }); }}><option value="">Source default</option><option value="true">Enabled</option><option value="false">Disabled</option></Select></label>}
        {(busy || loading) && <LoadingState size="sm" label="Loading node parameters" />}
        {(error || catalogError) && <p role="alert">{error || catalogError}</p>}
        {missing && <p className="text-sm text-theme-muted">Select the required options above to load this optimization's parameters.</p>}
        {routerLayers.map(layer => <InferenceRouterParameters key={layer.path} entity={entity} embedded draft={{ routerValues: layer.content }} change={patch => change({ sourceRouter: { ...draft.sourceRouter, values: draft.sourceRouter.values.map(item => item.path === layer.path ? { ...item, content: patch.routerValues } : item) } })} onError={setError} />)}
        {previews.map(preview => <div key={preview.path}>{nodeSourceFields(preview.documents, entity).filter(field => !field.readOnly).map(field => <InferenceParameterField key={field.key} field={field}><InferenceParameterInput key={JSON.stringify([field.key, field.value])} leaf={field} onCommit={raw => {
            try {
                const edit = sourceParameterEdit(field, field.encode ? field.encode(raw) : raw);
                const documents = structuredClone(preview.documents); let owner = documents;
                for (const key of edit.path.slice(0, -1)) owner = owner[key];
                owner[edit.path.at(-1)] = edit.value;
                const current = latest.current.draft;
                const unit = current.sourceUnits.find(item => item.path === preview.path);
                latest.current.change({ sourceUnits: current.sourceUnits.map(item => item.path === preview.path ? { ...unit, chartVersion: preview.chartVersion ?? unit.chartVersion, chartCommit: preview.chartCommit ?? unit.chartCommit, replacementContent: documents.map(doc => yaml.dump(doc, { noRefs: true, lineWidth: -1 })).join('---\n'), edits: [] } : item) });
                setPreviews(current => current.map(item => item.path === preview.path ? { ...preview, documents } : item)); setError('');
            } catch (reason) { setError(reason.message); }
        }} /></InferenceParameterField>)}</div>)}
        {!busy && !loading && !missing && !error && !catalogError && !routerLayers.length && !previews.some(preview => nodeSourceFields(preview.documents, entity).some(field => !field.readOnly)) && !['prefix', 'chunked'].includes(entity?.id) && <p className="text-sm text-theme-muted">This optimization inherits its defaults; the selected source declares no editable node parameters. Full deployment editing is available in Deployment settings.</p>}
    </section>;
}
