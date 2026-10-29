// 本地维护界面：草稿与已保存内容分离，模型建议必须经过差异确认。
let token = '', inventory = {}, page = 'memories', current = null, draft = null;
let chat = [], proposal = null, pending = null, busy = false, deleting = false;
let selectionVersion=0, pageVersion=0, pageController=new AbortController();
const navigationCache=new Map(), staleRequest='页面已切换';
function invalidateNavigation(){navigationCache.clear();}
async function loadNavigation(section){
  const cached=navigationCache.get(section);
  if(cached&&Date.now()-cached.time<30000){Object.assign(inventory,cached.value);return;}
  const value=await api('/api/navigation?section='+section);
  navigationCache.set(section,{value,time:Date.now()});Object.assign(inventory,value);
}
const settingsChanged=new Set();
const $ = s => document.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const text = v => typeof v === 'string' ? v : JSON.stringify(v, null, 2);
const memoryKinds={fact:'事实',event:'事件',impression:'个人认知',thought:'想法',diary:'日记',self:'自我档案',index:'索引',context:'群背景',emote:'表情经验'};
const memoryStates={active:'生效',archived:'归档',quarantine:'待核对'};
const labels = {fixed:'人工固定',state:'状态',summary:'群摘要',knowledge:'群认知 · 共识与背景',interests:'机器人兴趣（所有群聊与私聊共用）',persona:'统一人设',description:'画面、文字、情绪与适用场景',content:'记忆正文'};
// 仅通过文本节点渲染后台返回的名称片段，昵称不能注入 HTML。
function showNameParts(element,parts,fallback){
  element.replaceChildren();
  if(!Array.isArray(parts)||!parts.length){element.textContent=fallback||'暂无认知';return;}
  for(const part of parts){
    if(part.kind==='person'||part.kind==='group'){
      const span=document.createElement('span');span.className='name-'+part.kind;span.textContent=part.text;element.append(span);
    }else if(part.kind==='message'){
      const span=document.createElement('span');span.className='message-reference';span.textContent=part.text;
      span.tabIndex=0;span.setAttribute('role','button');span.setAttribute('aria-label','查看引用消息：'+part.text);
      const resource=current?.resource,scope=null;
      span.onmouseenter=()=>showMessagePreview(span,resource,scope,part.ids);
      span.onfocus=()=>showMessagePreview(span,resource,scope,part.ids);
      span.onclick=()=>showMessagePreview(span,resource,scope,part.ids);
      span.onmouseleave=schedulePreviewClose;span.onblur=schedulePreviewClose;
      span.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();showMessagePreview(span,resource,scope,part.ids);}};
      element.append(span);
    }else element.append(document.createTextNode(part.text||''));
  }
}
let messagePreview=null,previewAnchor=null,previewTimer=null,previewSequence=0;
function closeMessagePreview(){clearTimeout(previewTimer);messagePreview?.remove();messagePreview=null;previewAnchor=null;previewSequence++;}
function schedulePreviewClose(){clearTimeout(previewTimer);previewTimer=setTimeout(closeMessagePreview,250);}
function placeMessagePreview(){
  if(!messagePreview||!previewAnchor?.isConnected)return;
  const r=previewAnchor.getBoundingClientRect(),height=messagePreview.offsetHeight,width=messagePreview.offsetWidth;
  messagePreview.style.left=Math.max(12,Math.min(r.left,innerWidth-width-12))+'px';
  messagePreview.style.top=Math.max(12,r.bottom+height+12<=innerHeight?r.bottom+6:r.top-height-6)+'px';
}
async function showMessagePreview(anchor,resource,scope,ids){
  clearTimeout(previewTimer);
  if(messagePreview&&previewAnchor===anchor)return;
  closeMessagePreview();previewAnchor=anchor;
  const sequence=previewSequence,version=selectionVersion;
  const panel=document.createElement('section');panel.className='message-preview';panel.setAttribute('role','tooltip');
  const header=document.createElement('div');header.className='message-preview-header';
  const title=document.createElement('strong');title.textContent='引用消息';
  const close=document.createElement('button');close.className='quiet';close.textContent='×';close.setAttribute('aria-label','关闭消息预览');close.onclick=closeMessagePreview;
  header.append(title,close);const body=document.createElement('div');body.className='message-preview-body';body.textContent='正在读取消息…';
  panel.append(header,body);panel.onmouseenter=()=>clearTimeout(previewTimer);panel.onmouseleave=schedulePreviewClose;
  panel.onfocusin=()=>clearTimeout(previewTimer);panel.onfocusout=schedulePreviewClose;
  messagePreview=panel;document.body.append(panel);placeMessagePreview();
  if(!resource||!ids?.length){body.textContent='引用范围无效或超过 100 条，请缩小范围查看。';return;}
  try{
    const params=new URLSearchParams({resource,ids:ids.join(',')});if(scope!==null)params.set('scope',scope);
    const data=await api('/api/referenced-messages?'+params);
    if(sequence!==previewSequence||version!==selectionVersion||!anchor.isConnected)return;
    body.replaceChildren();title.textContent='引用消息 · '+data.messages.length+' 条';
    for(const item of data.messages){
      const row=document.createElement('article');row.className='message-preview-row'+(item.is_self?' self':'');
      const avatar=document.createElement('div');avatar.className='message-preview-avatar';avatar.textContent=Array.from(item.name||'?')[0];
      const column=document.createElement('div');column.className='message-preview-column';
      const meta=document.createElement('div');meta.className='message-preview-meta';
      const name=document.createElement('span');name.className='name-person';name.textContent=item.name+(item.is_self?'（机器人）':'');
      const time=document.createElement('span');time.textContent=new Date(item.time*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
      meta.append(name,time);
      const source=document.createElement('div');source.className='message-preview-source';source.textContent=item.scope_name+' · id '+item.id;
      const bubble=document.createElement('div');bubble.className='message-preview-bubble';bubble.textContent=item.text||'（无文字内容）';
      column.append(meta,source,bubble);row.append(avatar,column);body.append(row);
    }
    if(data.missing.length){const note=document.createElement('p');note.className='note';note.textContent='以下引用未找到或不属于当前会话：'+data.missing.join('、');body.append(note);}
    placeMessagePreview();
  }catch(error){if(sequence===previewSequence){body.textContent=error.message;placeMessagePreview();}}
}
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeMessagePreview();});
window.addEventListener('resize',closeMessagePreview);
window.addEventListener('scroll',event=>{if(messagePreview&&!messagePreview.contains(event.target))closeMessagePreview();},true);
function status(message, error=false) { if(message===staleRequest)return; $('#status').textContent = message; $('#status').className = error?'error':''; }
async function api(path, data, options={}) {
  const version=pageVersion,read=data===undefined&&(!options.method||options.method==='GET')&&!options.detached;
  const init={...options,...(read?{signal:pageController.signal}:{}),headers:{'X-Console-Token':token,...options.headers}};
  if(data!==undefined){init.method='POST';init.body=JSON.stringify(data);init.headers['Content-Type']='application/json';}
  try{
    const response=await fetch(path,init);
    if(read&&version!==pageVersion)throw new Error(staleRequest);
    if(!response.ok){let e;try{e=await response.json();}catch{e={detail:'后台请求失败，请检查模型配置或运行状态'};}if(read&&version!==pageVersion)throw new Error(staleRequest);throw new Error(typeof e.detail==='string'?e.detail:'输入格式无效');}
    const result=options.blob?response:await response.json();
    if(read&&version!==pageVersion)throw new Error(staleRequest);return result;
  }catch(error){if(error.name==='AbortError'||read&&version!==pageVersion)throw new Error(staleRequest);throw error;}
}
function dirty(){return settingsChanged.size>0 || current && text(draft)!==text(current.value);}
function addAgentReset(holder){
  const button=document.createElement('button');button.className='quiet';button.textContent='重置全部 agent 会话';holder.append(button);
  button.onclick=async()=>{
    if(busy)return;
    if(!window.confirm('重置全部群聊和私聊的 agent 执行会话？下一轮将重新加载当前规则。聊天记录、认知、人设和后台任务保留；已执行的操作不会撤回，尚未发出的旧回复将在有效性检查时停止。未保存的提示词草稿不会生效。'))return;
    busy=true;button.disabled=true;
    try{const result=await api('/api/agent/reset',{confirm:'reset_all'});status(result.message);}
    catch(e){status(e.message,true);}
    finally{busy=false;button.disabled=false;}
  };
}
async function navigate(next){
  closeMessagePreview();
  if(busy){status('当前操作尚未完成，请稍后切换');return false;}
  if(dirty()&&!window.confirm('当前草稿还没有保存，放弃草稿并切换？'))return false;
  settingsChanged.clear();selectionVersion++;page=next;current=null;draft=null;chat=[];proposal=null;
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===page));
  status('');
  try{await renderPage();return true;}catch(e){status(e.message,true);return false;}
}
async function renderPage(){
  pageController.abort();pageController=new AbortController();const version=++pageVersion;
  showSelection();
  const container=$('#content');container.innerHTML='<div class="panel panel-body loading" role="status">正在加载…</div>';
  await Promise.all([loadNavigation('base'),...(['groups','topics','settings'].includes(page)?[loadNavigation('groups')]:page==='prompts'?[loadNavigation('prompts')]:[])]);
  if(version!==pageVersion)return;
  if(inventory.memory_upgrade_required&&['groups','memories','robot'].includes(page)){container.innerHTML='<div class="panel panel-body"><p>旧记忆尚未迁移，请先预演并完成统一记忆升级。</p><button id="go-memory-upgrade">打开数据迁移</button></div>';$('#go-memory-upgrade').onclick=()=>navigate('migration');return;}
  if(page==='topics'){await renderTopics();return;}
  if(page==='memories'||page==='robot'){await renderLibrary();return;}
  if(page==='memory-status'){await renderMemoryStatus();return;}
  if(page==='settings'){await renderSettings();return;}
  if(page==='migration'){renderMigration();return;}
  if(page==='changes'){await renderChanges();return;}
  container.innerHTML=(page==='prompts'?'<div class="preview"><details><summary>查看完整请求组装预览（不调用模型）</summary><div class="toolbar"><input id="preview-group" placeholder="群号 / 私聊负QQ" aria-label="预览会话"><input id="preview-user" placeholder="当前用户 QQ" aria-label="预览用户"><input id="preview-text" class="wide" placeholder="测试消息" value="你好"><button id="preview-run">组装预览</button></div><pre id="preview-result">选择会话和用户后，可查看 system、背景记忆、历史问答、当前消息及工具。</pre></details></div>':'')+
  '<div class="workspace"><section class="panel"><div class="panel-title"><input class="search" id="filter" placeholder="搜索 / 筛选" aria-label="筛选资源"><div class="count" id="list-count"></div></div><div id="list" class="listing"></div></section><section class="panel" id="editor"><div class="empty">从左侧选择一项，开始查看与维护</div></section><aside class="panel assistant" id="assistant"><div class="panel-title"><h3>✦ AI 辅助修改</h3><span class="badge">确认后保存</span></div><div class="panel-body"><p class="note">选中资源后，描述你希望如何修改。个人认知统一参考该人的各群和私聊记录，群背景整理参考来源群；历史按时间段与近期均衡抽样；表情调整会附原图。选中内容将发送给已配置的模型服务。</p><div id="chat" class="chat"></div><textarea id="instruction" placeholder="例如：整理这条记录的表达，保留依据和不确定之处" aria-label="修改要求" disabled></textarea><button id="suggest" disabled>生成修改建议</button><button id="review-proposal" class="quiet" hidden>查看建议差异</button></div></aside></div>';
  if(page==='groups'){$('#assistant').hidden=true;$('#content .workspace').classList.add('group-workspace');}
  let items=[];
  if(page==='groups')items=(inventory.groups||[]).map(g=>({...g,name:g.name,subtitle:g.count+' 条记录'}));
  if(page==='prompts')items=[...(inventory.personas||[]).map(p=>({...p,name:'人设 · '+p.name})),...(inventory.modules||[])];
  if(page==='emotes'){await renderEmoteList();if(version!==pageVersion)return;items=null;}
  function list(){const query=$('#filter').value.toLowerCase();const found=items.filter(i=>(i.name+' '+(i.subtitle||'')+' '+i.resource).toLowerCase().includes(query));$('#list-count').textContent=`${found.length} 项`;
    $('#list').className='listing';
    $('#list').innerHTML=found.length?found.map(i=>`<button class="list-item" data-id="${esc(i.resource)}"><strong class="${page==='groups'?'name-group':''}">${esc(i.name)}</strong><small>${esc(i.subtitle||i.resource)}</small></button>`).join(''):'<div class="empty">暂无匹配数据</div>';
    $('#list').querySelectorAll('button').forEach(b=>b.onclick=()=>selectResource(b.dataset.id));}
  if(items){$('#filter').oninput=list;list();}
  if(page==='prompts'||page==='emotes'){
    const actions=document.createElement('div');actions.className='toolbar';
    const add=document.createElement('button');add.textContent=page==='prompts'?'新增人设':'上传表情包';
    add.onclick=()=>openCreate(page==='emotes');actions.append(add);container.prepend(actions);
  }
  $('#suggest').onclick=suggest;$('#review-proposal').onclick=()=>review(proposal.value);
  if(page==='prompts')$('#preview-run').onclick=async()=>{const output=$('#preview-result');try{output.textContent='正在组装…';output.textContent=text(await api('/api/preview',{group:Number($('#preview-group').value),user:Number($('#preview-user').value),text:$('#preview-text').value}));}catch(e){if(output.isConnected){status(e.message,true);output.textContent=e.message;}}};
}
async function selectResource(id){
  if(busy)return;
  if(dirty()&&!window.confirm('放弃当前未保存草稿？'))return;
  const version=++selectionVersion;
  try{const loaded=await api('/api/resource?id='+encodeURIComponent(id));if(version!==selectionVersion)return;current=loaded;draft=structuredClone(current.value);chat=[];proposal=null;renderEditor();
    document.querySelectorAll('[data-id]').forEach(b=>b.classList.toggle('selected',b.dataset.id===id));
    document.querySelectorAll('[data-person]').forEach(el=>{el.hidden=el.dataset.person!==id;});
    if($('#instruction')){$('#instruction').disabled=false;$('#suggest').disabled=false;$('#chat').textContent='';$('#review-proposal').hidden=true;}
    const savedProposal=await api('/api/knowledge-proposal?resource='+encodeURIComponent(id));if(version!==selectionVersion)return;proposal=savedProposal||current.proposal||null;if(proposal){chat=[{role:'assistant',content:JSON.stringify(proposal)}];showChat();$('#review-proposal').hidden=false;}status(proposal?'已载入修改建议，查看差异后确认保存':'已载入保存版本');
  }catch(e){if(version===selectionVersion)status(e.message,true);}
}
function showSelection(){
  const el=$('#selection');
  el.hidden=page==='topics';
  if(!current){el.textContent=page==='settings'?'当前选择：设置':'尚未选择资源';el.removeAttribute('title');return;}
  const [kind]=current.resource.split(':');
  const names={memory:'记忆库',person:'人物档案',group:'群管理',emote:'表情包管理',persona:'人设',module:'能力模块'};
  let name=current.title||current.resource.substring(current.resource.indexOf(':')+1),scope='';
  if(kind==='person'){
    const person=(inventory.people||[]).find(p=>p.resource===current.resource);
    name=person?.name||name;
    scope='统一档案';
  }else if(kind==='group')name=(inventory.groups||[]).find(g=>g.resource===current.resource)?.name||name;
  el.textContent='当前选择：'+[names[kind]||kind,name,scope].filter(Boolean).join(' / ');
  el.title=current.resource;
}
async function saveManual(){
  if(busy||!current)return;
  if(!dirty()){status('内容没有变化');return;}
  review(draft);
}
// 人数滑块在全局设置和群覆盖中共用；空值表示继续跟随默认值。
function participantControl(parent,title,value,onchange,defaultValue){
  const field=document.createElement('div');field.className='field participants-field';
  const label=document.createElement('label'),heading=document.createElement('span'),amount=document.createElement('output');
  heading.textContent=title;
  const describe=value=>Number(value)===10?'10+（不主动回复）':value+' 人';
  const slider=document.createElement('input');slider.type='range';slider.min='1';slider.max='10';slider.step='1';slider.value=value??defaultValue;slider.setAttribute('aria-label',title);
  label.append(heading,amount,slider);field.append(label);
  let inherit=null,disabled=false;
  function refresh(){amount.textContent=describe(slider.value);slider.setAttribute('aria-valuetext',describe(slider.value));slider.disabled=disabled||Boolean(inherit?.checked);if(inherit)inherit.disabled=disabled;}
  function selected(){return inherit?.checked?null:Number(slider.value);}
  if(defaultValue!==undefined){
    const follow=document.createElement('label');follow.className='participants-inherit';
    inherit=document.createElement('input');inherit.type='checkbox';inherit.checked=value===null;inherit.setAttribute('aria-label','参与人数跟随默认');
    follow.append(inherit,document.createTextNode('跟随默认：'+describe(defaultValue)));field.append(follow);
    inherit.onchange=()=>{if(inherit.checked)slider.value=defaultValue;refresh();onchange(selected());};
  }
  const note=document.createElement('small');note.className='note';note.textContent='1～9 按不同群成员计数，达标后每批新消息均评估；10+ 不主动回复，直接 @仍可回应。';field.append(note);
  slider.oninput=()=>{refresh();onchange(selected());};parent.append(field);refresh();
  return {value:selected,setDisabled:value=>{disabled=value;refresh();}};
}
function renderEditor(){
  closeMessagePreview();
  $('#editor').oninput=null;settingsChanged.delete('memory-create');
  const kind=current.resource.split(':')[0];
  const title=current.title||current.resource.substring(current.resource.indexOf(':')+1);
  showSelection();
  $('#editor').innerHTML=`<div class="panel-body"><div id="fields"></div><div id="evidence"></div><div class="actions">${current.default!==undefined?'<button class="quiet" id="reset-default">载入默认值</button>':''}<button id="save-manual">保存</button></div><p class="note">手动修改与模型建议均在确认差异后保存；修改正文会固定该记忆。</p></div>`;
  if(kind==='emote')$('#fields').innerHTML=`<img class="image-preview" src="/api/image?name=${encodeURIComponent(title)}" alt="当前表情">`;
  function showKnowledge(label,input,display,parts){
    const preview=document.createElement('div');preview.className='knowledge-display';showNameParts(preview,parts,display);
    const edit=document.createElement('details');const summary=document.createElement('summary');summary.textContent='编辑原文';
    edit.append(summary,input);label.append(preview,edit);
    input.addEventListener('input',()=>{preview.textContent='原文已修改，保存后按当前昵称显示';});
  }
  function fieldName(key,path){
    if(kind!=='module')return labels[key]||key;
    if(!path.length)return {prompts:'上下文正文',tools:'工具定义'}[key]||key;
    if(path[0]==='prompts')return current.metadata.prompts[key]?.title||key;
    return {description:'用途与调用规则',parameters:'参数说明'}[key]||key;
  }
  if(kind==='module'){
    const placement=document.createElement('p');placement.className='note';placement.textContent='注入位置：'+current.metadata.placement;$('#fields').append(placement);
  }
  function fields(data, parent, path=[]){
    const entries=typeof data==='string'?[['__text',data]]:Object.entries(data);
    for(const [key,value] of entries){
      if(key==='min_participants'){
        participantControl(parent,'接话所需参与人数',value,v=>{draft.min_participants=v;status('草稿尚未保存');},current.default_min_participants);
        continue;
      }
      if(value&&typeof value==='object'&&!Array.isArray(value)){
        const section=document.createElement('details');section.open=path.length===0;
        const title=document.createElement('summary');title.textContent=path[0]==='会话'?(Number(key)>0?'群 '+key:'私聊'):fieldName(key,path);
        section.append(title);parent.append(section);fields(value,section,[...path,key]);continue;
      }
      const label=document.createElement('label');label.className='field';const span=document.createElement('span');span.textContent=key==='__text'?'提示词正文':fieldName(key,path);label.append(span);
      if(kind==='module'&&path[0]==='prompts'){
        const note=document.createElement('small');const meta=current.metadata.prompts[key];note.textContent=meta.placement+(meta.placeholders.length?'；保留占位符：'+meta.placeholders.map(v=>'{'+v+'}').join('、'):'');label.append(note);
      }
      if(kind==='module'&&path[0]==='tools'&&path[2]==='parameters'){
        let schema=current.metadata.tools[path[1]].parameters,required=false;
        for(const segment of key.split('.')){
          const name=segment.replace(/\[\]$/,'');required=(schema.required||[]).includes(name);schema=schema.properties?.[name];
          if(segment.endsWith('[]'))schema=schema?.items;
          if(!schema)break;
        }
        if(schema){const note=document.createElement('small');note.textContent=[required?'必填':'可选','类型：'+JSON.stringify(schema.type),schema.enum?'可选值：'+schema.enum.join(' / '):'',schema.minimum!==undefined?'最小值：'+schema.minimum:'',schema.maximum!==undefined?'最大值：'+schema.maximum:''].filter(Boolean).join('；');label.append(note);}
      }
      let input;
      if(kind==='memory'&&key==='fixed'){input=document.createElement('input');input.type='checkbox';input.checked=value;}
      else if(kind==='memory'&&key==='state'){input=document.createElement('select');for(const [key,title] of Object.entries(memoryStates)){const o=document.createElement('option');o.value=key;o.textContent=title;input.append(o);}input.value=value;}
      else{input=document.createElement('textarea');input.value=Array.isArray(value)?value.join('\n'):value;if(key==='__text')input.className='long';}
      input.oninput=()=>{let v=key==='fixed'?input.checked:input.value;if(key==='interests')v=v.split('\n').map(s=>s.trim()).filter(Boolean);if(key==='__text')draft=v;else{let target=draft;for(const part of path)target=target[part];target[key]=v;}status('草稿尚未保存');};if(kind==='group'&&['summary','knowledge'].includes(key))showKnowledge(label,input,current.display_value?.[key]??value,current.display_parts?.[key]);else label.append(input);parent.append(label);
    }
  }
  fields(draft,$('#fields'));
  if(kind==='memory'){
    const info=document.createElement('p');info.className='note';info.textContent=`${memoryKinds[current.kind]} · 主体 QQ ${current.subject||'无'} · 所有群聊和私聊可召回`;
    $('#fields').prepend(info);
    if(current.history?.length){const history=document.createElement('details'),title=document.createElement('summary'),body=document.createElement('pre');title.textContent='查看历史版本';body.textContent=text(current.history);history.append(title,body);$('#fields').append(history);}
  }
  if(kind==='module'){
    const details=document.createElement('details'),summary=document.createElement('summary'),body=document.createElement('pre');summary.textContent='完整工具契约（结构由程序维护）';
    const definitions=structuredClone(current.metadata.tools);
    function apply(schema,descriptions,prefix=''){
      if(Object.hasOwn(descriptions,prefix))schema.description=descriptions[prefix];
      for(const [name,child] of Object.entries(schema.properties||{}))apply(child,descriptions,prefix?prefix+'.'+name:name);
      if(schema.items)apply(schema.items,descriptions,prefix+'[]');
    }
    function refreshContract(){for(const [name,tool] of Object.entries(definitions)){tool.description=draft.tools[name].description;apply(tool.parameters,draft.tools[name].parameters);}body.textContent=JSON.stringify(definitions,null,2);}
    refreshContract();$('#fields').addEventListener('input',refreshContract);details.append(summary,body);$('#fields').append(details);
  }
  if(kind==='emote'){
    const section=document.createElement('section');
    const heading=document.createElement('h3');heading.textContent='统一表情经验';section.append(heading);
    for(const row of current.experiences||[]){
      const button=document.createElement('button');button.className='list-item';
      button.textContent=(row.subject?'用户 '+row.subject:'全局')+' · '+row.value;
      button.onclick=async()=>{if(await navigate('memories'))await selectResource(row.resource);};section.append(button);
    }
    if(!(current.experiences||[]).length){const empty=document.createElement('p');empty.textContent='尚无有依据的反馈；没有反馈不会自动当作赞同。';section.append(empty);}
    $('#fields').append(section);
  }
  if(kind==='persona')renderProfileLab();
  if(kind==='person'){
    const rebuild=document.createElement('button');rebuild.className='quiet';rebuild.textContent='根据聊天重新整理';
    rebuild.onclick=()=>{if(busy)return;$('#instruction').value='根据提供的各群和私聊记录整理统一档案，保留全部字段，合并重复信息，注明来源语境与不确定之处。';suggest();};$('#editor .actions').append(rebuild);
  }
  if(kind==='group'){
    const holder=document.createElement('section');holder.className='plugin-manager';$('#editor .panel-body').append(holder);
    const [,bot,group]=current.resource.split(':');renderPlugins(holder,Number(bot),Number(group)).catch(e=>status(e.message,true));
  }
  if(current.placeholders?.length){const p=document.createElement('p');p.className='note';p.textContent='请保留动态占位符：'+current.placeholders.map(s=>'{'+s+'}').join('、');$('#fields').append(p);}
  const evidence=current.display_evidence||current.evidence;
  if(evidence?.length)$('#evidence').innerHTML=`<details><summary>查看认知依据与来源</summary><pre>${esc(text(evidence))}</pre></details>`;
  $('#save-manual').onclick=saveManual;
  if($('#reset-default'))$('#reset-default').onclick=()=>{draft=current.default;renderEditor();status('默认值已载入草稿，点击保存后生效');};
  if(kind==='persona'&&title!=='默认'){
    const remove=document.createElement('button');remove.className='quiet';remove.textContent='删除';
    remove.onclick=()=>{if(!busy)review(null,true);};$('#editor .actions').append(remove);
  }
}
function renderProfileLab(){
  const panel=document.createElement('section');panel.className='profile-lab';
  const controls=document.createElement('div');controls.className='toolbar';
  const check=document.createElement('button');check.className='quiet';check.textContent='检查人设草稿';
  const scenario=document.createElement('select');scenario.setAttribute('aria-label','固定试聊场景');
  for(const [id,title] of [['chat','普通闲聊'],['help','认真求助'],['joke','接梗'],['correct','接受纠正'],['end','结束话题']]){
    const option=document.createElement('option');option.value=id;option.textContent=title;scenario.append(option);
  }
  const trial=document.createElement('button');trial.className='quiet';trial.textContent='对比试聊（调用模型）';
  controls.append(check,scenario,trial);
  const note=document.createElement('p');note.className='note';note.textContent='比较已保存人设与当前草稿。每次两轮生成，仅使用固定虚构场景；不发QQ、不写记忆、不执行工具。结果有随机性，不是评分。';
  const result=document.createElement('div');result.setAttribute('aria-live','polite');
  panel.append(controls,note,result);$('#editor .panel-body').append(panel);
  async function run(action){
    if(busy)return;
    const resource=current.resource,revision=current.revision,value=structuredClone(draft),version=selectionVersion;
    busy=true;check.disabled=trial.disabled=scenario.disabled=true;result.textContent=action==='check'?'正在检查…':'正在生成两组回答…';
    try{
      const data=await api('/api/profile-lab',{resource,revision,value,action,scenario:scenario.value});
      if(!panel.isConnected||version!==selectionVersion||current?.resource!==resource)return;
      result.textContent='';
      if(action==='check'){
        const list=document.createElement('ul');for(const warning of data.warnings){const li=document.createElement('li');li.textContent=warning;list.append(li);}
        const summary=document.createElement('p');summary.textContent=data.warnings.length?data.note:'未发现规则提示。'+data.note;result.append(summary,list);
      }else{
        const input=document.createElement('p');input.textContent='场景：'+data.scenario.text;result.append(input);
        const columns=document.createElement('div');columns.className='trial-columns';
        for(const [key,label] of [['saved','已保存人设'],['draft','草稿人设']]){
          const column=document.createElement('section'),title=document.createElement('strong');title.textContent=label;column.append(title);
          for(const message of data.results[key]){const bubble=document.createElement('p');bubble.className='trial-bubble';bubble.textContent=message;column.append(bubble);}
          columns.append(column);
        }result.append(columns);
      }
      if(JSON.stringify(draft)!==JSON.stringify(value)){const stale=document.createElement('p');stale.textContent='草稿已继续修改，以上结果对应点击时的版本。';result.append(stale);}
    }catch(error){if(panel.isConnected)result.textContent=error.message;}
    finally{busy=false;check.disabled=trial.disabled=scenario.disabled=false;}
  }
  check.onclick=()=>run('check');trial.onclick=()=>run('trial');
}
async function refreshResources(id){
  current=null;draft=null;proposal=null;chat=[];
  invalidateNavigation();await loadNavigation('base');await renderPage();
  if(id){current=await api('/api/resource?id='+encodeURIComponent(id));draft=structuredClone(current.value);renderEditor();
    document.querySelectorAll('[data-person]').forEach(el=>{el.hidden=el.dataset.person!==id;});
    document.querySelectorAll('[data-id]').forEach(b=>b.classList.toggle('selected',b.dataset.id===id));
    if($('#instruction')){$('#instruction').disabled=false;$('#suggest').disabled=false;}}
}
function openCreate(emote){
  if(busy)return;
  if(dirty()&&!window.confirm('放弃当前未保存草稿，新增资源？'))return;
  const dialog=document.createElement('dialog');let previewUrl='';
  dialog.innerHTML=emote?
    '<h2>上传表情包</h2><label class="field"><span>图片文件（PNG、JPEG、GIF、WebP）</span><input id="new-file" type="file" accept=".png,.jpg,.jpeg,.gif,.webp"></label><label class="field"><span>保存文件名（含扩展名）</span><input id="new-name" maxlength="100"></label><img id="new-preview" class="image-preview" alt="待上传表情" hidden><p class="note">确认后加入图库；同名文件不会覆盖。上传后可编辑表情说明。</p>':
    '<h2>新增人设</h2><label class="field"><span>名称（不含 .txt）</span><input id="new-name" maxlength="100"></label><label class="field"><span>提示词正文（1 至 12000 字）</span><textarea id="new-value" maxlength="12000" class="long"></textarea></label><p class="note">确认后新增，不覆盖已有内容；在群设定中切换后使用。</p>';
  dialog.insertAdjacentHTML('beforeend','<p id="new-error" class="error" role="status"></p><div class="actions"><button id="new-cancel" class="quiet">取消</button><button id="new-save">确认并新增</button></div>');
  document.body.append(dialog);dialog.showModal();
  dialog.addEventListener('close',()=>{if(previewUrl)URL.revokeObjectURL(previewUrl);dialog.remove();});
  dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  $('#new-cancel').onclick=()=>dialog.close();
  if(emote)$('#new-file').onchange=()=>{
    if(previewUrl)URL.revokeObjectURL(previewUrl);
    const file=$('#new-file').files[0];$('#new-preview').hidden=!file;
    if(file){$('#new-name').value=file.name;previewUrl=URL.createObjectURL(file);$('#new-preview').src=previewUrl;}
  };
  $('#new-save').onclick=async()=>{
    const name=$('#new-name').value.trim(),file=emote?$('#new-file').files[0]:null;
    if(!name||(emote&&!file)||(!emote&&!$('#new-value').value.trim())){$('#new-error').textContent='请填写名称并选择图片或填写正文';return;}
    busy=true;$('#new-save').disabled=true;$('#new-cancel').disabled=true;
    try{
      const result=emote?await api('/api/emotes/upload?name='+encodeURIComponent(name),undefined,{method:'POST',body:file}):
        await api('/api/profiles',{kind:'persona',name,value:$('#new-value').value});
      dialog.close();await refreshResources(result.resource);status('已新增，可继续维护；后续请求实时读取');
    }catch(e){if(dialog.isConnected)$('#new-error').textContent=e.message;else status(e.message,true);}
    finally{busy=false;if(dialog.isConnected){$('#new-save').disabled=false;$('#new-cancel').disabled=false;}}
  };
}
function review(value, remove=false){
  if(busy||!current)return;
  if(!remove&&current.resource.startsWith('memory:')&&value.content!==current.value.content)value={...value,fixed:true};
  deleting=remove;pending=structuredClone(value);
  $('#confirm .diff').hidden=remove;
  $('#confirm h2').textContent=remove?'确认删除 '+current.resource:'确认这次修改';
  $('#confirm p').textContent=remove?'原文件会备份，可在操作记录中撤销。未保存草稿将丢弃。':'只修改当前选中的资源。保存前会保留原值。';
  $('#apply-confirm').textContent=remove?'确认删除':'确认并保存';
  const displayBefore=current.display_value??current.value;
  const displayAfter=proposal&&text(proposal.value)===text(value)?proposal.display_value??value:value;
  let before=displayBefore,after=displayAfter;
  $('#before').textContent=text(before);$('#after').textContent=remove?'删除所选资源':text(after);$('#confirm').showModal();
}
$('#cancel-confirm').onclick=()=>$('#confirm').close();
$('#apply-confirm').onclick=async()=>{
  const b=$('#apply-confirm');b.disabled=true;busy=true;$('#cancel-confirm').disabled=true;
  try{
    if(deleting){const result=await api('/api/resource/delete',{resource:current.resource,revision:current.revision});$('#confirm').close();await refreshResources();status(result.message);}
    else{current=await api('/api/resource',{resource:current.resource,value:pending,revision:current.revision});draft=structuredClone(current.value);proposal=null;renderEditor();if($('#review-proposal'))$('#review-proposal').hidden=true;$('#confirm').close();await refreshResources(current.resource);status('已保存，后续请求使用新内容；原值已保留');}
  }catch(e){status(e.message,true);$('#confirm').close();}finally{busy=false;b.disabled=false;$('#cancel-confirm').disabled=false;}
};
$('#confirm').addEventListener('cancel',e=>{if(busy)e.preventDefault();});
async function suggest(){
  if(busy)return;
  const instruction=$('#instruction').value.trim();if(!instruction||!current)return;
  if(dirty()){status('请先保存或放弃手动草稿，再让模型基于保存版本提出建议',true);return;}
  busy=true;$('#suggest').disabled=true;chat.push({role:'user',content:instruction});chat=chat.slice(-14);$('#instruction').value='';showChat();status('模型正在提出修改建议，尚未保存…');
  try{proposal=await api('/api/suggest',{resource:current.resource,revision:current.revision,chat,scope:null});chat.push({role:'assistant',content:JSON.stringify(proposal)});showChat();$('#review-proposal').hidden=false;status('建议已生成，查看前后差异后确认保存');}catch(e){status(e.message,true);}finally{busy=false;$('#suggest').disabled=false;}
}
function showChat(){$('#chat').innerHTML=chat.map(m=>{let content=m.content;if(m.role==='assistant'){try{const parsed=JSON.parse(content);content=parsed.display_explanation??parsed.explanation;}catch{}}return `<p class="${m.role==='user'?'user':''}">${esc(content)}</p>`;}).join('');$('#chat').scrollTop=$('#chat').scrollHeight;}
function renderMigration(){
  $('#content').innerHTML='<div class="two-columns"><section class="panel"><span class="badge">全部数据</span><h2>导出备份</h2><p>聊天与认知、群摘要、表情包、人设、提示词覆盖和 .env 配置，保存在一个普通 ZIP 中。</p><p class="note">备份含明文密钥。数据库快照包含 WAL 中已提交的记录；要求严格一致时，请先停止机器人。</p><button id="export">生成并下载备份 ↓</button></section><section class="panel"><span class="badge">同号合并</span><h2>导入数据</h2><p>先停止机器人进程，后台可以保持打开。导入前自动备份；不同 QQ 拒绝导入。</p><label class="field"><span>目标机器人 QQ（新部署必填）</span><input id="import-qq" inputmode="numeric" placeholder="目标账号"></label><label class="field"><span>迁移 ZIP（最大 512 MiB）</span><input id="import-file" type="file" accept=".zip"></label><button id="import">检查并合并</button><p class="note">已有配置优先，来源缺项补齐。失败回滚，命令行 recover 可恢复中断的导入。</p></section></div>';
  const upgrade=document.createElement('section');upgrade.className='panel panel-body';upgrade.innerHTML='<div class="actions"><button id="memory-preview">预演统一记忆升级</button><button id="memory-apply" disabled>确认执行升级</button></div><pre id="memory-report">先预演查看转换数量与待核对项目。执行前须停止机器人；会先备份，失败可恢复。</pre>';$('#content').prepend(upgrade);
  $('#memory-preview').onclick=async()=>{try{$('#memory-report').textContent=text(await api('/api/memory-upgrade'));$('#memory-apply').disabled=false;}catch(e){status(e.message,true);}};
  $('#memory-apply').onclick=async()=>{if(!window.confirm('按当前预演执行统一记忆迁移？机器人须已停止，原数据将先备份。'))return;const button=$('#memory-apply');button.disabled=true;try{$('#memory-report').textContent=text(await api('/api/memory-upgrade',{confirm:'upgrade_memory'}));invalidateNavigation();await loadNavigation('base');status('统一记忆升级已完成');}catch(e){status(e.message,true);}};
  $('#export').onclick=async()=>{const b=$('#export');b.disabled=true;status('正在生成备份…');try{const response=await api('/api/export',{}, {blob:true});const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='qqbot-backup.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);status('备份已生成，浏览器已开始下载；服务器 backups 目录也保留一份');}catch(e){status(e.message,true);}finally{b.disabled=false;}};
  $('#import').onclick=async()=>{const file=$('#import-file').files[0];if(!file){status('请选择 ZIP 文件',true);return;}if(!window.confirm('确认目标机器人已停止，开始同号合并？已有数据会先自动备份。'))return;const b=$('#import');b.disabled=true;status('正在校验并合并…');try{const q=$('#import-qq').value;const result=await api('/api/import'+(q?'?bot_qq='+encodeURIComponent(q):''),undefined,{method:'POST',body:file});invalidateNavigation();await loadNavigation('base');status(result.message+'；原数据备份：'+result.backup);}catch(e){status(e.message,true);}finally{b.disabled=false;}};
}
async function renderChanges(){
  const container=$('#content');container.innerHTML='<section class="panel panel-body"><div id="change-list"></div><button id="change-more" class="quiet">加载更多</button></section>';
  const list=$('#change-list'),more=$('#change-more'),seen=new Set();let offset=0;
  async function load(){
    more.disabled=true;try{
      const data=await api('/api/changes?'+new URLSearchParams({offset,limit:20}));if(!list.isConnected)return;
      for(const item of data.items){
        if(seen.has(item.id))continue;seen.add(item.id);
        const row=document.createElement('article');row.className='memory-activity';
        const title=document.createElement('strong');title.textContent=item.resource;
        const time=document.createElement('small');time.className='note activity-meta';time.textContent=dateText(item.created);
        const detail=document.createElement('details'),summary=document.createElement('summary');summary.textContent='查看前后内容';detail.append(summary);let loaded=false;
        detail.ontoggle=async()=>{if(!detail.open||loaded)return;loaded=true;while(detail.children.length>1)detail.lastElementChild.remove();const body=document.createElement('div');body.textContent='正在读取…';detail.append(body);try{const result=await api('/api/changes/'+encodeURIComponent(item.id));if(!detail.isConnected)return;body.className='diff';body.innerHTML='<div><h4>修改前</h4><pre>'+esc(text(result.before))+'</pre></div><div><h4>修改后</h4><pre>'+esc(text(result.after))+'</pre></div>';}catch(e){loaded=false;if(detail.isConnected)body.textContent=e.message;}};
        const undo=document.createElement('button');undo.className='quiet';undo.textContent=item.retired?'旧条目仅供查阅':'撤销此修改';undo.disabled=item.status!=='saved'||item.retired;
        undo.onclick=async()=>{if(!window.confirm('恢复这条记录的修改前内容？后续已修改的资源会拒绝撤销。'))return;busy=true;try{await api('/api/undo',{id:item.id});invalidateNavigation();await renderChanges();status('已撤销，并记录本次恢复');}catch(e){status(e.message,true);}finally{busy=false;}};
        row.append(title,time,detail,undo);list.append(row);
      }
      offset+=data.items.length;more.hidden=!data.has_more;if(!offset)list.innerHTML='<div class="empty">尚无后台操作记录</div>';
    }catch(e){status(e.message,true);}finally{more.disabled=false;}
  }
  more.onclick=load;await load();
}
async function renderEmoteList(){
  const list=$('#list'),filter=$('#filter'),count=$('#list-count');list.className='tiles';
  const more=document.createElement('button');more.className='quiet';more.textContent='加载更多表情';list.after(more);
  let offset=0,sequence=0,timer;const seen=new Set();
  async function load(reset){
    if(!list.isConnected)return;const request=++sequence;if(reset){offset=0;seen.clear();list.replaceChildren();}more.disabled=true;
    try{const data=await api('/api/emotes?'+new URLSearchParams({query:filter.value,offset,limit:40}));if(request!==sequence||!list.isConnected)return;
      for(const item of data.items){if(seen.has(item.resource))continue;seen.add(item.resource);const button=document.createElement('button');button.className='tile';button.dataset.id=item.resource;button.title=item.name;button.innerHTML='<img loading="lazy" src="/api/image?name='+encodeURIComponent(item.name)+'" alt="'+esc(item.name)+'"><small>'+esc(item.name)+'</small>';button.onclick=()=>selectResource(item.resource);list.append(button);}
      offset+=data.items.length;count.textContent=data.total+' 项';more.hidden=!data.has_more;if(!offset)list.innerHTML='<div class="empty">暂无匹配数据</div>';
    }catch(e){if(request===sequence)status(e.message,true);}finally{if(request===sequence)more.disabled=false;}
  }
  filter.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>load(true),250);};more.onclick=()=>load(false);await load(true);
}
$('#nav').querySelectorAll('button').forEach(b=>b.onclick=()=>navigate(b.dataset.page));
$('#refresh').onclick=async()=>{if(busy)return;try{invalidateNavigation();await navigate(page);}catch(e){status(e.message,true);}};
window.addEventListener('beforeunload',e=>{if(dirty()||busy){e.preventDefault();e.returnValue='';}});
(async()=>{try{token=(await api('/api/session',undefined,{detached:true})).token;await navigate('memories');}catch(e){status(e.message,true);}})();

