// Execute actual template functions with a small DOM; no browser or service startup.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
process.env.TZ = 'Asia/Shanghai';
const root = path.resolve(__dirname, '..');
const admin = fs.readFileSync(path.join(root, 'templates/dashboard.html'), 'utf8');
const portal = fs.readFileSync(path.join(root, 'templates/portal.html'), 'utf8');

// 老板日报：未知值不能渲染成零，负值保持，复制文本使用服务端原文。
const briefNodes = Object.fromEntries(['bsProgress','bsRegionOperations','bsFocusStores','bsBriefText','dailyComparisons'].map(id=>[id,{innerHTML:'',value:''}]));
const briefCtx = vm.createContext({document:{getElementById:id=>briefNodes[id]},esc:value=>String(value).replaceAll('<','&lt;')});
for (const name of ['dailyNumber','dailyMoney','briefChange','renderBusinessBrief','renderDailyComparisons']) vm.runInContext(sourceFunction(admin,name),briefCtx);
briefCtx.renderBusinessBrief({target_progress:{time_pct:50,gap:20000,remaining_days:15,needed_daily:1333.33},completion_rate:30,operating_ratios:{payment_pct:null,coin_out_ratio:0},region_completion:[{region:'深圳',actual:10000,target:30000,pct:33.33,payment_pct:null,payment_missing:['<新店>']}],focus_stores:[{venue:'<新店>',monthly_income:8134.2,daily_income:null,completion:null}],brief_text:'原始日报\n数据待核对'});
assert.equal(briefNodes.bsBriefText.value,'原始日报\n数据待核对');
assert.ok(briefNodes.bsFocusStores.innerHTML.includes('待补齐可比数据'));
assert.ok(briefNodes.bsFocusStores.innerHTML.includes('&lt;新店>'));
assert.ok(briefNodes.bsRegionOperations.innerHTML.includes('查看缺少货款记录的 1 家门店'));
assert.ok(briefNodes.bsProgress.innerHTML.includes('0.00∶1'));
briefCtx.renderDailyComparisons({previous_week:{date:'2026-09-08',income:30198.56,change:654.04,change_pct:2.17,complete:false}});
assert.ok(briefNodes.dailyComparisons.innerHTML.includes('↑ +2.17%'));
assert.ok(briefNodes.dailyComparisons.innerHTML.includes('仅已知数据比较'));
assert.ok(briefCtx.briefChange(-2.17).includes('is-down'));
assert.ok(!/NaN|undefined/.test(Object.values(briefNodes).map(n=>n.innerHTML).join('')));

function sourceFunction(source, name) {
  const match = new RegExp('^([ \\t]*)function ' + name + '\\(', 'm').exec(source);
  assert.ok(match, name);
  const tail = source.slice(match.index);
  const firstLine = tail.split('\n')[0];
  if (firstLine.trim().endsWith('}')) return firstLine;
  const end = new RegExp('^' + match[1] + '\\}[^\\S\\n]*$', 'm').exec(tail);
  assert.ok(end, name + ' closing brace');
  return tail.slice(0, end.index + end[0].length);
}

for (const source of [admin, portal]) {
  for (const match of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
    new vm.Script(match[1]);
  }
}

for (const [now, yesterday, today] of [
  ['2026-09-01T00:00:00+08:00', '2026-08-31', '2026-09-01'],
  ['2027-01-01T02:00:00+08:00', '2026-12-31', '2027-01-01'],
  ['2026-08-31T07:59:59+08:00', '2026-08-30', '2026-08-31'],
  ['2026-08-31T15:00:00+08:00', '2026-08-30', '2026-08-31'],
]) {
  const nodes = Object.fromEntries(['bsDateInput', 'dailyDateInput', 'fcDateInput', 'dqDateInput', 'endDate'].map(id => [id, {value: ''}]));
  class TestDate extends Date { constructor(...args) { super(...(args.length ? args : [now])); } }
  const ctx = vm.createContext({Date: TestDate, document: {getElementById: id => nodes[id]}});
  for (const name of ['monLocalDate', 'initBsDate', 'initDailyDate', 'initFcDate', 'initDqDate']) {
    vm.runInContext(sourceFunction(admin, name), ctx);
  }
  for (const name of ['initBsDate', 'initDailyDate', 'initFcDate', 'initDqDate']) ctx[name]();
  for (const id of ['bsDateInput', 'dailyDateInput', 'fcDateInput']) assert.equal(nodes[id].value, yesterday, now + ' ' + id);
  assert.equal(nodes.dqDateInput.value, today);
  nodes.bsDateInput.value = '2026-01-10';
  ctx.initBsDate();
  assert.equal(nodes.bsDateInput.value, '2026-01-10');
  nodes.dqDateInput.value = '';
  nodes.endDate.value = '2026-02-10';
  ctx.initDqDate();
  assert.equal(nodes.dqDateInput.value, '2026-02-10');
}

