/* V3 provider and read-only observer control. All evidence renders as text. */
'use strict';
Object.assign(fullViews,{agentsv3:['Agent 运行配置','Decision → Thinking → Reply；编程代理与只读审查独立配置。']});
Object.assign(labels,fullViews);
const v3Section=fullView('agentsv3'),v3Config=n('div'),v3Reviews=n('div'),v3Task=input('需要审查的完整任务 ID'),v3Revision=input('固定 Git revision（可留空使用最新检查点）');
async function loadV3Config(){try{const config=await api('/agents/config');v3Config.replaceChildren(n('pre',JSON.stringify(config,null,2)))}catch(e){v3Config.replaceChildren(n('p','V3 未启用或无访问权限；可在 .env 设置 OSA_V3_ENABLED=true 后重启。'))}}
async function v3Inspect(mode){const tid=v3Task.value.trim();if(!tid)throw Error('请填写任务 ID');const report=await api('/coding/tasks/'+encodeURIComponent(tid)+'/inspect','POST',{mode,revision:v3Revision.value.trim()||null});v3Reviews.replaceChildren(n('pre',JSON.stringify(report,null,2)))}
v3Section.append(panel('模型与运行时',n('p','独立配置 thinking、reply、memory 的模型路由；未单独配置时共用聊天供应商，但提示和调用目的分离。Observer 使用独立密钥；未配置时只展示快照。'),btn('刷新配置',loadV3Config),v3Config),panel('只读代码审查',n('p','不会写工作区、执行代码或修改编程任务。报告绑定固定 Git 检查点；审查意见不等于测试通过。'),v3Task,v3Revision,btn('真实状态',()=>v3Inspect('STATUS')),btn('检查快照',()=>v3Inspect('INSPECT')),btn('审查代码',()=>v3Inspect('REVIEW')),btn('历史报告',async()=>{v3Reviews.replaceChildren(n('pre',JSON.stringify(await api('/coding/tasks/'+encodeURIComponent(v3Task.value.trim())+'/reviews'),null,2)))}),v3Reviews));
const v3PriorLoad=fullLoad;fullLoad=async function(id){if(id==='agentsv3')return loadV3Config();return v3PriorLoad(id)};
const v3PriorDetails=v2Details;v2Details=async function(row){await v3PriorDetails(row);v3Task.value=row.id;v3Revision.value=row.git_commit||''};
const v3Logout=$('logout').onclick;$('logout').onclick=async()=>{await v3Logout();v3Task.value='';v3Revision.value='';v3Config.replaceChildren();v3Reviews.replaceChildren()};
const v3Foot=document.querySelector('.aside-foot');if(v3Foot)v3Foot.textContent='授权记忆 · 三段思考 · v0.4.0';