async function refreshRuntime(){
  try{
    const data=await api('/api/runtime',undefined,{detached:true});
    const states={connected:'运行中 · QQ 接入已连接',waiting:'运行中 · 等待 QQ 连接',stopped:'机器人已停止',stale:'机器人心跳中断，可能已停止'};
    const duration=data.uptime===null?'':` · 已运行 ${Math.floor(data.uptime/3600)}小时${Math.floor(data.uptime%3600/60)}分钟`;
    const el=$('#runtime-status');el.dataset.state=data.state;el.textContent=(states[data.state]||'状态未知')+duration;
    $('#bot-name').textContent=data.nickname;$('#bot-name').title=data.qq?'QQ '+data.qq:'';
    const avatar=$('#bot-avatar'),fallback=$('#bot-fallback');
    const url=data.qq?`/api/bot-avatar?v=${Math.floor(Date.now()/300000)}&qq=${encodeURIComponent(data.qq)}`:'';
    if(avatar.dataset.url!==url){
      avatar.dataset.url=url;avatar.hidden=true;fallback.hidden=false;
      avatar.onload=()=>{avatar.hidden=false;fallback.hidden=true;};
      avatar.onerror=()=>{avatar.hidden=true;fallback.hidden=false;};
      if(url)avatar.src=url;else avatar.removeAttribute('src');
    }
  }catch{const el=$('#runtime-status');el.dataset.state='unknown';el.textContent='无法读取运行状态，请检查后台连接';}
}
refreshRuntime();
setInterval(refreshRuntime,5000);