const samples = [100, null, null, 80, 0].map((daily, i) => ({date: '2026-08-0' + (i + 1), daily}));
const adminNode = {innerHTML: ''};
const adminCtx = vm.createContext({document: {getElementById: () => adminNode}, esc: String});
vm.runInContext(sourceFunction(admin, 'renderTrend'), adminCtx);
adminCtx.renderTrend({daily_trend: samples}, value => String(value));
assert.equal((adminNode.innerHTML.match(/<circle /g) || []).length, 3);
const lines = [...adminNode.innerHTML.matchAll(/<polyline points="([^"]+)"/g)];
assert.deepEqual(lines.map(line => line[1].split(' ').length), [1, 2]);
assert.ok(adminNode.innerHTML.includes('缺'));
assert.ok(!/NaN|undefined/.test(adminNode.innerHTML));
adminCtx.renderTrend({daily_trend: [samples[0]]}, String);
assert.equal((adminNode.innerHTML.match(/<circle /g) || []).length, 1);
assert.ok(!/NaN|undefined/.test(adminNode.innerHTML));

function node(tag, attrs = {}) { return {tag, attrs, children: [], textContent: '', appendChild(child) { this.children.push(child); }}; }
const portalNode = node('container');
const portalCtx = vm.createContext({
  byId: () => portalNode,
  clear: el => { el.children = []; },
  emptyState: message => { const el = node('div'); el.textContent = message; return el; },
  svgNode: node,
  document: {createElement: node},
});
vm.runInContext(sourceFunction(portal, 'renderTrend'), portalCtx);
portalCtx.renderTrend(samples);
const svg = portalNode.children.find(el => el.tag === 'svg');
assert.ok(svg);
assert.equal(svg.children.filter(el => el.tag === 'circle').length, 3);
assert.deepEqual(svg.children.filter(el => el.tag === 'polyline').map(el => el.attrs.points.split(' ').length), [1, 2]);
assert.ok(JSON.stringify(portalNode).includes('缺'));
portalCtx.renderTrend([samples[0]]);
assert.equal(portalNode.children.find(el => el.tag === 'svg').children.filter(el => el.tag === 'circle').length, 1);
assert.ok(!/NaN|undefined/.test(JSON.stringify(portalNode)));

const missingDateNodes = {startDate: {value: ''}, endDate: {value: ''}};
const storedDates = {};
let shownSection = '';
let collectionStarts = 0;
const missingDateCtx = vm.createContext({
  document: {getElementById: id => missingDateNodes[id]},
  localStorage: {setItem: (key, value) => { storedDates[key] = value; }},
  showSection: name => { shownSection = name; },
  notify: () => {},
  collect: () => { collectionStarts += 1; },
});
vm.runInContext(sourceFunction(admin, 'collectMissingDate'), missingDateCtx);
assert.equal(missingDateCtx.collectMissingDate('2026-08-30'), false);
assert.equal(missingDateNodes.startDate.value, '2026-08-30');
assert.equal(missingDateNodes.endDate.value, '2026-08-30');
assert.equal(storedDates.dashboard_startDate, '2026-08-30');
assert.equal(storedDates.dashboard_endDate, '2026-08-30');
assert.equal(shownSection, 'dashboard');
assert.equal(collectionStarts, 1);
missingDateCtx.collectMissingDate('not-a-date');
assert.equal(collectionStarts, 1);

