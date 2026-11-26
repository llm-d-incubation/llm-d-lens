import test from 'node:test';
import assert from 'node:assert/strict';
import { Children, createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ResourceBasisControl from './ResourceBasisControl.jsx';
import Tree from './OptimizationComparisonTree.jsx';
import { TopologyPreview } from './ConfigurationPreview.jsx';
import { ConfigurationValidationContext } from './configurationFieldState.js';
import { expandConfigurationTopologies, analyzeConfigurationSweep, topologySweepFromConfiguration } from '../../model/configuration.js';
import { validateConfigurationInputs } from '../../model/configurationValidation.js';

test('generation skips resource confirmation when currently available cards are sufficient', () => {
 const html=renderToStaticMarkup(createElement(ResourceBasisControl,{value:'available',hardware:{availableGpuCount:2,gpuCount:8},onGenerate(){}}));
 assert.match(html,/Generate YAML/);
 assert.equal((html.match(/<button/g)||[]).length,1);
 assert.doesNotMatch(html,/aria-haspopup="dialog"/);
 assert.doesNotMatch(html,/aria-label="YAML resource budget"/);
});
test('generation requires confirmation when current availability is insufficient', () => {
 const html=renderToStaticMarkup(createElement(ResourceBasisControl,{hardware:{availableGpuCount:2,gpuCount:8},requiredCards:5,onGenerate(){}}));
 assert.match(html,/aria-haspopup="dialog"/);
 assert.doesNotMatch(html,/Replica \/ TP resource budget/);
});
test('multiple branches remain active simultaneously and use a single selection entry',()=>{
 const html=renderToStaticMarkup(createElement(Tree,{guide:'optimized-baseline',selected:['full','affinity-only','load-only'],onChange(){}}));
 assert.equal((html.match(/type="checkbox"/g)||[]).length,0);
 assert.equal((html.match(/aria-pressed="true"/g)||[]).length,3);
 assert.doesNotMatch(html,/Add comparison scenario/);
 assert.match(html,/3 scenarios/);
 assert.match(html,/same workload/);
});

test('configuration card comparison summary contains only text, without diagrams or editing controls',async()=>{
 const {default:Summary}=await import('./OptimizationSelectionSummary.jsx');
 const html=renderToStaticMarkup(createElement(Summary,{guide:'optimized-baseline',selected:['full','load-only']}));
 assert.match(html,/2 comparison targets/);assert.match(html,/Full Guide/);assert.match(html,/Load-aware/);
 assert.doesNotMatch(html,/<svg|<button|<input/);
});


test('generated YAML is editable and forwards the full text without interpreting markup', async () => {
 const { YamlPreview } = await import('./ConfigurationPreview.jsx');
 let edited;
 const content = 'kind: Deployment\nmetadata:\n  name: sample\n  annotations:\n    note: "<script>alert(1)</script>"';
 const tree = YamlPreview({ content, onChange(value) { edited = value; } });
 const html = renderToStaticMarkup(tree);
 assert.match(html, /<textarea[^>]*aria-label="Editable deployment YAML"/);
 assert.doesNotMatch(html, /<script>|readonly/);
 const editor = tree.props.children.find(child => child?.props?.['aria-label'] === 'Editable deployment YAML');
 editor.props.onChange({ target: { value: content + '\n' } });
 assert.equal(edited, content + '\n');
});


function renderTopologies(props, context = { errors: [], submitted: false }) {
 let tree;
 function Capture() { tree = TopologyPreview(props); return tree; }
 const html = renderToStaticMarkup(createElement(ConfigurationValidationContext.Provider, { value: context }, createElement(Capture)));
 const find = (label, node = tree) => node?.props?.['aria-label'] === label || (node?.props?.children === label && typeof node.props.onClick === 'function') ? node : Children.toArray([node?.props?.children, node?.props?.actions]).map(child => find(label, child)).find(Boolean);
 return { html, find };
}

test('PD range editing filters the complete batch while rendering only one preview page', () => {
 let sweep = { ...topologySweepFromConfiguration(), prefillReplicaVariants: '1-4', replicaVariants: '1-8', prefillTensorParallelVariants: '1,2', tensorParallelVariants: '1,2' };
 const render = () => renderTopologies({ sweep, isPd: true, analysis: analyzeConfigurationSweep(sweep), onChange: next => { sweep = next; } });
 let view = render();
 assert.match(view.html, /128/);
 assert.match(view.html, /Showing 1–10 of 128 configurations/);
 assert.equal((view.html.match(/<tbody>([\s\S]*?)<\/tbody>/)[1].match(/<tr/g) || []).length, 10);
 assert.doesNotMatch(view.html, /Duplicate|Compare a preset|Add configuration/);
 view.find('Maximum accelerators').props.onChange({ target: { value: '8' } });
 view = render();
 assert.match(view.html, /Showing 1–10 of 50 configurations/);
 assert.equal(expandConfigurationTopologies({ sweep }).length, 50);
 view.find('P/D replica relationship').props.onChange({ target: { value: 'equal' } });
 assert.ok(expandConfigurationTopologies({ sweep }).every(row => row.prefillReplicaCount === row.replicaCount && row.gpuCount <= 8));
 render().find('Clear filters').props.onClick();
 assert.equal(expandConfigurationTopologies({ sweep }).length, 128);
});

test('single and multiple PD configurations use the same range fields', () => {
 for (const maxConfigurations of [1, 256]) {
  const sweep = topologySweepFromConfiguration({ prefillReplicaCount: 2, replicaCount: 2 });
  const view = renderTopologies({ sweep, isPd: true, maxConfigurations, perSlice: true, onChange() {} });
  for (const label of ['Prefill Replicas values', 'Prefill TP values', 'Decode Replicas values', 'Decode TP values']) assert.ok(view.find(label));
  assert.match(view.html, /Showing 1–1 of 1 configurations/);
  assert.match(view.html, /Accelerators \/ Slice/);
 }
});

test('invalid ranges and empty filtered results remain editable and identify the source controls', () => {
 const sweep = { ...topologySweepFromConfiguration(), prefillReplicaVariants: '1-' };
 const errors = validateConfigurationInputs({ guide: 'pd-disaggregation', sweep });
 const view = renderTopologies({ sweep, isPd: true, onChange() {} }, { errors, submitted: true });
 assert.match(view.html, /data-configuration-field="sweep.prefillReplicaVariants"/);
 assert.match(view.html, /aria-invalid="true"[^>]*aria-describedby=/);
 assert.match(view.html, /No matching configurations/);
 assert.equal(view.find('Prefill Replicas values').props.value, '1-');
 const empty = renderTopologies({ sweep: { ...topologySweepFromConfiguration(), fixedAccelerators: '1' }, isPd: true, onChange() {} });
 assert.match(empty.html, /No configurations match these filters/);
 assert.equal(empty.find('Export CSV').props.disabled, true);
});


test('CSV export includes the whole filtered batch beyond the visible preview page', async (t) => {
 const sweep = { ...topologySweepFromConfiguration(), prefillReplicaVariants: '1-4', replicaVariants: '1-8', prefillTensorParallelVariants: '1,2', tensorParallelVariants: '1,2', maxAccelerators: '8' };
 const view = renderTopologies({ sweep, isPd: true, onChange() {} });
 let exported;
 const anchor = { click() {}, remove() {} };
 const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'document');
 Object.defineProperty(globalThis, 'document', { configurable: true, value: { createElement: () => anchor, body: { appendChild() {} } } });
 t.after(() => { if (documentDescriptor) Object.defineProperty(globalThis, 'document', documentDescriptor); else delete globalThis.document; });
 t.mock.method(URL, 'createObjectURL', blob => { exported = blob; return 'blob:test'; });
 t.mock.method(URL, 'revokeObjectURL', () => {});
 t.mock.method(globalThis, 'setTimeout', callback => { callback(); return 0; });
 view.find('Export CSV').props.onClick();
 const lines = (await exported.text()).trim().split('\r\n');
 assert.equal(lines.length, 51);
 assert.equal(anchor.download, 'evaluation-configurations.csv');
 assert.ok(lines.slice(1).every(line => Number(line.split(',').at(-1)) <= 8));
});