async function renderTopics(){
  const version=selectionVersion;
  $('#content').innerHTML='<div class="toolbar topic-filters"><select id="topic-group" aria-label="筛选群"><option value="">全部群</option></select><input id="topic-query" placeholder="搜索话题" aria-label="搜索话题"><button id="topic-search">查询</button></div><div class="topic-layout"><section class="panel topic-list"><div id="topic-list" class="listing"></div><button id="topic-more" class="quiet" hidden>加载更多话题</button></section><section class="panel topic-detail" id="topic-detail"><div class="empty">选择话题查看聊天与回复原因</div></section></div>';
  for(const group of inventory.groups||[]){const option=document.createElement('option');option.value=group.bot+':'+group.grp;option.textContent=group.name;$('#topic-group').append(option);}
  let offset=0,loading=false,detailVersion=0;
  function timestamp(value){return new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});}
  async function load(reset){
    if(loading)return;loading=true;
    const controls=[$('#topic-search'),$('#topic-group'),$('#topic-query'),$('#topic-more')];controls.forEach(c=>c.disabled=true);
    if(reset){offset=0;detailVersion++;$('#topic-list').replaceChildren();$('#topic-detail').innerHTML='<div class="empty">选择话题查看聊天与回复原因</div>';}
    const params=new URLSearchParams({query:$('#topic-query').value.trim(),offset});
    const scope=$('#topic-group').value;if(scope){const [bot,group]=scope.split(':');params.set('bot',bot);params.set('group',group);}
    try{
      const data=await api('/api/topics?'+params);if(version!==selectionVersion)return;
      for(const topic of data.items){
        const button=document.createElement('button');button.className='list-item';
        const title=document.createElement('strong');title.textContent=topic.title;
        const meta=document.createElement('small');meta.textContent=topic.group_name+' · '+topic.replies+' 轮回复 · '+timestamp(topic.created);
        button.append(title,meta);button.onclick=()=>openTopic(topic,button);$('#topic-list').append(button);
      }
      offset+=data.items.length;$('#topic-more').hidden=!data.more;
      if(!offset)$('#topic-list').innerHTML='<div class="empty">暂无已回复的话题</div>';
    }catch(e){if(version===selectionVersion)status(e.message,true);}finally{loading=false;controls.forEach(c=>c.disabled=false);}
  }
  async function openTopic(topic,button){
    const sequence=++detailVersion;let messageOffset=0;
    document.querySelectorAll('#topic-list button').forEach(b=>b.classList.toggle('selected',b===button));
    const detail=$('#topic-detail');detail.replaceChildren();
    const meta=document.createElement('div');meta.className='topic-meta';meta.textContent=topic.title+' · 起始消息 '+topic.start+' · 机器人 '+topic.bot;
    const chat=document.createElement('div');chat.className='topic-chat';
    const more=document.createElement('button');more.className='quiet';more.textContent='加载后续聊天';more.hidden=true;
    detail.append(meta,chat,more);
    async function messages(){
      more.disabled=true;
      try{
        const params=new URLSearchParams({bot:topic.bot,group:topic.grp,start:topic.start,offset:messageOffset});
        const data=await api('/api/topics/messages?'+params);
        if(version!==selectionVersion||sequence!==detailVersion)return;
        for(const message of data.messages){
          if(message.is_start){const marker=document.createElement('div');marker.className='topic-start';marker.textContent='话题开始 · 消息 '+message.message_id;chat.append(marker);}
          const row=document.createElement('article');row.className='message-preview-row'+(message.is_self?' self':'');
          const avatar=document.createElement('div');avatar.className='message-preview-avatar';avatar.textContent=Array.from(message.name||'?')[0];
          const column=document.createElement('div');column.className='message-preview-column';
          const info=document.createElement('div');info.className='message-preview-meta';
          const name=document.createElement('span');name.className='name-person';name.textContent=message.name+(message.is_self?'（机器人）':'');
          const time=document.createElement('span');time.textContent=timestamp(message.time);info.append(name,time);
          column.append(info);
          if(message.reply_to){const quote=document.createElement('div');quote.className='topic-quote';quote.textContent='引用 '+(message.reply_to.sender_name||message.reply_to.sender||'消息')+'：'+(message.reply_to.text||'消息 '+message.reply_to.message_id);column.append(quote);}
          const bubble=document.createElement('div');bubble.className='message-preview-bubble';bubble.textContent=message.text||'（无文字内容）';column.append(bubble);
          const reasons=document.createElement('details');reasons.className='topic-reasons';
          const summary=document.createElement('summary');summary.textContent='话题判断'+(message.decisions.length?'与回复原因':'');
          const judgement=document.createElement('p');judgement.textContent=message.judgement;reasons.append(summary,judgement);
          if(message.response_to_bot!==null){const target=document.createElement('p');target.textContent='回应机器人消息：'+message.response_to_bot;reasons.append(target);}
          for(const decision of message.decisions){const reason=document.createElement('p');reason.textContent=decision.outcome+'：'+decision.reason;reasons.append(reason);if(decision.outcome==='已回复')reasons.open=true;}
          column.append(reasons);row.append(avatar,column);chat.append(row);
        }
        messageOffset+=data.messages.length;more.hidden=!data.more;
      }catch(e){if(version===selectionVersion&&sequence===detailVersion)status(e.message,true);}finally{more.disabled=false;}
    }
    more.onclick=messages;await messages();
  }
  $('#topic-search').onclick=()=>load(true);$('#topic-group').onchange=()=>load(true);
  $('#topic-query').onkeydown=e=>{if(e.key==='Enter')load(true);};$('#topic-more').onclick=()=>load(false);
  await load(true);
}