const venuePickerCtx = vm.createContext({});
for (const name of ['normalizeVenueOptions', 'venuePickerText']) {
  vm.runInContext(sourceFunction(admin, name), venuePickerCtx);
}
const venueOptions = venuePickerCtx.normalizeVenueOptions([
  {venue: '已停门店', operating: false},
  {venue: '在营乙店', operating: true},
  {venue: '在营甲店', operating: true},
]);
assert.deepEqual(JSON.parse(JSON.stringify(venueOptions)), [
  {venue: '在营甲店', operating: true},
  {venue: '在营乙店', operating: true},
  {venue: '已停门店', operating: false},
]);
assert.equal(venuePickerCtx.venuePickerText(['甲店', '乙店']), '甲店、乙店');
assert.equal(venuePickerCtx.venuePickerText(['甲店', '乙店', '丙店']), '已选 3 家：甲店、乙店 等');

const classList = () => ({add: () => {}, remove: () => {}});
const screenNodes = Object.fromEntries(['bsLoading', 'bsEmpty', 'bsMissing', 'bsContent'].map(id => [id, {classList: classList()}]));
screenNodes.bsMissingList = {innerHTML: ''};
const screenCtx = vm.createContext({
  document: {getElementById: id => screenNodes[id]},
  esc: String,
  renderScreenData: () => {},
});
vm.runInContext(sourceFunction(admin, 'renderScreenCache'), screenCtx);
screenCtx.renderScreenCache({kind: 'missing', status: {
  dates_available: [{date: '2026-08-31', label: '当日', total: 120000}],
  dates_missing: [{date: '2026-08-30', label: '前一日'}],
}});
assert.equal((screenNodes.bsMissingList.innerHTML.match(/采集这一天/g) || []).length, 1);
assert.ok(screenNodes.bsMissingList.innerHTML.includes('data-collect-date="2026-08-30"'));
assert.ok(!screenNodes.bsMissingList.innerHTML.includes('data-collect-date="2026-08-31"'));

console.log('PASS: template syntax, local date defaults, missing-date collection, gap-aware charts and single-day display');

// Unknown targets/comparisons must render as unknown, including the shared portal.
const incomeScreenNodes = Object.fromEntries(['bsCards','bsQuality','bsTopCN','bsTopHK','bsTopDay','bsTopHKCard'].map(id => [id,{innerHTML:'',setAttribute(){}}]));
const incomeScreenCtx = vm.createContext({document:{getElementById:id=>incomeScreenNodes[id]},esc:String,renderCompletion(){},renderTrend(){},renderRegionBar(){}});
vm.runInContext(sourceFunction(admin,'renderScreenData'),incomeScreenCtx);
const screenData={ready:true,target_date:'2026-09-07',target_month:'2026-09',curr_month_total:500085.47,avg_daily:71440.78,completion_rate:null,month_target:null,target_daily:null,last_month_total:1031227.81,month_change_pct:-51.51,today_total:37770.1,day_change_pct:null,data_issues:[],missing_target_venues:['A']};
incomeScreenCtx.renderScreenData(screenData);
assert.ok(incomeScreenNodes.bsCards.innerHTML.includes('50.01'));
assert.ok(incomeScreenNodes.bsCards.innerHTML.includes('103.12'));
assert.ok(incomeScreenNodes.bsCards.innerHTML.includes('目标未配置'));
assert.ok(incomeScreenNodes.bsCards.innerHTML.includes('日环比 —'));
assert.ok(!/NaN|undefined|Infinity/.test(incomeScreenNodes.bsCards.innerHTML));
incomeScreenCtx.renderScreenData({...screenData,month_target:300,target_daily:10,days_in_month:30,target_store_count:2,completion_rate:20});
assert.ok(incomeScreenNodes.bsCards.innerHTML.includes('目标日均 10.00万（整月 30 天）'));
for (const [value, direction, text] of [[12.5,'up','↑ +12.50%'],[-12.5,'down','↓ −12.50%'],[0,'flat','→ 0.00%'],[-0.001,'flat','→ 0.00%']]) {
  incomeScreenCtx.renderScreenData({...screenData,month_change_pct:value,day_change_pct:value});
  assert.equal((incomeScreenNodes.bsCards.innerHTML.match(new RegExp('monthly-change is-'+direction,'g'))||[]).length,2);
  assert.ok(incomeScreenNodes.bsCards.innerHTML.includes(text));
}

