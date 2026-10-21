import test from 'node:test';
import assert from 'node:assert/strict';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import Tree from './OptimizationComparisonTree.jsx';
import {toggleOptimizationSelection} from '../../model/experimentDesign.js';
test('combination selection is independent and enforces the same-pod dependency',()=>{
 assert.deepEqual(toggleOptimizationSelection(['full'],'router-neutral'),['full','router-neutral']);
 assert.deepEqual(toggleOptimizationSelection([],'kubernetes-service'),['kubernetes-service','full']);
 assert.deepEqual(toggleOptimizationSelection(['kubernetes-service','full'],'full'),[]);
});
test('scenario tree exposes selectable branches without checkbox lists',()=>{
 const html=renderToStaticMarkup(createElement(Tree,{guide:'optimized-baseline',selected:['full'],onChange(){}}));
 assert.match(html,/Compare serving scenarios with the same workload/);
 assert.doesNotMatch(html,/Add comparison scenario/);
 assert.match(html,/Full Guide/);
 assert.match(html,/aria-label="Serving scenario comparison tree"/);
 assert.match(html,/aria-pressed="true"/);
 assert.doesNotMatch(html,/type="checkbox"/);

});
test('only unselected scenario surfaces and hubs receive the light-theme override',()=>{
 const render=selected=>renderToStaticMarkup(createElement(Tree,{guide:'optimized-baseline',selected,onChange(){}}));
 const unselected=render([]);
 const selected=render(['full']);
 const surfaceCount=html=>(html.match(/class="scenario-node-surface"/g)||[]).length;
 assert.match(selected,/class="optimization-comparison-tree /);
 assert.equal(surfaceCount(unselected),surfaceCount(selected)+1);
 assert.match(selected,/<circle class="scenario-node-surface" cx="80"/);
 assert.match(selected,/<circle class="scenario-node-surface" cx="225"/);
 assert.match(selected,/fill-opacity="0.09"/);
});