async function renderSettings(){
  const version=selectionVersion;
  const [config,defaults,plugins]=await Promise.all([api('/api/settings'),api('/api/defaults'),api('/api/plugins')]);
  if(version!==selectionVersion)return;
  $('#content').innerHTML='<div class="settings-layout"><section class="panel panel-body"><div id="default-settings"></div><button id="save-defaults">保存默认设置</button></section><section class="panel panel-body"><p class="note">以下运行配置保存后需重启机器人。深度思考需所选型号支持；OpenAI 使用 high / none 档位，不支持的型号会由服务拒绝，不自动换型号。密钥留空保留原值。</p><div id="env-settings"></div><button id="save-settings">保存运行配置</button></section><section class="panel panel-body" id="global-plugins"></section></div>';
  function control(parent,label,value,type,options){
    const wrap=document.createElement('label');wrap.className='field';const title=document.createElement('span');title.textContent=label;wrap.append(title);
    const input=document.createElement(options?'select':'input');
    if(options)for(const item of options){const option=document.createElement('option');option.value=item;option.textContent=item;input.append(option);}
    else input.type=type;
    if(type==='checkbox')input.checked=value;else input.value=value;
    wrap.append(input);parent.append(wrap);return input;
  }
  const area=$('#default-settings');
  addAgentReset(area.parentElement);
  const participants=participantControl(area,'默认接话参与人数',defaults.min_participants,()=>settingsChanged.add('defaults'));
  const persona=control(area,'统一人设',defaults.persona,'select',defaults.personas);
  persona.onchange=()=>settingsChanged.add('defaults');
  const interestLabel=document.createElement('label');interestLabel.className='field';interestLabel.textContent='机器人自身兴趣（所有对话共用，每行一项）';
  const interests=document.createElement('textarea');interests.value=(defaults.interests||[]).join('\n');interests.oninput=()=>settingsChanged.add('defaults');interestLabel.append(interests);area.append(interestLabel);
  $('#save-defaults').onclick=async()=>{if(busy)return;busy=true;persona.disabled=true;participants.setDisabled(true);try{const saved=await api('/api/defaults',{persona:persona.value,min_participants:participants.value(),revision:defaults.revision,interests:interests.value.split('\n').map(v=>v.trim()).filter(Boolean)});defaults.revision=saved.revision;settingsChanged.delete('defaults');status(saved.message);}catch(e){status(e.message,true);}finally{busy=false;persona.disabled=false;participants.setDisabled(false);}};
  const inputs={},keys={};
  for(const group of config.groups){
    const section=document.createElement('fieldset');const legend=document.createElement('legend');legend.textContent=group.name;section.append(legend);$('#env-settings').append(section);
    for(const field of group.fields){
      const type=field.type==='boolean'?'checkbox':['integer','number'].includes(field.type)?'number':'text';
      const input=control(section,field.label,field.value,type,field.options);
      if(type==='number'){input.min=field.min;input.step=field.type==='integer'?'1':'any';if(field.max!==null)input.max=field.max;}
      input.oninput=()=>settingsChanged.add('env');inputs[field.key]={input,type};
    }
  }
  const secrets=document.createElement('fieldset');const legend=document.createElement('legend');legend.textContent='密钥替换';secrets.append(legend);$('#env-settings').append(secrets);
  for(const field of config.secrets){const input=control(secrets,field.label+(field.configured?'（已配置）':'（未配置）'),'','password');input.autocomplete='new-password';input.placeholder='留空保留原值';input.oninput=()=>settingsChanged.add('env');keys[field.key]=input;}
  $('#save-settings').onclick=async()=>{
    if(busy)return;busy=true;const button=$('#save-settings');button.disabled=true;const controls=[...Object.values(inputs).map(x=>x.input),...Object.values(keys)];controls.forEach(x=>x.disabled=true);
    try{
      const values={},secretValues={};for(const [key,{input,type}] of Object.entries(inputs))values[key]=type==='checkbox'?input.checked:type==='number'?Number(input.value):input.value;
      for(const [key,input] of Object.entries(keys))if(input.value)secretValues[key]=input.value;
      const result=await api('/api/settings',{values,secrets:secretValues,revision:config.revision});config.revision=result.revision;
      for(const field of result.secrets){const input=keys[field.key];input.value='';input.previousElementSibling.textContent=field.label+(field.configured?'（已配置）':'（未配置）');}settingsChanged.delete('env');status(result.message);
    }catch(e){status(e.message,true);}finally{busy=false;button.disabled=false;controls.forEach(x=>x.disabled=false);}
  };
  await renderPlugins($('#global-plugins'),null,null,plugins);
}
async function renderPlugins(holder,bot=null,group=null,initial=null){
  const query=bot===null?'':`?bot=${bot}&group=${group}`;
  const data=initial||await api('/api/plugins'+query);if(!holder.isConnected)return;
  holder.innerHTML='<h3>'+(group===null?'全局插件管理':'本群插件管理')+'</h3><p class="note">'+(group===null?'全局关闭时所有群均停用。批量卸载前须停止机器人，将删除所选插件的配置、任务和运行数据，原文件保留备份。':'只显示适用于本群的插件；本群开启仍受全局开关约束。')+'</p>';
  if(!data.items.length){holder.insertAdjacentHTML('beforeend','<p>没有已安装插件</p>');return;}
  const table=document.createElement('table');table.innerHTML='<thead><tr>'+(group===null?'<th>选择</th>':'')+'<th>插件</th><th>状态</th><th>操作</th></tr></thead><tbody></tbody>';holder.append(table);
  const selected=new Map();
  for(const item of data.items){
    const row=document.createElement('tr');const on=group===null?item.enabled:item.group_enabled;
    const scopes=item.scope===null?'全部群':item.scope.map(g=>(inventory.groups||[]).find(x=>String(x.bot)===item.bot&&String(x.grp)===g)?.name||g).join('、');
    row.innerHTML=(group===null?'<td><input type="checkbox" aria-label="选择卸载 '+esc(item.title)+'" '+(item.deletable?'':'disabled')+'></td>':'')+`<td>${esc(item.title)}<small class="plugin-details">${esc(item.id)} · 机器人 ${esc(item.bot)} · ${esc(scopes)}</small></td><td>${on?'已开启':'已关闭'}${group!==null&&!item.enabled?'（全局关闭）':''}</td><td><button class="quiet">${on?'关闭':'开启'}</button></td>`;
    if(group===null)row.querySelector('input').onchange=e=>{const key=item.bot+':'+item.id;if(e.target.checked)selected.set(key,{bot:item.bot,id:item.id});else selected.delete(key);};
    row.querySelector('button').onclick=async()=>{if(busy)return;busy=true;try{await api('/api/plugins/switch',{bot:item.bot,id:item.id,enabled:!on,group,revision:data.revision});await renderPlugins(holder,bot,group);status('插件开关已保存');}catch(e){status(e.message,true);await renderPlugins(holder,bot,group);}finally{busy=false;}};
    table.querySelector('tbody').append(row);
  }
  if(group===null){const button=document.createElement('button');button.className='quiet';button.textContent='卸载选中插件';holder.append(button);button.onclick=async()=>{if(busy)return;if(!selected.size){status('请先选择插件',true);return;}if(!window.confirm(`卸载选中的 ${selected.size} 个插件及其任务、运行数据？请先停止机器人。`))return;busy=true;try{const result=await api('/api/plugins/delete',{items:[...selected.values()],revision:data.revision});await renderPlugins(holder);status(result.message);}catch(e){status(e.message,true);}finally{busy=false;}};}
}