const formatCtx=vm.createContext({});
for(const name of ['formatWan','formatPercent','formatNumber']){
  vm.runInContext(sourceFunction(portal,name),formatCtx);
  assert.equal(formatCtx[name](null),'—');
  assert.notEqual(formatCtx[name](0),'—');
}
console.log('PASS: historical income cards, calendar daily target, and unknown-value rendering');

(async()=>{
  let calls=0,delay=null;
  const polling=vm.createContext({_currentSection:'bigscreen',document:{hidden:false},_pollTimer:null,
    checkScreenData:async options=>{assert.equal(options.force,true);calls++;},
    setTimeout:(_,ms)=>{delay=ms;}});
  vm.runInContext(sourceFunction(admin,'pollLoop'),polling);
  polling.pollLoop();await Promise.resolve();await Promise.resolve();
  assert.equal(calls,1);assert.equal(delay,30000);
  polling.document.hidden=true;polling.pollLoop();assert.equal(calls,1);

  const nodes={bsDateInput:{value:'2026-09-07'}};
  for(const id of ['bsEmpty','bsEmptyMessage','bsLoading','bsMissing','bsContent'])nodes[id]={classList:{add(){},remove(){}}};
  const pending=[],renders=[];
  const refresh=vm.createContext({document:{getElementById:id=>nodes[id]},_screenRequestVersion:0,_screenManualPending:false,_screenRenderedDate:"",
    _bigscreenCache:new Map(),Date,encodeURIComponent,
    fetch:url=>new Promise(resolve=>pending.push({url,resolve})),responsePayload:async response=>response.payload,
    renderScreenCache:entry=>renders.push(entry),setViewCacheMeta(){},notify(){}});
  const plain=admin.replace('async function checkScreenData(','function checkScreenData(');
  vm.runInContext('async '+sourceFunction(plain,'checkScreenData'),refresh);
  const old=refresh.checkScreenData({force:true});
  nodes.bsDateInput.value='2026-09-06';
  const latest=refresh.checkScreenData({force:true});
  pending[1].resolve({ok:true,payload:{dates_missing:[]}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(pending[2].url.includes('2026-09-06'));
  pending[2].resolve({ok:true,payload:{ready:true,target_date:'2026-09-06'}});
  await latest;
  pending[0].resolve({ok:true,payload:{dates_missing:[]}});await old;
  assert.equal(renders.length,1);assert.equal(renders[0].data.target_date,'2026-09-06');

  const loadingEvents=[];
  nodes.bsLoading.classList={add:value=>loadingEvents.push('hide'),remove:value=>loadingEvents.push('show')};
  const settle=async (start,request,ok=true)=>{
    pending[start].resolve({ok,payload:{dates_missing:[]}});
    await new Promise(resolve=>setImmediate(resolve));
    if(ok)pending[start+1].resolve({ok:true,payload:{ready:true,target_date:'2026-09-06'}});
    await request;
  };
  let start=pending.length;
  await settle(start,refresh.checkScreenData({force:true}));
  assert.ok(!loadingEvents.includes('show'),'automatic refresh stays silent');
  assert.equal(renders.length,1,'unchanged results preserve the DOM');
  start=pending.length;
  const manual=refresh.checkScreenData({force:true,manual:true});
  assert.equal(loadingEvents.at(-1),'show');
  const count=pending.length;
  await refresh.checkScreenData({force:true});
  assert.equal(pending.length,count,'polling cannot interrupt a manual check');
  await settle(start,manual);
  assert.equal(loadingEvents.at(-1),'hide');
  start=pending.length;
  let notices=0;refresh.notify=()=>notices++;
  await settle(start,refresh.checkScreenData({force:true}),false);
  assert.equal(notices,0,'background failure stays silent');
  assert.equal(renders.length,1,'background failure preserves content');
  start=pending.length;
  await settle(start,refresh.checkScreenData({force:true,manual:true}),false);
  assert.equal(notices,1);assert.equal(loadingEvents.at(-1),'hide');
  console.log('PASS: silent refresh, manual overlay, failure cleanup and stale-response protection');

})().catch(error=>{console.error(error);process.exitCode=1;});
