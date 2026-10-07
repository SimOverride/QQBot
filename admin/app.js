// 本地维护界面：草稿与已保存内容分离，模型建议必须经过差异确认。
let token = '', inventory = {}, page = 'people', current = null, draft = null;
let chat = [], proposal = null, pending = null, busy = false, offset = 0;
const $ = s => document.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const text = v => typeof v === 'string' ? v : JSON.stringify(v, null, 2);
const labels = {summary:'群摘要',knowledge:'群认知 · 共识与背景',activity:'主动参与积极性（0–100）',interests:'群兴趣（每行一项）',persona:'群人格',style:'群表达风格',description:'画面、文字、情绪与适用场景',content:'归档消息正文'};
function status(message, error=false) { $('#status').textContent = message; $('#status').className = error?'error':''; }
async function api(path, data, options={}) {
  const init = {...options,headers:{'X-Console-Token':token,...options.headers}};
  if(data !== undefined){init.method='POST';init.body=JSON.stringify(data);init.headers['Content-Type']='application/json';}
  const response=await fetch(path,init);
  if(!response.ok){let e;try{e=await response.json();}catch{e={detail:'后台请求失败，请检查模型配置或运行状态'};}throw new Error(typeof e.detail==='string'?e.detail:'输入格式无效');}
  return options.blob?response:response.json();
}
function dirty(){return current && text(draft)!==text(current.value);}
async function navigate(next){
  if(busy){status('正在等待模型建议，请稍后切换');return;}
  if(dirty()&&!window.confirm('当前草稿还没有保存，放弃草稿并切换？'))return;
  page=next;current=null;draft=null;chat=[];proposal=null;offset=0;
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===page));
  status('');
  try{await renderPage();}catch(e){status(e.message,true);}
}
async function renderPage(){
  const container=$('#content');
  if(page==='migration'){renderMigration();return;}
  if(page==='changes'){await renderChanges();return;}
  if(page==='messages'){renderMessages();return;}
  container.innerHTML=(page==='prompts'?'<div class="preview"><details><summary>查看完整请求组装预览（不调用模型）</summary><div class="toolbar"><input id="preview-group" placeholder="群号 / 私聊负QQ" aria-label="预览会话"><input id="preview-user" placeholder="当前用户 QQ" aria-label="预览用户"><input id="preview-text" class="wide" placeholder="测试消息" value="你好"><button id="preview-run">组装预览</button></div><pre id="preview-result">选择会话和用户后，可查看 system、背景记忆、历史问答、当前消息及工具。</pre></details></div>':'')+
  '<div class="workspace"><section class="panel"><div class="panel-title"><input class="search" id="filter" placeholder="搜索 / 筛选" aria-label="筛选资源"><div class="count" id="list-count"></div></div><div id="list" class="listing"></div></section><section class="panel" id="editor"><div class="empty">从左侧选择一项，开始查看与维护</div></section><aside class="panel assistant" id="assistant"><div class="panel-title"><h3>✦ AI 辅助修改</h3><span class="badge">确认后保存</span></div><div class="panel-body"><p class="note">选中资源后，描述你希望如何修改。个人与群调整会参考当前会话最近 30 条记录；表情调整会附原图。选中内容将发送给已配置的模型服务。</p><div id="chat" class="chat"></div><textarea id="instruction" placeholder="例如：根据最近聊天整理群摘要，不推断没有依据的事实" aria-label="修改要求" disabled></textarea><button id="suggest" disabled>生成修改建议</button><button id="review-proposal" class="quiet" hidden>查看建议差异</button></div></aside></div>';
  let items=[];
  if(page==='people')items=(inventory.people||[]).map(p=>({...p,name:p.name,subtitle:`QQ ${p.usr} · ${p.grp>0?'群 '+p.grp:'私聊'} · ${p.count} 条`}));
  if(page==='groups')items=(inventory.groups||[]).map(g=>({...g,name:'群 '+g.grp,subtitle:g.count+' 条记录'}));
  if(page==='prompts')items=[...(inventory.personas||[]).map(p=>({...p,name:'人格 · '+p.name})),...(inventory.styles||[]).map(p=>({...p,name:'风格 · '+p.name})),...(inventory.prompts||[])];
  if(page==='emotes')items=await api('/api/emotes');
  function list(){const query=$('#filter').value.toLowerCase();const found=items.filter(i=>(i.name+' '+i.subtitle+' '+i.resource).toLowerCase().includes(query));$('#list-count').textContent=`${found.length} 项`;
    $('#list').className=page==='emotes'?'tiles':'listing';
    $('#list').innerHTML=found.length?found.map(i=>page==='emotes'?`<button class="tile" data-id="${esc(i.resource)}" title="${esc(i.name)}"><img loading="lazy" src="/api/image?name=${encodeURIComponent(i.name)}" alt="${esc(i.name)}"><small>${esc(i.name)}</small></button>`:`<button class="list-item" data-id="${esc(i.resource)}"><strong>${esc(i.name)}</strong><small>${esc(i.subtitle||i.resource)}</small></button>`).join(''):'<div class="empty">暂无匹配数据</div>';
    $('#list').querySelectorAll('button').forEach(b=>b.onclick=()=>selectResource(b.dataset.id));}
  $('#filter').oninput=list;list();
  $('#suggest').onclick=suggest;$('#review-proposal').onclick=()=>review(proposal.value);
  if(page==='prompts')$('#preview-run').onclick=async()=>{try{$('#preview-result').textContent='正在组装…';$('#preview-result').textContent=text(await api('/api/preview',{group:Number($('#preview-group').value),user:Number($('#preview-user').value),text:$('#preview-text').value}));}catch(e){status(e.message,true);$('#preview-result').textContent=e.message;}};
}
async function selectResource(id){
  if(busy)return;
  if(dirty()&&!window.confirm('放弃当前未保存草稿？'))return;
  try{current=await api('/api/resource?id='+encodeURIComponent(id));draft=structuredClone(current.value);chat=[];proposal=null;renderEditor();
    document.querySelectorAll('[data-id]').forEach(b=>b.classList.toggle('selected',b.dataset.id===id));
    if($('#instruction')){$('#instruction').disabled=false;$('#suggest').disabled=false;$('#chat').textContent='';$('#review-proposal').hidden=true;}
    status('已载入保存版本');
  }catch(e){status(e.message,true);}
}
function renderEditor(){
  const kind=current.resource.split(':')[0];
  const title=current.title||current.resource.substring(current.resource.indexOf(':')+1);
  $('#editor').innerHTML=`<div class="panel-title"><h2>${esc(title)}</h2></div><div class="panel-body"><div id="fields"></div><div id="evidence"></div><div class="actions">${current.default!==undefined?'<button class="quiet" id="reset-default">载入默认值</button>':''}<button class="quiet" id="reload-resource">重新载入</button><button id="review-manual">查看修改差异</button></div><p class="note">修改仅在确认保存后生效。正在生成的回答仍使用当时的内容。</p></div>`;
  if(kind==='emote')$('#fields').innerHTML=`<img class="image-preview" src="/api/image?name=${encodeURIComponent(title)}" alt="当前表情">`;
  const entries=typeof draft==='string'?[['__text',draft]]:Object.entries(draft);
  for(const [key,value] of entries){
    const label=document.createElement('label');label.className='field';const span=document.createElement('span');span.textContent=key==='__text'?'提示词正文':labels[key]||key;label.append(span);
    let input;
    if(key==='persona'||key==='style'){input=document.createElement('select');for(const item of inventory[key==='persona'?'personas':'styles']||[]){const o=document.createElement('option');o.value=item.name;o.textContent=item.name;input.append(o);}input.value=value;}
    else{input=document.createElement(key==='activity'?'input':'textarea');if(key==='activity')input.type='number';input.value=Array.isArray(value)?value.join('\n'):value;if(key==='__text')input.className='long';}
    input.oninput=()=>{let v=input.value;if(key==='activity')v=Number(v);if(key==='interests')v=v.split('\n').map(s=>s.trim()).filter(Boolean);if(key==='__text')draft=v;else draft[key]=v;status('草稿尚未保存');};label.append(input);$('#fields').append(label);
  }
  if(current.placeholders?.length){const p=document.createElement('p');p.className='note';p.textContent='请保留动态占位符：'+current.placeholders.map(s=>'{'+s+'}').join('、');$('#fields').append(p);}
  if(current.evidence?.length)$('#evidence').innerHTML=`<details><summary>查看认知依据与来源</summary><pre>${esc(text(current.evidence))}</pre></details>`;
  $('#review-manual').onclick=()=>review(draft);$('#reload-resource').onclick=()=>selectResource(current.resource);
  if($('#reset-default'))$('#reset-default').onclick=()=>{draft=current.default;renderEditor();status('默认值已载入草稿，确认保存后生效');};
}
function review(value){pending=structuredClone(value);$('#before').textContent=text(current.value);$('#after').textContent=text(value);$('#confirm').showModal();}
$('#cancel-confirm').onclick=()=>$('#confirm').close();
$('#apply-confirm').onclick=async()=>{const b=$('#apply-confirm');b.disabled=true;try{current=await api('/api/resource',{resource:current.resource,value:pending,revision:current.revision});draft=structuredClone(current.value);proposal=null;renderEditor();if($('#review-proposal'))$('#review-proposal').hidden=true;$('#confirm').close();status('已保存，后续请求使用新内容；原值已保留');}catch(e){status(e.message,true);$('#confirm').close();}finally{b.disabled=false;}};
async function suggest(){
  const instruction=$('#instruction').value.trim();if(!instruction||!current)return;
  if(dirty()){status('请先保存或放弃手动草稿，再让模型基于保存版本提出建议',true);return;}
  busy=true;$('#suggest').disabled=true;chat.push({role:'user',content:instruction});chat=chat.slice(-14);$('#instruction').value='';showChat();status('模型正在提出修改建议，尚未保存…');
  try{proposal=await api('/api/suggest',{resource:current.resource,revision:current.revision,chat});chat.push({role:'assistant',content:JSON.stringify(proposal)});showChat();$('#review-proposal').hidden=false;status('建议已生成，查看前后差异后确认保存');}catch(e){status(e.message,true);}finally{busy=false;$('#suggest').disabled=false;}
}
function showChat(){$('#chat').innerHTML=chat.map(m=>{let content=m.content;if(m.role==='assistant'){try{content=JSON.parse(content).explanation;}catch{}}return `<p class="${m.role==='user'?'user':''}">${esc(content)}</p>`;}).join('');$('#chat').scrollTop=$('#chat').scrollHeight;}
function renderMessages(){
  $('#content').innerHTML='<div class="toolbar"><input id="msg-group" placeholder="群号 / 私聊负QQ" aria-label="群号筛选"><input id="msg-user" placeholder="发送者QQ" aria-label="用户筛选"><input id="msg-query" class="wide" placeholder="搜索原文关键词"><button id="msg-search">检索</button></div><section class="panel" id="messages-list"></section><div class="actions"><button class="quiet" id="prev">上一页</button><button class="quiet" id="next">下一页</button></div><div class="two-columns"><section id="editor" class="panel"></section><aside class="panel assistant"><h3>AI 辅助修改</h3><p class="note">模型只根据当前选中消息提出更正建议，确认后才保存。</p><div id="chat" class="chat"></div><textarea id="instruction" aria-label="修改要求" placeholder="描述希望怎样更正这条记录" disabled></textarea><button id="suggest" disabled>生成修改建议</button><button id="review-proposal" class="quiet" hidden>查看建议差异</button></aside></div>';
  async function load(){try{const params=new URLSearchParams({q:$('#msg-query').value,offset});if($('#msg-group').value)params.set('group',$('#msg-group').value);if($('#msg-user').value)params.set('user',$('#msg-user').value);const rows=await api('/api/messages?'+params);$('#messages-list').innerHTML=rows.length?'<table><thead><tr><th>时间 / 发送者</th><th>消息内容</th><th></th></tr></thead><tbody>'+rows.map(r=>`<tr><td>${esc(new Date(r.created*1000).toLocaleString())}<br>QQ ${r.usr}<br>会话 ${r.grp}</td><td class="message-text">${esc(r.content)}</td><td><button class="quiet" data-id="message:${r.id}">更正</button></td></tr>`).join('')+'</tbody></table>':'<div class="empty">没有匹配记录</div>';$('#messages-list').querySelectorAll('button').forEach(b=>b.onclick=()=>selectResource(b.dataset.id));$('#prev').disabled=offset===0;$('#next').disabled=rows.length<50;}catch(e){status(e.message,true);}}
  $('#suggest').onclick=suggest;$('#review-proposal').onclick=()=>review(proposal.value);
  $('#msg-search').onclick=()=>{offset=0;load();};$('#prev').onclick=()=>{offset=Math.max(0,offset-50);load();};$('#next').onclick=()=>{offset+=50;load();};load();
}
function renderMigration(){
  $('#content').innerHTML='<div class="two-columns"><section class="panel"><span class="badge">全部数据</span><h2>导出备份</h2><p>聊天与认知、群摘要、表情包、人设、提示词覆盖和 .env 配置，保存在一个普通 ZIP 中。</p><p class="note">备份含明文密钥。数据库快照包含 WAL 中已提交的记录；要求严格一致时，请先停止机器人。</p><button id="export">生成并下载备份 ↓</button></section><section class="panel"><span class="badge">同号合并</span><h2>导入数据</h2><p>先停止机器人进程，后台可以保持打开。导入前自动备份；不同 QQ 拒绝导入。</p><label class="field"><span>目标机器人 QQ（新部署必填）</span><input id="import-qq" inputmode="numeric" placeholder="目标账号"></label><label class="field"><span>迁移 ZIP（最大 512 MiB）</span><input id="import-file" type="file" accept=".zip"></label><button id="import">检查并合并</button><p class="note">已有配置优先，来源缺项补齐。失败回滚，命令行 recover 可恢复中断的导入。</p></section></div>';
  $('#export').onclick=async()=>{const b=$('#export');b.disabled=true;status('正在生成备份…');try{const response=await api('/api/export',{}, {blob:true});const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='qqbot-backup.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);status('备份已生成，浏览器已开始下载；服务器 backups 目录也保留一份');}catch(e){status(e.message,true);}finally{b.disabled=false;}};
  $('#import').onclick=async()=>{const file=$('#import-file').files[0];if(!file){status('请选择 ZIP 文件',true);return;}if(!window.confirm('确认目标机器人已停止，开始同号合并？已有数据会先自动备份。'))return;const b=$('#import');b.disabled=true;status('正在校验并合并…');try{const q=$('#import-qq').value;const result=await api('/api/import'+(q?'?bot_qq='+encodeURIComponent(q):''),undefined,{method:'POST',body:file});inventory=await api('/api/inventory');status(result.message+'；原数据备份：'+result.backup);}catch(e){status(e.message,true);}finally{b.disabled=false;}};
}
async function renderChanges(){const rows=await api('/api/changes');$('#content').innerHTML='<div class="panel">'+(rows.length?'<table><thead><tr><th>时间</th><th>资源与差异</th><th>操作</th></tr></thead><tbody>'+rows.map(r=>`<tr><td>${esc(new Date(r.created*1000).toLocaleString())}</td><td>${esc(r.resource)}<details><summary>查看前后内容</summary><pre>${esc(text(r.before))}</pre><pre>${esc(text(r.after))}</pre></details></td><td><button class="quiet" data-undo="${r.id}" ${r.status==='saved'?'':'disabled'}>撤销此修改</button></td></tr>`).join('')+'</tbody></table>':'<div class="empty">尚无后台操作记录</div>')+'</div>';document.querySelectorAll('[data-undo]').forEach(b=>b.onclick=async()=>{if(!window.confirm('恢复这条记录的修改前内容？后续已修改的资源会拒绝撤销。'))return;try{await api('/api/undo',{id:b.dataset.undo});await renderChanges();status('已撤销，并记录本次恢复');}catch(e){status(e.message,true);}});}
$('#nav').querySelectorAll('button').forEach(b=>b.onclick=()=>navigate(b.dataset.page));
$('#refresh').onclick=async()=>{try{inventory=await api('/api/inventory');await navigate(page);}catch(e){status(e.message,true);}};
window.addEventListener('beforeunload',e=>{if(dirty()||busy){e.preventDefault();e.returnValue='';}});
(async()=>{try{token=(await api('/api/session')).token;inventory=await api('/api/inventory');await navigate('people');}catch(e){status(e.message,true);}})();
