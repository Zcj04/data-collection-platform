const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../templates/dashboard.html'),'utf8');
const nodes={};
const node=id=>nodes[id]||(nodes[id]={innerHTML:'',textContent:'',value:'2026-09-14'});
const pending=[],renders=[];
const ctx=vm.createContext({document:{getElementById:node},notify(){},
  esc:v=>String(v??'').replaceAll('<','&lt;').replaceAll('>','&gt;'),
  fetch:()=>new Promise(resolve=>pending.push(resolve))});
vm.runInContext(source.slice(source.indexOf('function fmtWan('),source.indexOf('\nfunction renderFcAnomaly(')),ctx);
ctx.renderFcAnomaly=()=>{};
ctx.renderFcChart({available:true,points:[{month:'2026-07',value:0,kind:'actual'},{month:'2026-08',value:0,kind:'projected'}],forecast:[{month:'2026-09',point:0}]});
assert.ok(!/NaN|Infinity/.test(node('fcTrendChart').innerHTML));
assert.ok(node('fcTrendChart').innerHTML.includes('按日均推演'));
ctx.renderFcChart({available:true,points:[{month:'2026-06',value:1,kind:'actual'},{month:'2026-07',value:null,kind:'actual'},{month:'2026-08',value:2,kind:'actual'}]});
assert.equal((node('fcTrendChart').innerHTML.match(/<polyline/g)||[]).length,0);
ctx.renderFcChart({available:true,points:[{month:'2026-06',value:1,kind:'actual'},{month:'2026-08',value:2,kind:'actual'}]});
assert.equal((node('fcTrendChart').innerHTML.match(/<polyline/g)||[]).length,0);
ctx.renderFcCards({available:true,target:1000,gap:-20,mtd:100,projected_end:1020});
assert.ok(node('fcCards').innerHTML.includes('预计达到目标'));
assert.ok(!node('fcCards').innerHTML.includes('已达标'));
ctx.renderFcCards({available:true,target:1000,gap:null});
assert.ok(!node('fcCards').innerHTML.includes('预计达到目标'));
(async()=>{
 ctx.renderFcCards=p=>renders.push(p);
 ctx.renderFcChart=()=>{};
 const old=ctx.loadForecast();node('fcDateInput').value='2026-09-15';const current=ctx.loadForecast();
 for(const i of [2,3])pending[i]({ok:true,json:async()=>({projection:'new'})});await current;
 for(const i of [0,1])pending[i]({ok:true,json:async()=>({projection:'old'})});await old;
 assert.deepEqual(renders,['new']);
 const failure=ctx.loadForecast();pending[4]({ok:false});pending[5]({ok:true,json:async()=>({})});await failure;
 assert.ok(node('fcCards').textContent.includes('读取失败'));
 console.log('PASS: forecast zero and missing points, inferred labels, forecast-only target claim, latest-response and failure state');
})().catch(e=>{console.error(e);process.exitCode=1;});
