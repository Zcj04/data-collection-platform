const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const admin = fs.readFileSync(path.join(root, 'templates/dashboard.html'), 'utf8');
const portal = fs.readFileSync(path.join(root, 'templates/portal.html'), 'utf8');

(async () => {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'old', cleared:false,
      replaceChildren(){this.cleared=true;}, classList:{add(){},remove(){}}});
    return nodes.get(id);
  }
  const pending = [], rendered = [];
  const context = vm.createContext({AbortController, encodeURIComponent,
    document:{getElementById:node}, window:{renderMemberWatch(){}},
    _monitorAbort:null, _monitorLoaded:false, notify(){}, console:{error(){}},
    renderMonitoring:data=>rendered.push(data),
    fetch:()=>new Promise(resolve=>pending.push(resolve))});
  vm.runInContext(admin.slice(admin.indexOf('async function loadMonitoring('), admin.indexOf('\nfunction renderMonitoring(')), context);
  node('mon-date').value='2026-09-13';
  const old=context.loadMonitoring();
  node('mon-date').value='2026-09-14';
  const current=context.loadMonitoring();
  const controller=context._monitorAbort;
  pending[0]({ok:true,json:async()=>({date:'old'})});await old;
  assert.equal(context._monitorAbort,controller,'old finally must not release current controller');
  assert.equal(rendered.length,0,'late old response must not render');
  pending[1]({ok:true,json:async()=>({date:'current'})});await current;
  assert.equal(rendered[0].date,'current');
  const failed=context.loadMonitoring();
  pending[2]({ok:false,json:async()=>({error:'failed'})});await failed;
  assert.equal(node('mon-recharge').textContent,'—');
  assert.equal(node('mon-activities').cleared,true);

  const jobs=[],updates=[];
  const dateInput={value:'2026-09-14'};
  const roles=vm.createContext({dateInput, encodeURIComponent,
    hasPermission:()=>true,fetchJson:()=>new Promise((resolve,reject)=>jobs.push({resolve,reject})),
    renderMonitoringPanel:()=>updates.push('monitor'),renderPaymentPanel:()=>updates.push('payment'),
    resultValue:(r,f)=>r.status==='fulfilled'?r.value:f,
    text:(id,value)=>updates.push([id,value]),setRolePanelStatus:(...args)=>updates.push(args),
    window:{renderMemberWatch:()=>updates.push('clear')}});
  vm.runInContext(portal.slice(portal.indexOf('      async function loadRolePanels('),portal.indexOf('      async function loadPortal(')),roles);
  const abort=new AbortController();
  const first=roles.loadRolePanels('2026-09-13',abort.signal);
  abort.abort();jobs.forEach(job=>job.reject(new Error('aborted')));await first;
  assert.equal(updates.length,0,'aborted role jobs must not overwrite current UI');
  jobs.length=0;
  const next=roles.loadRolePanels(dateInput.value,new AbortController().signal);
  jobs[0].resolve({});jobs[1].reject(new Error('offline'));jobs[2].resolve({});jobs[3].resolve({});await next;
  assert.ok(updates.some(v=>Array.isArray(v)&&v[0]==='portal-payment-rows'&&v[1]==='—'));
  assert.ok(updates.some(v=>Array.isArray(v)&&v[2]==='读取不完整'));
  const paymentPending=[],paymentRenders=[];
  const payment=vm.createContext({PD:{date:null},document:{getElementById:node},pdSetMsg(){},
    pdRenderDetail:data=>paymentRenders.push(data),
    fetch:()=>new Promise(resolve=>paymentPending.push(resolve))});
  vm.runInContext(admin.slice(admin.indexOf('async function pdOpenDate('),admin.indexOf('\nfunction pdRenderDetail(')),payment);
  const earlier=payment.pdOpenDate('2026-09-13');
  const later=payment.pdOpenDate('2026-09-14');
  paymentPending[1]({ok:true,json:async()=>({date:'new'})});await later;
  paymentPending[0]({ok:true,json:async()=>({date:'old'})});await earlier;
  assert.equal(paymentRenders.length,1);
  assert.equal(paymentRenders[0].date,'new');
  const bad=payment.pdOpenDate('2026-09-15');
  paymentPending[2]({ok:false});await bad;
  assert.ok(node('pd-detail-meta').textContent.includes('加载失败'));
  assert.ok(node('pd-detail-rows').cleared);
  console.log('PASS: latest monitoring response, controller ownership, failure clearing, aborted portal responses, partial payment unknown state');
  console.log('PASS: payment date latest-response ownership and failure state');
  const monthJobs=[],monthRenders=[];
  const monthly=vm.createContext({PA:{monthData:{old:true}},document:{getElementById:node},
    paSetStrip(){},paRenderUnmatched(){},paRenderMonth:d=>monthRenders.push(d),
    responsePayload:r=>r.json(),responseError:()=> 'failed',
    fetch:()=>new Promise(resolve=>monthJobs.push(resolve))});
  vm.runInContext(admin.slice(admin.indexOf('let _paMonthRequest='),admin.indexOf('\nfunction paRenderMonth(')),monthly);
  node('pa-month-input').value='2026-08';const m1=monthly.paLoadMonth();
  node('pa-month-input').value='2026-09';const m2=monthly.paLoadMonth();
  monthJobs[1]({ok:true,json:async()=>({month:9})});await m2;
  monthJobs[0]({ok:false,json:async()=>({})});await m1;
  assert.equal(monthly.PA.monthData.month,9,'old failure cannot clear current month');
  const m3=monthly.paLoadMonth(true),m4=monthly.paLoadMonth(true);
  monthJobs[3]({ok:true,json:async()=>({month:9,revision:2})});await m4;
  monthJobs[2]({ok:true,json:async()=>({month:9,revision:1})});await m3;
  assert.equal(monthly.PA.monthData.revision,2,'same-month refresh must also preserve latest request');
  const m5=monthly.paLoadMonth();assert.equal(monthly.PA.monthData,null);
  monthJobs[4]({ok:false,json:async()=>({})});await m5;
  assert.ok(node('pa-month-kpis').cleared);
  assert.ok(node('pa-month-rows').innerHTML.includes('读取失败'));
  console.log('PASS: month switch, repeated refresh and failure clearing');
  const summaryJobs=[],summaryRenders=[];
  const summary=vm.createContext({_dashboardDataRefreshing:false,summaryAll:{old:true},
    document:{getElementById:node},encodeURIComponent,console:{error(){}},
    renderSummary:d=>summaryRenders.push(d),renderSummaryCutoff(){},
    renderAutoCollectionStatus(){},renderAutoCollectionUnavailable(){},
    fetch:()=>new Promise(resolve=>summaryJobs.push(resolve))});
  vm.runInContext(admin.slice(admin.indexOf('async function refreshDashboardData('),admin.indexOf('\nasync function refresh(opts)')),summary);
  node('endDate').value='2026-09-13';const s1=summary.refreshDashboardData();
  node('endDate').value='2026-09-14';
  summaryJobs[0]({ok:true,json:async()=>({date:'old'})});summaryJobs[1]({ok:false});await s1;
  assert.equal(summaryRenders.length,0);
  assert.equal(summaryJobs.length,4,'date change schedules fresh summary');
  summaryJobs[2]({ok:true,json:async()=>({date:'new'})});summaryJobs[3]({ok:false});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(summaryRenders[0].date,'new');
  const s2=summary.refreshDashboardData();summaryJobs[4]({ok:false});summaryJobs[5]({ok:false});await s2;
  assert.equal(summary.summaryAll,null,'failed summary cannot retain export/filter data');
  console.log('PASS: summary date switch retries current date and failure clears old data');
})().catch(error=>{console.error(error);process.exitCode=1;});