function dateText(value){return value?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';}
async function renderMemoryStatus(){
  $('#content').innerHTML='<section class="panel panel-body"><div class="toolbar"><button id="refresh-memory-status">刷新活动</button><span class="note">只显示实际记忆变化和需要处理的问题</span></div><div id="memory-activity" role="feed"></div><button id="activity-more" class="quiet">加载更早活动</button><details id="memory-diagnostics"><summary>查看运行诊断</summary><div id="memory-diagnostics-body"></div></details></section>';
  const holder=$('#memory-activity'),more=$('#activity-more'),seen=new Set();let cursor='',sequence=0;
  async function load(reset){
    const request=++sequence;if(reset){cursor='';seen.clear();holder.replaceChildren();}more.disabled=true;more.textContent='正在加载…';
    try{const data=await api('/api/memory-activities?'+new URLSearchParams({cursor,limit:20}));if(request!==sequence||!holder.isConnected)return;
      for(const item of data.items){
        if(seen.has(item.id))continue;seen.add(item.id);
        const row=document.createElement('article');row.className='memory-activity';
        const title=document.createElement('strong');title.textContent=item.title;
        const meta=document.createElement('small');meta.className='note activity-meta';meta.textContent=[dateText(item.created),item.origin].filter(Boolean).join(' · ');
        const summary=document.createElement('p');summary.textContent=item.summary;
        const detail=document.createElement('details'),toggle=document.createElement('summary');toggle.textContent='查看具体变化与依据';detail.append(toggle);let loaded=false;
        detail.ontoggle=async()=>{if(!detail.open||loaded)return;loaded=true;while(detail.children.length>1)detail.lastElementChild.remove();const body=document.createElement('div');body.textContent='正在读取变化…';detail.append(body);
          try{const result=await api('/api/memory-activities/'+encodeURIComponent(item.id)+'?bot='+item.bot);if(!detail.isConnected)return;body.replaceChildren();
            for(const change of result.changes||[]){
              const section=document.createElement('section'),heading=document.createElement('h4');heading.textContent=change.title;section.append(heading);
              if(change.historical){const diff=document.createElement('div');diff.className='diff activity-diff';for(const [label,value] of [['修改前',change.before],['修改后',change.after]]){const column=document.createElement('div'),name=document.createElement('small'),content=document.createElement('p');name.textContent=label;content.textContent=value??'无';column.append(name,content);diff.append(column);}section.append(diff);}
              else{const note=document.createElement('p');note.className='note';note.textContent='这条旧活动未保存当时的完整内容，无法还原前后变化。';section.append(note);}
              for(const line of change.evidence||[]){const evidence=document.createElement('p');evidence.className='note';evidence.textContent=line;section.append(evidence);}
              if(change.resource){const open=document.createElement('button');open.className='quiet';open.textContent='查看当前记录';open.onclick=async()=>{if(await navigate('memories'))await selectResource(change.resource);};section.append(open);}
              body.append(section);
            }
            for(const reason of result.reasons||[]){const paragraph=document.createElement('p');paragraph.className='note';paragraph.textContent=reason;body.append(paragraph);}
            if(!body.childNodes.length)body.textContent='没有额外的变化详情。';
          }catch(e){loaded=false;if(detail.isConnected)body.textContent=e.message;}
        };
        row.append(title,meta,summary,detail);holder.append(row);
      }
      cursor=data.next_cursor||'';more.hidden=!data.has_more;if(!holder.childNodes.length)holder.innerHTML='<div class="empty">暂无新的记忆变化。未保存内容的整理和普通召回不在这里显示。</div>';
    }catch(e){if(request===sequence)status(e.message,true);}finally{if(request===sequence){more.disabled=false;more.textContent='加载更早活动';}}
  }
  $('#refresh-memory-status').onclick=()=>load(true);more.onclick=()=>load(false);
  const diagnostics=$('#memory-diagnostics'),diagnosticBody=$('#memory-diagnostics-body');let diagnosticsLoaded=false;
  diagnostics.ontoggle=async()=>{if(!diagnostics.open||diagnosticsLoaded)return;diagnosticsLoaded=true;diagnosticBody.textContent='正在检查队列与记录…';try{const data=await api('/api/memory-status');if(!diagnostics.isConnected)return;diagnosticBody.replaceChildren();
    const counts=document.createElement('p');counts.textContent='等待整理 '+data.counts.pending+' · 等待重试 '+data.counts.failed+' · 已处理 '+data.counts.done;diagnosticBody.append(counts);
    for(const issue of data.issues||[]){const line=document.createElement('p');line.textContent=(issue.title||issue.resource||issue.id||'记录')+'：'+(issue.message||issue.reason||'需要核对来源或状态');diagnosticBody.append(line);}
    for(const job of data.jobs||[]){const line=document.createElement('p');line.textContent='整理任务 '+(job.id||job.job||'')+'：'+({pending:'等待整理',failed:'等待重试',running:'正在整理'}[job.state||job.status]||'待处理');diagnosticBody.append(line);}
    if(!(data.issues||[]).length&&!(data.jobs||[]).length){const line=document.createElement('p');line.textContent='当前没有需要处理的任务或异常记录。';diagnosticBody.append(line);}
  }catch(e){diagnosticsLoaded=false;if(diagnostics.isConnected)diagnosticBody.textContent=e.message;}};
  await load(true);
}

async function renderLibrary(){
  let loadSequence=0,offset=0;const seen=new Set();
  $('#content').innerHTML='<div class="toolbar"><select id="library-category" aria-label="记忆类别">'+(page==='robot'?'<option value="机器人">机器人档案</option>':'<option value="">全部内容</option><option>人物</option><option>主题</option><option>事件</option><option>群背景</option><option>表情</option>')+'</select><input id="library-query" placeholder="搜索人物、主题或正文" aria-label="搜索记忆"><label><input type="checkbox" id="library-maintenance">维护记录</label><button id="library-search">查询</button><button id="library-add">新增记录</button><button id="obsidian-manage" class="quiet">Obsidian</button></div><details class="panel panel-body"><summary>测试记忆召回（不调用模型、不发送消息）</summary><div class="toolbar"><select id="recall-bot" aria-label="机器人账号">'+(inventory.bots||[]).map(b=>`<option value="${b}">${b}</option>`).join('')+'</select><input id="recall-subject" placeholder="人物 QQ，可留空"><input id="recall-query" class="wide" placeholder="输入要测试的问题"><button id="recall-test">测试召回</button></div><div id="recall-output"></div><p class="note">结果表示会提供给模型的记忆，不代表模型一定采纳。</p></details><div class="workspace memory-workspace"><section class="panel"><div id="library-list" class="listing"></div><button id="library-more" class="quiet">加载更多</button></section><section class="panel"><div class="panel-body" id="library-overview"><p>选择人物或主题查看完整档案与近期变化。</p></div><div id="editor"></div></section><aside class="panel assistant"><div class="panel-title"><h3>AI 辅助修改</h3><span class="badge">确认后保存</span></div><div class="panel-body"><div id="chat" class="chat"></div><textarea id="instruction" aria-label="修改要求" placeholder="选中具体记录后提出修改要求" disabled></textarea><button id="suggest" disabled>生成修改建议</button><button id="review-proposal" class="quiet" hidden>查看建议差异</button></div></aside></div>';
  async function load(reset=true){
    const request=++loadSequence,target=$('#library-list'),more=$('#library-more');if(!target)return;if(reset){offset=0;seen.clear();target.replaceChildren();}more.disabled=true;
    try{
    const params=new URLSearchParams({query:$('#library-query').value,category:$('#library-category').value,maintenance:$('#library-maintenance').checked,offset,limit:30});
    const data=await api('/api/memory-library?'+params);if(request!==loadSequence||target!==$('#library-list'))return;
    const list=target;
    for(const item of data.groups){
      const key=item.bot+':'+item.key;if(seen.has(key))continue;seen.add(key);
      let group=item;
      const button=document.createElement('button');button.className='list-item';
      const title=document.createElement('strong');title.textContent=group.title;
      const small=document.createElement('small');small.textContent=group.category+' · '+group.note_count+' 条依据';button.append(title,small);
      button.onclick=async()=>{
        if(busy||(dirty()&&!window.confirm('放弃未保存草稿并切换？')))return;
        const selected=++selectionVersion;current=null;draft=null;showSelection();$('#editor').replaceChildren();
        const panel=$('#library-overview');panel.textContent='正在读取档案…';
        try{group=await api('/api/memory-library/detail?'+new URLSearchParams({bot:item.bot,key:item.key,maintenance:$('#library-maintenance').checked}));if(selected!==selectionVersion||!panel.isConnected)return;panel.replaceChildren();
        const heading=document.createElement('h3');heading.textContent=group.title;
        const content=document.createElement('div');content.className='memory-document';for(const block of group.content.split(/(?=^### )/m)){const lines=block.split('\n');if(lines[0].startsWith('### ')){const heading=document.createElement('h4');heading.textContent=lines.shift().slice(4);content.append(heading);}const paragraph=document.createElement('div');paragraph.textContent=lines.join('\n');content.append(paragraph);}panel.append(heading,content);
        const details=document.createElement('details'),label=document.createElement('summary');label.textContent='查看依据与编辑记录（'+group.notes.length+'）';details.append(label);
        for(const note of [...group.notes].sort((a,b)=>b.updated-a.updated)){
          const row=document.createElement('div');row.className='memory-note-row';
          const edit=document.createElement('button');edit.className='quiet';edit.textContent=(note.field||memoryKinds[note.kind])+' · '+dateText(note.updated)+(note.fixed?' · 人工固定':'');edit.onclick=()=>selectResource(note.resource);
          const obsidian=document.createElement('button');obsidian.className='quiet';obsidian.textContent='Obsidian 编辑稿';obsidian.onclick=()=>openObsidianDraft(group.bot,note.id);row.append(edit,obsidian);details.append(row);
        }panel.append(details);
        }catch(e){if(selected===selectionVersion&&panel.isConnected){panel.textContent=e.message;status(e.message,true);}}
      };list.append(button);
    }
    offset+=data.groups.length;more.hidden=!data.has_more;
    if(!offset)list.innerHTML='<div class="empty">暂无有效内容；空白保护记录和历史索引可在维护记录中核对。</div>';
    }catch(e){if(request===loadSequence)status(e.message,true);}finally{if(request===loadSequence)more.disabled=false;}
  }
  $('#library-more').onclick=()=>load(false);
  $('#library-query').onkeydown=e=>{if(e.key==='Enter')load();};
  $('#library-search').onclick=()=>load().catch(e=>status(e.message,true));$('#library-category').onchange=$('#library-search').onclick;$('#library-maintenance').onchange=$('#library-search').onclick;
  $('#library-add').onclick=openMemoryCreate;$('#obsidian-manage').onclick=()=>openObsidian().catch(e=>status(e.message,true));
  $('#suggest').onclick=suggest;$('#review-proposal').onclick=()=>review(proposal.value);
  $('#recall-test').onclick=async()=>{
    const output=$('#recall-output'),button=$('#recall-test');button.disabled=true;output.textContent='正在检查记忆…';
    try{
      const subject=$('#recall-subject').value.trim(),result=await api('/api/memory-recall',{bot:Number($('#recall-bot').value),query:$('#recall-query').value,subject:subject?Number(subject):null});if(!output.isConnected)return;output.replaceChildren();
      for(const note of result.selected||[]){const row=document.createElement('article');row.className='memory-activity';const title=document.createElement('strong');title.textContent=note.field||memoryKinds[note.kind]||'参考记忆';const content=document.createElement('p');content.className='memory-document';content.textContent=note.value;row.append(title,content);output.append(row);}
      if(!(result.selected||[]).length)output.textContent='没有找到适合本次问题的有效记忆。';
      const labels={fixed:'人工固定',stale_version:'已有更新版本',target_removed:'目标已移除',duplicate_or_cooldown:'重复或未到强化间隔',source_invalid:'来源已失效',empty_protection:'人工清空保护',budget:'超过本次参考预算',not_relevant:'与本次问题不相关',decayed:'记忆权重已衰减',archived:'已归档',quarantine:'冲突待核对',fixed_override:'同字段人工固定优先'},counts={};
      for(const item of result.excluded||[])counts[item.reason]=(counts[item.reason]||0)+1;
      if(Object.keys(counts).length){const excluded=document.createElement('p');excluded.className='note';excluded.textContent='未采用的记录：'+Object.entries(counts).map(([reason,count])=>(labels[reason]||'其他原因')+' '+count+' 条').join('；');output.append(excluded);}
    }catch(e){if(output.isConnected){output.textContent=e.message;status(e.message,true);}}finally{button.disabled=false;}
  };
  await load();
}
async function openObsidianDraft(bot,id){
  try{const result=await api('/api/obsidian',{bot,action:'draft',id});await openObsidian(bot);status('编辑稿已准备；在 Obsidian 修改后回此处查看差异并保存。');const link=document.createElement('a');link.href=result.uri;link.textContent=result.uri==='obsidian://choose-vault'?'首次接入：打开仓库管理器':'打开当前编辑稿';link.onclick=async(event)=>{event.preventDefault();try{const fresh=await api('/api/obsidian',{bot,action:'launch',id});status(fresh.launch_message);}catch(e){status(e.message,true);}};$('#obsidian-body').prepend(link);}catch(e){status(e.message,true);}
}
async function openObsidian(selectedBot){
  const bot=selectedBot||(inventory.bots||[])[0];if(!bot)return status('尚未绑定机器人账号',true);
  const data=await api('/api/obsidian?bot='+bot);
  const dialog=document.createElement('dialog');dialog.className='obsidian-dialog';
  dialog.innerHTML='<div id="obsidian-body"><p>'+esc(data.installed?'已检测到 Obsidian':'未检测到 Obsidian，请先安装官方客户端')+'</p><p class="note">与机器人共享同一记忆库。源笔记用于浏览；编辑稿经后台确认后生效，不能直接编辑源笔记来绕过保护。</p><p id="obsidian-setup" class="note">'+esc(data.registration==='registered'?'仓库已登记，可以直接打开。':data.setup_hint)+'</p><code>'+esc(data.vault)+'</code><div class="actions"><button id="obsidian-prepare">刷新导航</button><a id="obsidian-open" href="'+esc(data.uri)+'">'+(data.registration==='registered'?'在 Obsidian 中打开':'首次接入：打开仓库管理器')+'</a><button id="obsidian-copy" class="quiet">复制仓库目录</button><button id="obsidian-refresh" class="quiet">读取待审稿</button></div><div id="obsidian-drafts"></div></div><div class="actions"><button id="obsidian-close">关闭</button></div>';
  document.body.append(dialog);dialog.showModal();dialog.onclose=()=>dialog.remove();$('#obsidian-close').onclick=()=>dialog.close();
  async function refresh(initial){const result=initial||await api('/api/obsidian?bot='+bot);if(!dialog.isConnected)return;$('#obsidian-open').href=result.uri;$('#obsidian-open').textContent=result.registration==='registered'?'在 Obsidian 中打开':'首次接入：打开仓库管理器';$('#obsidian-setup').textContent=result.registration==='registered'?'仓库已登记，可以直接打开。':result.setup_hint;const holder=$('#obsidian-drafts');holder.replaceChildren();for(const item of result.drafts){const button=document.createElement('button');button.className='list-item';button.textContent=item.title+(item.conflict?' · 源记忆已变化':' · 待审修改');button.onclick=async()=>{try{const review=await api('/api/obsidian',{bot,action:'review',id:item.id});const box=document.createElement('div');box.innerHTML='<div class="diff"><div><h3>修改前</h3><pre>'+esc(review.before)+'</pre></div><div><h3>修改后</h3><pre>'+esc(review.after)+'</pre></div></div>';const apply=document.createElement('button');apply.textContent=review.conflict?'存在版本冲突，保留稿件':'确认保存并固定';apply.disabled=review.conflict;apply.onclick=async()=>{apply.disabled=true;try{await api('/api/obsidian',{bot,action:'apply',id:item.id,revision:review.revision,confirm:'apply_draft'});box.remove();await refresh();status('Obsidian 编辑已保存，旧版本保留。');}catch(e){status(e.message,true);apply.disabled=false;}};box.append(apply);holder.append(box);}catch(e){status(e.message,true);}};holder.append(button);}if(!result.drafts.length)holder.textContent='没有待审修改。';}
  $('#obsidian-prepare').onclick=async()=>{try{await api('/api/obsidian',{bot,action:'prepare'});status('导航已刷新，原记忆和编辑稿保持不变。');}catch(e){status(e.message,true);}};
  $('#obsidian-copy').onclick=async()=>{try{await navigator.clipboard.writeText(data.vault);status('仓库目录已复制。在 Obsidian 选择“打开文件夹作为仓库”，粘贴此目录。');}catch(e){status('复制未成功，请选择并复制上方完整目录。',true);}};
  $('#obsidian-open').onclick=async(event)=>{event.preventDefault();try{const result=await api('/api/obsidian',{bot,action:'launch'});status(result.launch_message);await refresh();}catch(e){status(e.message,true);}};
  $('#obsidian-refresh').textContent='刷新接入状态与待审稿';
  $('#obsidian-refresh').onclick=()=>refresh().catch(e=>status(e.message,true));await refresh(data);
}

function openMemoryCreate(){
  if(busy||dirty())return status('请先保存或放弃当前草稿');
  const bots=inventory.bots||[];
  if(!bots.length)return status('请先运行机器人或导入已指定账号的数据',true);
  current=null;draft=null;showSelection();
  $('#editor').innerHTML='<div class="panel-body"><label class="field"><span>机器人账号</span><select id="memory-bot">'+bots.map(b=>`<option value="${b}">${b}</option>`).join('')+'</select></label><label class="field"><span>类别</span><select id="memory-kind">'+Object.entries(memoryKinds).filter(([k])=>page!=='robot'||['self','thought','diary'].includes(k)).map(([key,title])=>`<option value="${key}">${title}</option>`).join('')+'</select></label><label class="field"><span>主体 QQ（无特定人物填 0）</span><input id="memory-subject" inputmode="numeric" value="0"></label><label class="field"><span>名称</span><input id="memory-field" maxlength="100"></label><label class="field"><span>正文</span><textarea id="memory-content" maxlength="16000"></textarea></label><button id="memory-create">确认新增</button></div>';
  $('#editor').oninput=()=>settingsChanged.add('memory-create');
  $('#memory-create').onclick=async()=>{
    const kind=$('#memory-kind').value,bot=Number($('#memory-bot').value),subject=['self','thought','diary'].includes(kind)?bot:Number($('#memory-subject').value),field=$('#memory-field').value.trim(),content=$('#memory-content').value;
    if(!window.confirm('新增并固定以下记录？\n'+memoryKinds[kind]+' / '+field+' / QQ '+subject+'\n\n'+content))return;
    busy=true;$('#memory-create').disabled=true;
    try{const result=await api('/api/memories',{bot,kind,subject,field,content});settingsChanged.delete('memory-create');invalidateNavigation();await loadNavigation('base');await renderPage();busy=false;await selectResource(result.resource);status('记录已新增并固定');}catch(e){status(e.message,true);}finally{busy=false;if($('#memory-create'))$('#memory-create').disabled=false;}
  };
}
