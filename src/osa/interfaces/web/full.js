/* Full-feature console. Reuses the authenticated API and DOM helpers in index.html. */
'use strict';
const fullViews={deepseek:['DeepSeek 连接投屏','只负责登录、扫码和查看 DeepSeek 网页桌面。'],tasksfull:['AI 工作空间','查看当前执行中的工作，以及思考模型发给工作模型的指令。'],knowledgefull:['知识库','导入文档、查看来源和管理群作用域索引。'],mediafull:['媒体素材','管理图片、音频和视频素材。'],stickersfull:['表情包','审核收集到的表情包，编辑描述与标签，查看发送次数。'],botskillsfull:['群机器人技能','审核群里观察到的机器人命令和多轮用法。'],pluginsfull:['插件管理','管理已安装的扩展能力。'],qzonefull:['QQ 空间','查看已接入的空间能力。']};
Object.assign(labels,fullViews);
const fullMain=document.querySelector('main'),fullNav=document.querySelector('nav');
function panel(title,...children){const p=n('div',undefined,'panel'),head=n('div',undefined,'panel-head'),body=n('div',undefined,'panel-body');head.append(n('h2',title));body.append(...children);p.append(head,body);return p}
function input(placeholder,tag='input'){const el=n(tag);el.placeholder=placeholder;return el}
function groupSelect(){const select=n('select');select.dataset.groupSelect='true';select.setAttribute('aria-label','所属群');for(const group of groups){const option=n('option',groupLabel(group.id));option.value=group.id;select.append(option)}return select}
function fullEmpty(target,text){target.replaceChildren(n('div',text,'empty'))}
function table(target,headers,rows){const wrap=n('div',undefined,'table-wrap'),t=n('table'),head=n('thead'),body=n('tbody'),tr=n('tr');headers.forEach(h=>tr.append(n('th',h)));head.append(tr);for(const row of rows){const line=n('tr');for(const value of row){const td=n('td');if(value instanceof Node)td.append(value);else td.textContent=String(value??'');line.append(td)}body.append(line)}t.append(head,body);wrap.append(t);target.replaceChildren(wrap);if(!rows.length)target.append(n('div','暂无记录。','empty'))}
function fullView(id){const section=n('section',undefined,'hidden space-y-6');section.id=id;fullMain.insertBefore(section,document.querySelector('.footer'));const b=n('button',undefined,'nav-btn');b.dataset.view=id;b.append(n('span','◈','icon'),n('span',fullViews[id][0]));b.onclick=()=>navigate(id);window.extraNavigation.append(b);return section}
const fullSections=Object.fromEntries(Object.keys(fullViews).map(id=>[id,fullView(id)]));
let browserFrame,historyTarget,taskTarget,approvalTarget,knowledgeTarget,mediaTarget,botSkillTarget,pluginTarget,qzoneTarget;
const deepseekStatus=n('span','尚未检查','badge');
async function closeDesktop(){browserFrame.src='about:blank';browserFrame.hidden=true;await api('/browser/session','DELETE');toast('已断开实时桌面，登录资料保留')}
async function openDesktop(){const value=await api('/browser/session','POST');browserFrame.src=value.url;browserFrame.hidden=false}
async function refreshResearch(){try{const value=await api('/browser/status');deepseekStatus.textContent=({sleeping:'空闲休眠',waiting_memory_slot:'等待其他任务完成',starting:'浏览器启动中',waiting_login:'等待登录',waiting_human:'等待人工验证',ready:'已连接'})[value.state]||value.state}catch(e){deepseekStatus.textContent='未连接：'+e.message}}
{const section=fullSections.deepseek;browserFrame=n('iframe');browserFrame.id='liveBrowser';browserFrame.title='DeepSeek 可交互浏览器桌面';browserFrame.hidden=true;browserFrame.setAttribute('allow','clipboard-read; clipboard-write');historyTarget=n('div');const actions=n('div',undefined,'toolbar');actions.append(btn('连接投屏',openDesktop,'btn primary'),btn('打开登录页面',async()=>{await openDesktop();await api('/browser/open-login','POST');toast('请在投屏中扫码或完成验证')}),btn('断开连接',closeDesktop),btn('刷新连接状态',refreshResearch),deepseekStatus);section.append(panel('DeepSeek 网页连接',actions,n('p','DeepSeek 的提问由机器人思考流程自动发起。这里仅保留登录、扫码、人工验证和实时投屏。'),browserFrame))}
let workspaceLoading=false;
let workspaceFilter='all';
let taskDetailTimer=null;
let taskDetailGeneration=0;
const taskEventLabels={
 'coding.task.created':'任务已创建','coding.task.started':'工作 Agent 已启动',
 'coding.progress.updated':'工作进度更新','coding.instruction.applied':'追加指令已应用',
 'coding.completed':'任务完成','coding.failed':'任务失败','coding.cancelled':'任务已取消',
 'coding.input_required':'等待补充信息','coding.interrupted':'任务已中断'
};
function taskStatusLabel(value){return ({QUEUED:'排队中',RUNNING:'执行中',VALIDATING:'验证中',COMPLETED:'已完成',FAILED:'执行失败',CANCELLED:'已取消',WAITING_INPUT:'需要补充信息',WAITING_APPROVAL:'等待审批',PAUSED:'已暂停',PAUSING:'正在暂停',INTERRUPTED:'已中断',DENIED:'已拒绝',CANCEL_REQUESTED:'正在取消'})[String(value).toUpperCase()]||value||'未知'}
async function workspaceCommand(id,kind){const row=await api('/coding/tasks/'+encodeURIComponent(id));await api('/work/tasks/'+encodeURIComponent(id)+'/commands','POST',{kind,payload:{},expected_version:row.version,idempotency_key:uuid()});toast('操作已接收');await refreshTasks();if($('modal').open)await openTaskDetail(id)}
async function removeWorkspaceTask(id){if(!confirm('从列表移除此任务？项目文件和执行记录会保留。'))return;const row=await api('/coding/tasks/'+encodeURIComponent(id));await api('/coding/tasks/'+encodeURIComponent(id)+'?expected_version='+row.version,'DELETE');if($('modal').open)$('modal').close();toast('已从任务列表移除');await refreshTasks()}
function workspaceActions(row){const actions=n('div',undefined,'toolbar');actions.onclick=e=>e.stopPropagation();const state=String(row.status).toUpperCase();if(['PAUSED','WAITING_INPUT','INTERRUPTED'].includes(state)||(state==='FAILED'&&['infrastructure','provider','validation'].includes(row.failure_kind)))actions.append(btn(state==='FAILED'?'重试':'继续',()=>workspaceCommand(row.id,'RESUME')));if(['RUNNING','VALIDATING','QUEUED','PAUSED','WAITING_INPUT','INTERRUPTED'].includes(state))actions.append(btn('取消任务',()=>workspaceCommand(row.id,'CANCEL')));if(!['RUNNING','VALIDATING','QUEUED','PAUSING','CANCEL_REQUESTED'].includes(state))actions.append(btn('删除记录',()=>removeWorkspaceTask(row.id),'btn small danger'));if(state==='COMPLETED'&&row.artifact_id)actions.append(btn('下载打包文件',()=>v2Download(row.id)));return actions}
function taskDetailTimeline(events){
 const box=n('div',undefined,'agent-timeline');
 if(!events.length){box.append(n('div','暂时还没有任务事件。','empty compact'));return box}
 for(const event of events){
  const payload=event.payload||{},item=n('div',undefined,'agent-timeline-item');
  const marker=n('span',undefined,'agent-timeline-marker');
  const copy=n('div',undefined,'agent-timeline-copy');
  const top=n('div',undefined,'agent-timeline-top');top.append(n('strong',taskEventLabels[event.event_type]||event.event_type),n('time',date(event.occurred_at||event.created_at)));
  const activity=payload.agent_activity&&payload.agent_activity[0];
  const detail=activity ? (activity.label+(activity.detail?'：'+activity.detail:'')) : (payload.current_step|| (payload.status?taskStatusLabel(payload.status):(payload.evidence_ref?'已记录执行证据':'已写入任务事件')));
  copy.append(top,n('p',detail,'note'));item.append(marker,copy);box.append(item);
 }
 return box;
}
async function openTaskDetail(taskId){
 const generation=++taskDetailGeneration;
 if(taskDetailTimer){clearInterval(taskDetailTimer);taskDetailTimer=null}
 const body=n('div',undefined,'agent-task-detail');
 let closed=false,loading=false,terminal=false;
 const stop=()=>{closed=true;if(generation===taskDetailGeneration&&taskDetailTimer){clearInterval(taskDetailTimer);taskDetailTimer=null}};
 body.append(n('div','正在读取任务状态…','empty'));
 modal('AI 工作任务 · '+taskId.slice(0,12),body,[btn('关闭',()=>{$('modal').close();if(taskDetailTimer){clearInterval(taskDetailTimer);taskDetailTimer=null}})]);
 $('modal').addEventListener('close',stop,{once:true});
 async function load(){
  if(closed||loading||generation!==taskDetailGeneration)return;loading=true;
  try{
   const [row,events]=await Promise.all([api('/coding/tasks/'+encodeURIComponent(taskId)),api('/coding/tasks/'+encodeURIComponent(taskId)+'/timeline')]);
   const data=row.data||row, timeline=Array.isArray(events.data)?events.data:events;
   if(closed||generation!==taskDetailGeneration)return;
   const head=n('div',undefined,'agent-task-header');
   const status=n('span',taskStatusLabel(data.status),'agent-status agent-status-'+String(data.status||'').toLowerCase());
   head.append(n('div',undefined,'agent-task-title'),status);
   head.firstChild.append(n('strong',data.current_step||'等待工作模型执行'),n('span','任务 '+taskId,'note'));
   const progress=n('div',undefined,'agent-progress');
   if(data.percent!==null&&data.percent!==undefined){const percent=Math.max(0,Math.min(100,Number(data.percent)||0));progress.append(n('div',undefined,'agent-progress-bar'));progress.firstChild.style.width=percent+'%'}else progress.append(n('span','尚无可计算的阶段进度','note'));
   const stats=n('div',undefined,'agent-task-stats');
   stats.append(n('span','阶段 '+(data.milestones_done||0)+' / '+(data.milestones_total||0)),n('span','测试通过 '+(data.tests_passed||0)),n('span','测试失败 '+(data.tests_failed||0)));
   const instruction=n('section',undefined,'agent-detail-section');instruction.append(n('h3','思考模型交给工作模型的任务'),n('p',data.requirement||'未记录任务要求','agent-detail-text'));
   const activity=n('section',undefined,'agent-detail-section');activity.append(n('h3','正在做什么'),n('p',data.current_step||'等待下一步','agent-detail-now'));
   if(data.blocker)activity.append(n('p',data.blocker,'flow-error'));
   const eventsSection=n('section',undefined,'agent-detail-section');eventsSection.append(n('h3','执行过程'),taskDetailTimeline(timeline));
   const scroll=$('modal').scrollTop,bodyScroll=$('modalBody').scrollTop;
   body.replaceChildren(head,progress,stats,instruction,activity,workspaceActions(data),eventsSection);
   $('modal').scrollTop=scroll;$('modalBody').scrollTop=bodyScroll;
   terminal=['COMPLETED','FAILED','CANCELLED','DENIED'].includes(String(data.status).toUpperCase());
   if(terminal&&taskDetailTimer){clearInterval(taskDetailTimer);taskDetailTimer=null}
  }catch(error){if(!closed)body.replaceChildren(n('p','任务详情暂时无法读取：'+error.message,'flow-error'))}finally{loading=false}
 }
 await load();if(!closed&&!terminal&&generation===taskDetailGeneration)taskDetailTimer=setInterval(load,2500);
}
async function refreshTasks(){
 if(workspaceLoading||document.hidden)return;workspaceLoading=true;
 try{
  const rows=await api('/coding/tasks');
  const active=['RUNNING','VALIDATING','QUEUED','PAUSING','CANCEL_REQUESTED'];
  const detail=rows.filter(row=>workspaceFilter==='all'||(workspaceFilter==='active'?active.includes(row.status):row.status===workspaceFilter)).sort((a,b)=>Number(active.includes(b.status))-Number(active.includes(a.status))||b.created_at-a.created_at);
  if(!token)return;taskTarget.replaceChildren();
  taskTarget.append(n('p','共 '+rows.length+' 项 · 执行中 '+rows.filter(r=>active.includes(r.status)).length+' · 已完成 '+rows.filter(r=>r.status==='COMPLETED').length+' · 失败 '+rows.filter(r=>r.status==='FAILED').length,'note'));
  for(const row of detail){
   const card=n('article',undefined,'workspace-task anime-subcard');
   card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label','查看任务详情');
   card.onclick=()=>openTaskDetail(row.id);card.onkeydown=e=>{if(e.target===card&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openTaskDetail(row.id)}};
   const head=n('div',undefined,'section-heading');head.append(n('strong',row.current_step||'等待工作模型执行'),badge(row.status));
   const original=n('section'),requirement=row.requirement||row.goal||'未记录指令';original.append(n('h3','思考模型交给工作模型的指令'),n('p',requirement.slice(0,240)+(requirement.length>240?'…':''),'workspace-instruction'));
   const latest=(row.requirement_log||[]).slice(-1)[0];
   if(latest)original.append(n('h4','追加指令'),n('p',typeof latest==='string'?latest:latest.text,'workspace-instruction'));
   card.append(head,original,n('p','最近更新 '+date(row.last_progress_at||row.updated_at||row.created_at),'note'),n('span','点击查看实时工作详情 →','workspace-task-open'));
   card.append(workspaceActions(row));
   if(row.blocker){
    const legacyGatewayFailure=/HTTPStatusError/.test(row.blocker)&&/internal\/work-model/.test(row.blocker);
    card.append(n('p',legacyGatewayFailure?'工作模型接口请求失败，任务尚未完成；这不是要求你补充需求。请检查 AI 工作模型的服务状态。':row.blocker,'flow-error'));
   }
   taskTarget.append(card);
  }
  if(!detail.length)fullEmpty(taskTarget,'当前筛选条件下没有任务。');
 }finally{workspaceLoading=false;}
}
{taskTarget=n('div');approvalTarget=n('div');const filter=n('select');filter.setAttribute('aria-label','筛选任务状态');for(const [value,label] of [['all','全部任务'],['active','执行中'],['COMPLETED','已完成'],['FAILED','执行失败'],['WAITING_INPUT','需要补充信息'],['CANCELLED','已取消']]){const option=n('option',label);option.value=value;filter.append(option)}filter.onchange=()=>{workspaceFilter=filter.value;refreshTasks()};fullSections.tasksfull.append(panel('工作任务',filter,btn('刷新状态',refreshTasks),taskTarget))}
async function refreshKnowledge(){const values=await api('/knowledge/documents');table(knowledgeTarget,['文档','作用域','版本 / 状态','操作'],values.map(row=>[row.payload.title,row.group_id?groupLabel(row.group_id):'私有/公共',String(row.payload.version)+' / '+row.status,btn('删除索引',async()=>{if(!confirm('删除此文档及所有切片和向量索引？'))return;await api('/knowledge/documents/'+row.id,'DELETE');await refreshKnowledge()},'btn small danger')]))}
{knowledgeTarget=n('div');const group=groupSelect(),title=input('文档标题'),text=input('粘贴文档内容','textarea'),file=input('','input');file.type='file';file.accept='.pdf,.docx,.txt,.md,.csv,.json';const form=n('div',undefined,'full-form');form.append(group,title,text,btn('导入文本',async()=>{await api('/knowledge/documents','POST',{title:title.value,text:text.value,group_id:group.value});await refreshKnowledge()},'btn primary'),file,btn('上传并解析文件',async()=>{if(!file.files[0])throw Error('请选择文件');const body=new FormData();body.append('file',file.files[0]);const r=await fetch('/api/v1/knowledge/upload?group_id='+encodeURIComponent(group.value),{method:'POST',headers:{Authorization:'Bearer '+token},body});if(!r.ok)throw Error('上传/解析失败，请确认群内容索引权限和 indexer-worker');await refreshKnowledge()}));fullSections.knowledgefull.append(panel('导入群知识',n('p','先在群策略中开启 file_content_index。PDF/DOCX 在独立解析 Worker 中处理。'),form),panel('文档与索引',btn('刷新',refreshKnowledge),knowledgeTarget))}
async function downloadAsset(id){const r=await fetch('/api/v1/media/'+id+'/content',{headers:{Authorization:'Bearer '+token}});if(!r.ok)throw Error('附件不可下载');const url=URL.createObjectURL(await r.blob()),a=n('a');a.href=url;a.download=id;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
async function refreshMedia(){const values=await api('/media');table(mediaTarget,['素材','群','状态','操作'],values.map(row=>{const actions=n('div',undefined,'toolbar');actions.append(btn('下载',()=>downloadAsset(row.id)),btn('分析',async()=>json('媒体分析结果',await api('/media/'+row.id+'/analyze?question='+encodeURIComponent('描述内容'),'POST'))));if(row.payload.type==='image')actions.append(btn('加入表情库',async()=>{const tags=prompt('标签，用英文逗号分隔','');if(tags!==null){await api('/stickers/'+row.id+'?tags='+encodeURIComponent(tags),'POST');toast('已加入跨群共用表情库')}}));actions.append(btn('删除',async()=>{if(confirm('删除素材与关联分析？')){await api('/media/'+row.id,'DELETE');await refreshMedia()}},'btn small danger'));return [row.payload.type+' / '+row.id.slice(0,12),row.group_id?groupLabel(row.group_id):'私有',badge(row.status),actions]}))}
{mediaTarget=n('div');const group=groupSelect();const file=input('');file.type='file';const kind=n('select');for(const [value,label] of [['image','图片'],['record','音频'],['video','视频']]){const o=n('option',label);o.value=value;kind.append(o)}const form=n('div',undefined,'full-form');form.append(group,kind,file,btn('上传媒体',async()=>{if(!file.files[0])throw Error('请选择文件');const body=new FormData();body.append('file',file.files[0]);const r=await fetch('/api/v1/media/upload?group_id='+encodeURIComponent(group.value)+'&kind='+kind.value,{method:'POST',headers:{Authorization:'Bearer '+token},body});if(!r.ok)throw Error('上传失败');await refreshMedia()},'btn primary'));fullSections.mediafull.append(panel('媒体上传 / 音轨理解',form),panel('素材与分析',btn('刷新',refreshMedia),mediaTarget))}
let stickerRows=[],stickerPage=0;
const stickerPreviewUrls=[];
const stickerStatus=n('select'),stickerSearch=input('搜索描述或标签'),stickerGallery=n('div',undefined,'sticker-grid'),stickerPager=n('div',undefined,'toolbar');
for(const [value,label] of [['','全部状态'],['candidate','待审核'],['active','已启用'],['rejected','已拒绝']])stickerStatus.append(Object.assign(n('option',label),{value}));
function stickerState(value){return ({candidate:'待审核',active:'已启用',rejected:'已拒绝'})[value]||value}
async function refreshStickers(){stickerRows=await api('/stickers');stickerPage=0;renderStickers()}
function renderStickers(){
  stickerPreviewUrls.splice(0).forEach(url=>URL.revokeObjectURL(url));stickerGallery.replaceChildren();stickerPager.replaceChildren();
  const query=stickerSearch.value.trim().toLowerCase();
  const visible=stickerRows.filter(row=>(!stickerStatus.value||row.status===stickerStatus.value)&&(!query||[row.payload.description,...(row.payload.tags||[])].join(' ').toLowerCase().includes(query)));
  const pages=Math.max(1,Math.ceil(visible.length/24));stickerPage=Math.min(stickerPage,pages-1);
  for(const row of visible.slice(stickerPage*24,(stickerPage+1)*24)){
    const card=n('article',undefined,'sticker-card'),image=n('img');image.alt=row.payload.description||'表情包预览';image.loading='lazy';
    fetch('/api/v1/stickers/'+encodeURIComponent(row.id)+'/thumbnail',{headers:{Authorization:'Bearer '+token}}).then(response=>{if(!response.ok)throw Error('缩略图不可用');return response.blob()}).then(blob=>{if(!card.isConnected)return;const url=URL.createObjectURL(blob);stickerPreviewUrls.push(url);image.src=url}).catch(()=>{image.alt='预览不可用'});
    const description=n('p',row.payload.description||'暂无描述','sticker-description');
    const origin=row.payload.origin_group_id||row.group_id;
    const meta=n('p','全群共用'+(origin?' · 来源：'+groupLabel(origin):'')+' · '+stickerState(row.status)+' · 已发送 '+(row.payload.uses||0)+' 次','sticker-meta');
    const tags=n('p',(row.payload.tags||[]).join(' · ')||'未设置标签','sticker-meta');
    const actions=n('div',undefined,'toolbar');
    actions.append(btn('编辑',async()=>{const nextDescription=prompt('描述与情绪',row.payload.description||'');if(nextDescription===null)return;const nextTags=prompt('标签（逗号分隔）',(row.payload.tags||[]).join(','));if(nextTags===null)return;await api('/stickers/'+row.id,'PATCH',{status:row.status,description:nextDescription,tags:nextTags.split(',').map(value=>value.trim()).filter(Boolean)});await refreshStickers()}));
    if(row.status!=='active')actions.append(btn('启用',async()=>{await api('/stickers/'+row.id,'PATCH',{status:'active',description:row.payload.description||'',tags:row.payload.tags||[]});await refreshStickers()},'btn small primary'));
    if(row.status==='active')actions.append(btn('停用',async()=>{await api('/stickers/'+row.id,'PATCH',{status:'candidate',description:row.payload.description||'',tags:row.payload.tags||[]});await refreshStickers()}));
    if(row.status!=='rejected')actions.append(btn('拒绝',async()=>{await api('/stickers/'+row.id,'PATCH',{status:'rejected',description:row.payload.description||'',tags:row.payload.tags||[]});await refreshStickers()}));
    actions.append(btn('删除',async()=>{if(!confirm('删除这条表情记录？'))return;await api('/stickers/'+row.id,'DELETE');await refreshStickers()},'btn small danger'));
    card.append(image,description,meta,tags,actions);stickerGallery.append(card);
  }
  if(!visible.length)stickerGallery.append(n('p','暂无符合条件的表情包。','empty'));
  stickerPager.append(btn('上一页',()=>{stickerPage--;renderStickers()}),n('span',`${stickerPage+1} / ${pages} · 共 ${visible.length} 张`),btn('下一页',()=>{stickerPage++;renderStickers()}));
  stickerPager.firstChild.disabled=stickerPage===0;stickerPager.lastChild.disabled=stickerPage>=pages-1;
}
stickerStatus.onchange=()=>{stickerPage=0;renderStickers()};stickerSearch.oninput=()=>{stickerPage=0;renderStickers()};
{const uploadGroup=groupSelect(),file=input('');file.type='file';file.accept='image/png,image/jpeg,image/gif,image/webp';const tags=input('标签（逗号分隔，可留空）');const form=n('div',undefined,'full-form');form.append(uploadGroup,file,tags,btn('上传并启用',async()=>{if(!file.files[0])throw Error('请选择图片');const body=new FormData();body.append('file',file.files[0]);const response=await fetch('/api/v1/media/upload?group_id='+encodeURIComponent(uploadGroup.value)+'&kind=image',{method:'POST',headers:{Authorization:'Bearer '+token},body});if(!response.ok)throw Error('图片上传失败');const asset=(await response.json()).data;await api('/stickers/'+asset.id+'?tags='+encodeURIComponent(tags.value),'POST');file.value='';tags.value='';await refreshStickers();toast('表情包已启用，所有授权群可使用')},'btn primary'));const filters=n('div',undefined,'toolbar');filters.append(stickerStatus,stickerSearch,btn('刷新',refreshStickers));fullSections.stickersfull.append(panel('跨群表情包库',n('p','仅明确判定为表情的图片进入待审核；启用后供所有授权群使用。'),filters,stickerGallery,stickerPager),panel('手动添加到共用库',form))}
function skillState(value){return ({candidate:'待审核',active:'已启用',rejected:'已拒绝'})[String(value)]||value||'未知'}
async function refreshBotSkillsProfiles(){
 const [state, data]=await Promise.all([api('/bot-skills/status'),api('/bot-skills/profiles')]);
 const profiles=data.profiles||[], workflows=data.workflows||[];
 const admins=field('可登记外部机器人的 QQ 管理员（每行一个）',(state.qq_admin_ids||[]).join('\n'),'textarea');
 const adminPanel=panel('管理员设置',n('p','群主/群管理员会自动获得权限；这里可额外指定 QQ 号。保存后立即生效。','note'),admins.wrap,btn('保存管理员',async()=>{await api('/bot-skills/admins','PUT',{qq_ids:admins.control.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean)});toast('机器人学习管理员已保存');await refreshBotSkills()},'btn primary'));
 const group=groupSelect(),bot=input('机器人 QQ 号'),name=input('备注名称'),register=btn('登记机器人',async()=>{await api('/bot-skills/profiles','POST',{group_id:group.value,bot_id:bot.value.trim(),name:name.value.trim()});toast('外部机器人已登记');await refreshBotSkills()},'btn primary');
 const cards=n('div',undefined,'stack');for(const row of profiles){const p=row.payload||{},card=n('article',undefined,'anime-subcard rounded-xl p-4 stack');card.append(n('strong',(p.bot_name||'未命名机器人')+' · '+p.bot_id),n('p',p.summary||'尚未观察到足够的使用记录。','note'),n('p','已学习指令：'+(p.capabilities||[]).join('、')+'；多步流程：'+(p.workflow_count||0)+' 条','note'));cards.append(card)}if(!profiles.length)fullEmpty(cards,'还没有登记外部机器人。');
 const flows=n('div',undefined,'stack');for(const row of workflows){const p=row.payload||{},card=n('article',undefined,'anime-subcard rounded-xl p-4 stack');card.append(n('strong','机器人 '+p.bot_id),n('p',p.summary||'多步交互流程','note'),n('pre',JSON.stringify(p.steps||[],null,2)));flows.append(card)}if(!workflows.length)fullEmpty(flows,'还没有学习到多步交互流程。');
 fullSections.botskillsfull.replaceChildren(adminPanel,panel('登记外部机器人',n('p','也可以在群里 @ 机器人并说“这是个机器人”。','note'),group,bot,name,register),panel('机器人档案',cards),panel('已学习的多步流程',flows));
}
async function refreshBotSkills(){const [state,rows]=await Promise.all([api('/bot-skills/status'),api('/bot-skills')]);table(botSkillTarget,['机器人','触发方式','回复示例','来源群','置信度 / 状态','操作'],rows.map(row=>{const p=row.payload||{},actions=n('div',undefined,'toolbar');if(state.enabled&&row.status==='candidate')actions.append(btn('启用',async()=>{await api('/bot-skills/'+encodeURIComponent(row.id)+'/decision','POST',{allowed:true});await refreshBotSkills()},'btn small primary'),btn('拒绝',async()=>{await api('/bot-skills/'+encodeURIComponent(row.id)+'/decision','POST',{allowed:false});await refreshBotSkills()},'btn small danger'));actions.append(btn('查看原始',()=>json('技能观察样本',p)));return [p.bot_name||p.bot_id||'未知机器人',p.trigger_command||'@触发',p.response_text||'[媒体/列表]',groupLabel(row.group_id||''),state.enabled?Math.round(Number(p.confidence||0)*100)+'% / '+skillState(row.status):'模块已停用 / 历史记录',actions]}))}
{botSkillTarget=n('div');fullSections.botskillsfull.append(panel('群机器人技能',n('p','管理登记、管理员和已学习的多步机器人流程。','note'),botSkillTarget))}
async function refreshPlugins(){const rows=await api('/plugins');table(pluginTarget,['插件','状态','权限','操作'],rows.map(row=>{const actions=n('div',undefined,'toolbar');actions.append(btn('Manifest',()=>json('插件声明',row.payload.manifest)),btn('启用/授权',async()=>{const value=prompt('填写授权JSON',JSON.stringify({grants:row.payload.manifest.permissions.map(p=>p.capability),groups:groups.map(g=>g.id)},null,2));if(value){await api('/plugins/'+row.id+'/enable','POST',JSON.parse(value));await refreshPlugins()}}),btn('停用',async()=>{await api('/plugins/'+row.id+'/disable','POST');await refreshPlugins()}),btn('运行',async()=>{const value=prompt('执行参数JSON',JSON.stringify({group_id:groups[0]?.id,arguments:{}},null,2));if(value)json('插件结果',await api('/plugins/'+row.id+'/run','POST',JSON.parse(value)))}));return [row.payload.manifest.name+' / '+row.payload.manifest.version,badge(row.status),(row.payload.grants||[]).join(', '),actions]}))}
{pluginTarget=n('div');const manifest=input('Manifest JSON','textarea'),source=input('插件源代码：实现 run(context, arguments) 并返回 JSON 值','textarea');manifest.value=JSON.stringify({id:'example.hello',name:'Hello',version:'0.1.0',api_version:'1.0',plugin_type:'tool',isolation:'process',permissions:[]},null,2);source.value='def run(context, arguments):\n    return {"text": "你好", "group": context["group_id"]}';fullSections.pluginsfull.append(panel('注册隔离插件',manifest,source,btn('安装（默认停用）',async()=>{await api('/plugins','POST',{manifest:JSON.parse(manifest.value),source:source.value});await refreshPlugins()},'btn primary')),panel('插件与已授予权限',btn('刷新',refreshPlugins),pluginTarget))}
{qzoneTarget=n('div');const action=input('操作JSON','textarea');action.value=JSON.stringify({action:'post',arguments:{text:'待审阅的动态内容'}},null,2);fullSections.qzonefull.append(panel('QQ 空间适配器',n('p','需配置具有授权的 QZone 适配器。点赞、评论和发帖均进入审批，不会自动批量互动。'),btn('读取动态',async()=>json('QQ空间动态',await api('/qzone/timeline'))),action,btn('提交待审批操作',async()=>{const result=await api('/qzone/actions','POST',JSON.parse(action.value));toast('待审批任务：'+result.id)},'btn primary')))}
async function fullLoad(id){if(id==='deepseek')await refreshResearch();if(id==='tasksfull')await refreshTasks();if(id==='knowledgefull')await refreshKnowledge();if(id==='mediafull')await refreshMedia();if(id==='stickersfull')await refreshStickers();if(id==='botskillsfull')await refreshBotSkillsProfiles();if(id==='pluginsfull')await refreshPlugins();}
const oldLogout=$('logout').onclick;$('logout').onclick=async()=>{api('/browser/session','DELETE').catch(()=>{});if(browserFrame){browserFrame.src='about:blank';browserFrame.hidden=true}oldLogout();for(const target of [historyTarget,taskTarget,approvalTarget,knowledgeTarget,mediaTarget,botSkillTarget,pluginTarget])fullEmpty(target,'请连接管理员控制台')};
setInterval(()=>{if(token&&view==='deepseek')refreshResearch().catch(()=>{});if(token&&view==='tasksfull')refreshTasks().catch(()=>{})},3000);
// Reflect the new version and actual implementation scope in the original overview.
const foot=document.querySelector('.aside-foot');if(foot)foot.innerHTML='统一身份 · 多群协作<br>权限先于检索<br><br>v0.2.0 / Python';
for(const hint of document.querySelectorAll('#overview > .hint'))hint.textContent='完整版工程：文字聊天、授权记忆、自动巩固、文件与向量知识、多模态、DeepSeek实时浏览器、隔离执行与审批。外部能力需配置服务并授权。';

const originalLoadGroups=loadGroups;
loadGroups=async function(){await originalLoadGroups();for(const select of document.querySelectorAll('select[data-group-select]')){const chosen=select.value;select.replaceChildren(...groupSelect().children);if(groups.some(g=>g.id===chosen))select.value=chosen}const info=await api('/system/info');for(const id of Object.keys(fullViews)){const button=document.querySelector('nav button[data-view="'+id+'"]');if(button)button.hidden=info.role!=='owner'}for(const id of ['pauseBot','resumeBot'])$(id).hidden=info.role!=='owner';for(const b of document.querySelectorAll('#groupsTable button'))b.disabled=!['owner','admin'].includes(info.role)};
