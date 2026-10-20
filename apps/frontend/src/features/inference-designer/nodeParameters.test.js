import test from 'node:test';
import assert from 'node:assert/strict';
import yaml from 'js-yaml';
import { nodeSourceFields, nodeRouterFields } from './nodeParameters.js';
import { routerParameterDocument, changeRouterParameter } from './routerParameters.js';
import { deploymentGuideEntries } from './guideAdapters.js';

test('node engine fields preserve flag syntax and omit model and unrelated controls', () => {
    const docs = [{kind:'Deployment', spec:{template:{spec:{containers:[{name:'engine',args:['--model=org/model','--tensor-parallel-size=2','--max-num-seqs','64','--speculative-config={"num_speculative_tokens":3}','--enable-prefix-caching'],env:[{name:'HF_TOKEN',value:'private'}]}]}}}}];
    const fields = nodeSourceFields(docs, {id:'parallel',kind:'capability'});
    assert.deepEqual(fields.map(f=>f.sourceKey), ['--tensor-parallel-size','--max-num-seqs']);
    assert.equal(fields[0].value,'2');
    assert.equal(fields[0].encode('4'),'--tensor-parallel-size=4');
    assert.equal(fields[1].encode('128'),'128');
    assert.equal(fields[1].path.at(-1),3);
    assert.equal(nodeSourceFields(docs,{id:'prefix',kind:'capability'})[0].readOnly,true);
});
test('worker fields respect the source role label', () => {
    const docs = ['prefill','decode'].map(role=>({kind:'Deployment',metadata:{labels:{'llm-d.ai/role':role}},spec:{replicas:role==='prefill'?2:4}}));
    assert.deepEqual(nodeSourceFields(docs,{id:'prefill-worker',kind:'module'}).map(f=>f.value),[2]);
});
test('router scope follows plugin ownership rather than the source filename', () => {
    const content=yaml.dump({router:{epp:{pluginsCustomConfig:{'predicted-latency.yaml':yaml.dump({plugins:[{type:'prefix-cache-scorer',parameters:{cacheWeight:2,maxTTFTPenaltyMs:100}},{type:'predicted-latency-scorer',parameters:{ttftWeight:1,tpotWeight:2}}]})}}}});
    const fields=nodeRouterFields(routerParameterDocument(content),{id:'latency',kind:'capability'});
    assert.deepEqual(fields.map(f=>f.path.at(-1)),['maxTTFTPenaltyMs','ttftWeight','tpotWeight']);
    const changed=routerParameterDocument(changeRouterParameter(content,fields[1],'3'));
    assert.equal(changed.leaves.find(f=>f.path.at(-1)==='cacheWeight').value,2);
    assert.equal(changed.leaves.find(f=>f.path.at(-1)==='ttftWeight').value,3);
});
test('Guide picker excludes categories and aliases while preserving executable recipe IDs', () => {
    const recipe=id=>({id,title:'Source title',disposition:{kind:'recipe'},steps:{deploy:{default:[]}}});
    const catalog={guides:[recipe('optimized-baseline'),{...recipe('category'),disposition:{kind:'category'}},{...recipe('alias'),disposition:{kind:'alias'}},{id:'runtime-inventory',disposition:{kind:'recipe'}}],assets:[{guide:'runtime-inventory',kind:'chart'}]};
    assert.deepEqual(deploymentGuideEntries(catalog).map(g=>g.id),['optimized-baseline','runtime-inventory']);
});


test('P/D controls include both role replicas and connector tuning without unrelated flags', () => {
    const docs = ['prefill', 'decode'].map(role => ({ kind: 'Deployment', metadata: { labels: { 'llm-d.ai/role': role } }, spec: { replicas: 2, template: { spec: { containers: [{ name: 'engine', args: ['--tensor-parallel-size=4', '--kv-transfer-config={"kv_connector":"NixlConnector"}', '--max-model-len=8192'], env: [{ name: 'UCX_TLS', value: 'tcp' }, { name: 'UNRELATED_SETTING', value: 'hidden' }] }] } } } }));
    const fields = nodeSourceFields(docs, { id: 'transfer', kind: 'capability' });
    assert.deepEqual(fields.filter(field => field.sourceKey.endsWith('.replicas')).map(field => field.title), ['Prefill Replicas', 'Decode Replicas']);
    assert.equal(fields.filter(field => field.sourceKey === '--kv-transfer-config').length, 2);
    assert.deepEqual(fields.filter(field => field.sourceKey === '--tensor-parallel-size').map(field => field.title), ['Prefill TP', 'Decode TP']);
    assert.ok(!fields.some(field => /max-model|UNRELATED/.test(field.sourceKey)));
    const routing = yaml.dump({router:{epp:{replicas:3,pluginsCustomConfig:{'plugins.yaml':yaml.dump({plugins:[{type:'token-load-scorer',parameters:{loadWeight:4}},{type:'prefix-cache-affinity-filter',parameters:{affinityThreshold:0.5}}]})}}}});
    assert.deepEqual(nodeRouterFields(routerParameterDocument(routing), { id: 'affinity', kind: 'capability' }).map(field => field.path.at(-1)), ['affinityThreshold']);
});


test('service nodes expose endpoint fields without EPP plugin tuning', () => {
    const document = routerParameterDocument(yaml.dump({ router: { extraServicePorts: [{ name: 'http', port: 80 }], epp: { replicas: 2, pluginsCustomConfig: { 'plugins.yaml': yaml.dump({ plugins: [{ type: 'token-load-scorer', parameters: { loadWeight: 3 } }] }) } } } }));
    assert.deepEqual(nodeRouterFields(document, { id: 'service', kind: 'module' }).map(field => field.path.at(-1)), ['port']);
    assert.deepEqual(nodeRouterFields(document, { id: 'load-routing', kind: 'capability' }).map(field => field.path.at(-1)), ['loadWeight']);
});
