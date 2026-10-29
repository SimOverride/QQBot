const assert=require('node:assert/strict');
const {test}=require('node:test');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('admin/app.js','utf8');
function context(fetch){
  const sandbox=vm.createContext({fetch,AbortController,Date,JSON});
  vm.runInContext("let token='',pageVersion=0,pageController=new AbortController(),inventory={};const navigationCache=new Map(),staleRequest='页面已切换';"+source.slice(source.indexOf('function invalidateNavigation'),source.indexOf('const settingsChanged'))+source.slice(source.indexOf('async function api('),source.indexOf('function dirty()'))+';function switchPage(){pageController.abort();pageController=new AbortController();pageVersion++;}',sandbox);
  return sandbox;
}
// 延迟的旧响应不能覆盖新页面，即使网络层忽略了取消信号。
test('切页后旧响应在解析完成时被丢弃',async()=>{
  let resolve;
  const box=context(async()=>({ok:true,json:()=>new Promise(done=>resolve=done)}));
  const pending=box.api('/api/memory-library');await new Promise(setImmediate);box.switchPage();resolve({groups:['旧数据']});
  await assert.rejects(pending,/页面已切换/);
});
// 取消只影响读请求，已经提交的写入仍应等待明确结果。
test('切页取消读取但不取消写入',async()=>{
  const seen=[];let finish;
  const box=context(async(path,options)=>{seen.push(options);return {ok:true,json:()=>new Promise(done=>finish=done)};});
  const pending=box.api('/api/resource',{content:'新值'});await new Promise(setImmediate);box.switchPage();finish({saved:true});
  assert.equal((await pending).saved,true);assert.equal(seen[0].signal,undefined);
});
test('取消读取不显示网络错误',async()=>{
  const box=context((path,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'})))));
  const pending=box.api('/api/emotes');box.switchPage();await assert.rejects(pending,/页面已切换/);
});
// 保存之后必须使导航缓存失效，防止已删除资源重新出现。
test('导航分区缓存复用并在修改后失效',async()=>{
  let calls=0;const box=context(async()=>({ok:true,json:async()=>({bots:[++calls]})}));
  await box.loadNavigation('base');await box.loadNavigation('base');assert.equal(calls,1);
  box.invalidateNavigation();await box.loadNavigation('base');assert.equal(calls,2);
});
