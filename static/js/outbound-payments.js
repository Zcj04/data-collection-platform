/* 出货货款独立报表：现有 fetch 包装统一附加 CSRF。 */
const OB={initialized:false,data:null,page:1,pageSize:50,total:0,sku:'',equipment:'',itemLabel:'',overviewTicket:0,detailTicket:0,detailLoading:false,timer:null,busy:false,action:false,configured:false,stamp:''};
const obEl=id=>document.getElementById(id);
const obApi='/api/admin/payment-accounting/outbound';
async function obRequest(path,options){const response=await fetch(obApi+path,options),data=await responsePayload(response);if(!response.ok)throw new Error(responseError(data,'出货数据请求失败'));return data;}
function obEmpty(id,columns,message){obEl(id).innerHTML='<tr><td colspan="'+columns+'" class="payment-empty-cell">'+esc(message)+'</td></tr>';}
function obBadge(text,warning){return'<span class="outbound-state '+(warning?'is-warning':'')+'">'+esc(text)+'</span>';}
function obMonth(){return obEl('ob-month').value;}
function obStore(){return obEl('ob-store').value;}
async function obInit(){
  if(!OB.initialized){
    OB.initialized=true;
    const dailyCard=obEl('ob-days-card');if(dailyCard&&window.matchMedia('(max-width:760px)').matches)dailyCard.open=false;
    const now=new Date(),last=new Date(now.getFullYear(),now.getMonth(),0),month=last.getFullYear()+'-'+String(last.getMonth()+1).padStart(2,'0');
    ['ob-month','ob-start','ob-end'].forEach(id=>{obEl(id).max=month;obEl(id).value=month;});
    try{const history=await obRequest('/months'),recent=history.months.find(item=>item.status==='complete');if(recent)['ob-month','ob-start','ob-end'].forEach(id=>obEl(id).value=recent.month);}catch(error){notify(error.message,'error');}
  }
  await obReload();
  obPoll();
}
async function obReload(){await Promise.all([obLoadOverview(),obLoadStatus(),obLoadMonths()]);}
function obMonthChanged(){obEl('ob-start').value=obMonth();obEl('ob-end').value=obMonth();obResetFilters();obReload();}
function obStoreChanged(){obResetFilters();return Promise.all([obLoadOverview(),obLoadMonths()]);}
function obAllStores(){obEl('ob-store').value='';obStoreChanged();}
function obResetFilters(){OB.sku='';OB.equipment='';OB.itemLabel='';OB.page=1;obEl('ob-day').value='';obEl('ob-clear-item').hidden=true;}
async function obLoadOverview(){
  const ticket=++OB.overviewTicket,month=obMonth(),source=obStore();
  ++OB.detailTicket;OB.data=null;OB.total=0;OB.detailLoading=true;obPagination();obEl('ob-record-region').setAttribute('aria-busy','true');obEl('ob-all-stores').hidden=!source;obEl('ob-status').textContent='正在读取 '+month+' 的完整月份版本…';obEl('ob-kpis').innerHTML='';obEl('ob-coverage').textContent='正在读取门店覆盖情况…';obEl('ob-detail-scope').textContent='正在读取所选月份…';obEl('ob-detail-filters').textContent='正在读取…';obEmpty('ob-stores',6,'正在读取门店…');obEl('ob-days').innerHTML='';obEmpty('ob-details',10,'正在读取月份…');
  try{
    const data=await obRequest('/overview?'+new URLSearchParams({month,source_store:source}));if(ticket!==OB.overviewTicket)return;OB.data=data;
    obEl('ob-store').innerHTML='<option value="">全部有流水门店</option>'+data.stores.map(row=>'<option value="'+esc(row.source_store)+'">'+esc(row.venue||row.source_store)+'</option>').join('');
    if(source&&!data.stores.some(row=>row.source_store===source))obEl('ob-store').insertAdjacentHTML('beforeend','<option value="'+esc(source)+'">'+esc(source)+'（本月无源记录）</option>');
    obEl('ob-store').value=source;
    const summary=data.summary,complete=data.status==='complete',run=data.run,latest=data.latest_run;
    obEl('ob-status').textContent=complete?month+' · '+run.days_expected+'/'+run.days_expected+' 天采集完整 · '+(summary.review_count?summary.review_count+' 条待核对':'采集校验通过')+' · '+(latest&&latest.id!==run.id?'新批次'+obStatusLabel(latest.status)+'，仍展示旧完整版本。':'已记录成本未计入累计货款。'):(latest?'该月采集'+obStatusLabel(latest.status)+'，尚无完整版本。':'该月份尚未采集，请展开“按月采集与任务记录”。');
    obEl('ob-status').classList.toggle('is-warning',!complete||Boolean(summary&&summary.review_count));
    const metrics=[['已记录出礼成本（元）',summary?paMoney(summary.gift_cost):'—','平台预估成本口径'],['出礼数量',summary?paNumber(summary.gift_quantity):'—','只计算设备出礼'],['有流水门店',summary?paNumber(summary.store_count):'—','不代表全部门店覆盖'],['待核对记录',summary?paNumber(summary.review_count):'—',summary?'其中零成本出礼 '+summary.zero_cost_count+' 条':'缺少数据不按零处理']];
    obEl('ob-kpis').innerHTML=metrics.map((metric,index)=>'<div class="'+(index===3&&summary&&summary.review_count?'is-warning':'')+'"><span>'+metric[0]+'</span><strong>'+metric[1]+'</strong><small>'+metric[2]+'</small></div>').join('');
    obEl('ob-coverage').textContent=complete?'总部可访问 '+data.authorized_store_count+' 家，本月有流水 '+data.stores.length+' 家。'+(data.stores_without_records.length?'无源记录：'+data.stores_without_records.join('、')+'；不填零。':''):'标准门店和源店名将在采集成功后显示。';
    obRenderStores();obRenderDays();
    const kind=obEl('ob-type').value;obEl('ob-type').innerHTML='<option value="设备出礼">设备出礼</option><option value="__adjustments__">库存调整（不含出礼）</option><option value="">全部变更</option>'+data.types.filter(row=>row.business_type!=='设备出礼').map(row=>'<option value="'+esc(row.business_type)+'">'+esc(row.business_type)+'</option>').join('');
    obEl('ob-type').value=Array.from(obEl('ob-type').options).some(option=>option.value===kind)?kind:'设备出礼';
    obEl('ob-day').min=month+'-01';const parts=month.split('-');obEl('ob-day').max=month+'-'+new Date(Number(parts[0]),Number(parts[1]),0).getDate();
    if(complete)await obLoadDetails();else{OB.total=0;obEmpty('ob-details',10,'该月暂无完整数据，请展开“按月采集与任务记录”');obEl('ob-detail-scope').textContent=month+' · 暂无完整版本';obEl('ob-detail-filters').textContent='采集完成后可查看明细。';}
  }catch(error){if(ticket!==OB.overviewTicket)return;obEl('ob-status').textContent=error.message;obEl('ob-status').classList.add('is-warning');obEl('ob-coverage').textContent='门店覆盖情况未能读取';obEl('ob-detail-scope').textContent='读取失败，请刷新重试';obEl('ob-detail-filters').textContent='';obEmpty('ob-stores',6,'读取失败，请刷新重试');obEmpty('ob-details',10,'读取失败');}
  finally{if(ticket===OB.overviewTicket&&(!OB.data||OB.data.status!=='complete')){OB.detailLoading=false;obEl('ob-record-region').setAttribute('aria-busy','false');obPagination();}}
}
function obRenderStores(){
  if(!OB.data||!OB.data.stores.length){obEmpty('ob-stores',6,'该月没有可展示的门店流水');return;}
  const sort=obEl('ob-sort').value,source=obStore(),rows=OB.data.stores.filter(row=>!source||row.source_store===source).slice().sort((a,b)=>Number(b[sort])-Number(a[sort]));
  if(!rows.length){obEmpty('ob-stores',6,'所选门店本月没有源记录');return;}
  obEl('ob-stores').innerHTML=rows.map(row=>'<tr><td><button type="button" class="outbound-link" data-ob-store="'+esc(row.source_store)+'">'+esc(row.venue||row.source_store)+'</button>'+(row.venue&&row.venue!==row.source_store?'<small class="payment-row-meta">'+esc(row.source_store)+'</small>':'')+'</td><td class="text-right">'+paNumber(row.gift_records)+'</td><td class="text-right">'+paNumber(row.gift_quantity)+'</td><td class="text-right outbound-money">'+paMoney(row.gift_cost)+'</td><td>'+row.days_with_gifts+' 天</td><td>'+obBadge(row.mapping_status==='matched'?'已匹配':'门店待匹配',row.mapping_status!=='matched')+(row.review_count?' '+obBadge('待核对 '+row.review_count+' 条',true):'')+(row.zero_cost_count?'<button type="button" class="outbound-link outbound-warning-link" data-ob-zero="'+esc(row.source_store)+'">零成本 '+row.zero_cost_count+' 条</button>':'')+'</td></tr>').join('');
}
function obRenderDays(){
  const rows=OB.data?OB.data.daily:[],selected=obEl('ob-day').value;
  obEl('ob-days-title').textContent='每日出礼 · '+(obStore()?obEl('ob-store').selectedOptions[0].textContent:'全部有流水门店');
  obEl('ob-days').innerHTML=rows.map(row=>'<button type="button" class="outbound-day '+(row.source_status==='no_source_records'?'is-empty ':'')+(row.date===selected?'is-selected':'')+'" data-ob-day="'+row.date+'" aria-pressed="'+(row.date===selected)+'" aria-label="'+row.date+' '+(row.gift_cost===null?'无源记录':'出礼成本 '+paMoney(row.gift_cost)+' 元，数量 '+paNumber(row.gift_quantity))+'"><strong>'+Number(row.date.slice(-2))+'</strong><span>'+paMoney(row.gift_cost)+'</span><small>'+(row.gift_quantity===null?'无源记录':paNumber(row.gift_quantity)+' 件')+'</small></button>').join('');
}
function obJumpToDetails(){obEl('ob-detail-section').focus({preventScroll:true});obEl('ob-detail-section').scrollIntoView({block:'start'});}
async function obDetailChanged(jump=false){OB.page=1;obRenderDays();if(await obLoadDetails()&&jump)obJumpToDetails();}
function obClearItem(){OB.sku='';OB.equipment='';OB.itemLabel='';obEl('ob-clear-item').hidden=true;obDetailChanged();}
function obClearDetail(jump=false){obResetFilters();obEl('ob-attention').checked=false;obEl('ob-type').value='设备出礼';obEl('ob-group').value='records';obDetailChanged(jump);}
async function obLoadDetails(){
  const ticket=++OB.detailTicket;if(!OB.data||OB.data.status!=='complete')return;
  const group=obEl('ob-group').value,params={month:obMonth(),source_store:obStore(),day:obEl('ob-day').value,business_type:obEl('ob-type').value,group,page:OB.page,page_size:OB.pageSize,attention:obEl('ob-attention').checked,sku_id:OB.sku,equipment_no:OB.equipment};
  obEl('ob-detail-scope').textContent=(obStore()?obEl('ob-store').selectedOptions[0].textContent:'全部有流水门店')+' · '+(params.day||obMonth())+(OB.itemLabel?' · '+OB.itemLabel:'')+'；数量和金额保留平台原始正负号。';
  obEl('ob-detail-filters').textContent=[obEl('ob-group').selectedOptions[0].textContent,obEl('ob-type').selectedOptions[0].textContent,params.day||'整月',params.attention?'仅看待核对':'全部记录',OB.itemLabel].filter(Boolean).join(' · ');
  const headers=group==='records'?['变更时间','门店','商品 / ID','变更成本（元）','变更数量','类型','设备','预估单价','原数量 → 变更后','核对问题']:['商品 / 设备','来源 ID','记录数','变更数量','变更成本（元）','待核对','操作'];
  const widths=group==='records'?[11,12,20,10,8,8,11,7,7,6]:[30,14,10,10,16,10,10],numbers=group==='records'?[3,4,7]:[3,4];
  obEl('ob-detail-head').parentElement.dataset.view=group;
  obEl('ob-detail-head').innerHTML='<tr>'+headers.map((text,index)=>'<th style="width:'+widths[index]+'%"'+(numbers.includes(index)?' class="text-right"':'')+'>'+text+'</th>').join('')+'</tr>';obEmpty('ob-details',headers.length,'正在读取明细…');OB.detailLoading=true;obEl('ob-record-region').setAttribute('aria-busy','true');obPagination();
  try{const data=await obRequest('/details?'+new URLSearchParams(params));if(ticket!==OB.detailTicket)return;OB.total=data.total;
    if(!data.rows.length)obEmpty('ob-details',headers.length,'当前筛选下没有源记录');
    else if(group==='records')obEl('ob-details').innerHTML=data.rows.map(row=>'<tr><td>'+esc(row.operation_time)+'</td><td>'+esc(row.venue||row.source_store)+'</td><td>'+esc(row.sku_name)+'<small class="payment-row-meta">'+esc(row.sku_id)+'</small></td><td class="text-right outbound-money">'+paMoney(row.cost)+'</td><td class="text-right">'+paNumber(row.stock_count)+'</td><td>'+esc(row.business_type)+'</td><td>'+esc(row.equipment_name)+'<small class="payment-row-meta">'+esc(row.equipment_no)+'</small></td><td class="text-right">'+paMoney(row.predict_cost)+'</td><td>'+paNumber(row.original_count)+' → '+paNumber(row.after_count)+'</td><td>'+row.issues.map(issue=>obBadge(issue,true)).join(' ')+'</td></tr>').join('');
    else obEl('ob-details').innerHTML=data.rows.map(row=>{const id=group==='sku'?row.sku_id:row.equipment_no,name=group==='sku'?row.sku_name:row.equipment_name;return'<tr><td>'+esc(name)+(group==='equipment'?'<small class="payment-row-meta">'+esc(row.venue||row.source_store)+'</small>':'')+'</td><td>'+esc(id||'—')+'</td><td>'+paNumber(row.records)+'</td><td class="text-right">'+paNumber(row.stock_count)+'</td><td class="text-right outbound-money">'+paMoney(row.cost)+'</td><td>'+paNumber(row.review_count)+'</td><td>'+(id?'<button type="button" class="outbound-link" data-ob-item="'+esc(id)+'" data-ob-kind="'+group+'" data-ob-label="'+esc(name)+'">查看流水</button>':'来源缺少 ID')+'</td></tr>';}).join('');
    obEl('ob-record-region').scrollTop=0;return true;
  }catch(error){if(ticket!==OB.detailTicket)return;OB.total=0;obEmpty('ob-details',headers.length,error.message);obPagination();}
  finally{if(ticket===OB.detailTicket){OB.detailLoading=false;obEl('ob-record-region').setAttribute('aria-busy','false');obPagination();}}
}
function obPagination(){const pages=Math.max(1,Math.ceil(OB.total/OB.pageSize));obEl('ob-page-info').textContent=OB.detailLoading?'正在读取明细…':OB.total?'共 '+paNumber(OB.total)+' 条 · 第 '+OB.page+' / '+pages+' 页 · 每页 '+OB.pageSize+' 条':'当前无明细';obEl('ob-prev').disabled=OB.detailLoading||!OB.total||OB.page<=1;obEl('ob-next').disabled=OB.detailLoading||!OB.total||OB.page>=pages;}
function obPage(delta){const next=OB.page+delta;if(OB.detailLoading||next<1||next>Math.ceil(OB.total/OB.pageSize))return;OB.page=next;obLoadDetails();}
async function obLoadMonths(){const source=obStore();obEmpty('ob-months',6,'正在读取逐月概览…');try{const data=await obRequest('/months?'+new URLSearchParams({source_store:source}));if(source!==obStore())return;if(!data.months.length){obEmpty('ob-months',6,'尚无采集月份，请展开“按月采集与任务记录”');return;}obEl('ob-months').innerHTML=data.months.map(item=>'<tr><td><button type="button" class="outbound-link" data-ob-month="'+item.month+'">'+item.month+'</button></td><td>'+obBadge(item.status==='complete'?'整月完整':item.latest_run?obStatusLabel(item.latest_run.status):'未采集',item.status!=='complete')+'</td><td>'+paNumber(item.summary&&item.summary.gift_quantity)+'</td><td class="outbound-money">'+paMoney(item.summary&&item.summary.gift_cost)+'</td><td>'+paNumber(item.summary&&item.summary.review_count)+'</td><td>'+esc(item.run?item.run.finished_at:'—')+'</td></tr>').join('');}catch(error){if(source===obStore())obEmpty('ob-months',6,error.message);}}
function obStatusLabel(status){return({queued:'等待中',running:'进行中',succeeded:'已完成',failed:'未完成',cancelled:'已停止'})[status]||status;}
async function obLoadStatus(){
  try{const data=await obRequest('/status');OB.busy=data.busy;OB.configured=data.credentials_configured;
    obEl('ob-start-button').disabled=OB.busy||OB.action||!OB.configured;
    obEl('ob-credential-note').textContent=OB.configured?'总部凭证已加密配置。按月份顺序读取，完整成功才替换旧版本。':'请在凭证管理 → 多金宝中填写“总部账号、总部密码”，不会覆盖普通采集账号。';
    obEl('ob-job-badge').textContent=OB.busy?'正在采集':'暂无进行中任务';
    if(!data.runs.length)obEmpty('ob-jobs',7,'暂无采集任务');
    else obEl('ob-jobs').innerHTML=data.runs.map(run=>'<tr><td>'+run.month+'</td><td>'+obBadge(obStatusLabel(run.status),run.status==='failed'||run.status==='cancelled')+(run.is_current?' 当前版本':'')+'</td><td><progress max="'+run.days_expected+'" value="'+run.days_succeeded+'" aria-label="'+run.month+' 完成天数"></progress><small>'+run.days_succeeded+'/'+run.days_expected+' 天</small></td><td>'+paNumber(run.record_count)+'</td><td>'+esc(run.current_date||'—')+(run.current_page?' / 第 '+run.current_page+' 页'+(run.current_pages?'，共 '+run.current_pages+' 页':''):'')+'</td><td class="outbound-job-error">'+esc(run.error||run.finished_at||'')+'</td><td>'+(['queued','running'].includes(run.status)?'<button type="button" class="outbound-link" data-ob-stop="'+run.id+'">停止该批次</button>':['failed','cancelled'].includes(run.status)?'<button type="button" class="outbound-link" data-ob-retry="'+run.id+'" '+(OB.busy?'disabled':'')+'>重试剩余日期</button>':'—')+'</td></tr>').join('');
    const stamp=data.runs.filter(run=>run.month===obMonth()).map(run=>run.id+run.status).join('|');if(OB.stamp&&OB.stamp!==stamp){obLoadOverview();obLoadMonths();}OB.stamp=stamp;
  }catch(error){obEl('ob-job-badge').textContent='任务状态读取失败';}
}
function obPoll(){clearTimeout(OB.timer);OB.timer=setTimeout(async()=>{if(PA.tab==='outbound'&&_currentSection==='paymentdata'){await obLoadStatus();obPoll();}},3500);}
async function obStart(){
  OB.action=true;obEl('ob-start-button').disabled=true;
  try{const data=await obRequest('/collect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_month:obEl('ob-start').value,end_month:obEl('ob-end').value})});notify(data.message,'success');obEl('ob-collection').open=true;await obLoadStatus();obPoll();}catch(error){notify(error.message,'error');}finally{OB.action=false;obEl('ob-start-button').disabled=OB.busy||!OB.configured;}
}
async function obRunAction(id,action){try{const data=await obRequest('/runs/'+encodeURIComponent(id)+'/'+action,{method:'POST'});notify(data.message,'success');await obLoadStatus();obPoll();}catch(error){notify(error.message,'error');}}
obEl('pa-panel-outbound').addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.obStore!==undefined){obEl('ob-store').value=button.dataset.obStore;obStoreChanged();}
  if(button.dataset.obZero!==undefined){obEl('ob-store').value=button.dataset.obZero;obEl('ob-attention').checked=true;obEl('ob-type').value='设备出礼';obEl('ob-group').value='records';obStoreChanged().then(()=>obJumpToDetails());}
  if(button.dataset.obDay){obEl('ob-day').value=button.dataset.obDay;obDetailChanged(true);}
  if(button.dataset.obMonth){obEl('ob-month').value=button.dataset.obMonth;obMonthChanged();}
  if(button.dataset.obItem){if(button.dataset.obKind==='sku'){OB.sku=button.dataset.obItem;OB.equipment='';}else{OB.equipment=button.dataset.obItem;OB.sku='';}OB.itemLabel=button.dataset.obLabel;obEl('ob-clear-item').hidden=false;obEl('ob-group').value='records';obDetailChanged();}
  if(button.dataset.obStop)obRunAction(button.dataset.obStop,'stop');
  if(button.dataset.obRetry)obRunAction(button.dataset.obRetry,'retry');
});
