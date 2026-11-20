/** Simulated DOM integration of the real shared component with each editor. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM, VirtualConsole} = require('jsdom');
const dir = path.join(__dirname, '..', 'static');
const A = 'a'.repeat(32), B = 'b'.repeat(32);
const style = {font_family:'DejaVu Sans', font_size:56, color:'#FFFFFF'};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const clone = value => structuredClone(value);
const reply = (body, status = 200) => ({ok:status < 400, status,
  headers:{get:() => 'application/json'}, json:async () => clone(body)});
async function until(check, message) {
  for (let i=0; i<250; i++) { if (check()) return; await sleep(8); }
  throw new Error(message || 'DOM did not settle');
}
function fixture(t, page = 'index', intercept = () => undefined) {
  const timeline = page === 'timeline', calls = [];
  const dom = new JSDOM(fs.readFileSync(path.join(dir, page+'.html'), 'utf8'), {
    url:'https://studio.example.test/'+(timeline ? 'studio/'+A : ''), runScripts:'outside-only',
    pretendToBeVisual:true, virtualConsole:new VirtualConsole()});
  t.after(() => dom.window.close());
  const w = dom.window, $ = selector => w.document.querySelector(selector);
  for (const name of ['load','pause','play']) w.HTMLMediaElement.prototype[name] = () => Promise.resolve();
  w.AutoClipsLive = {refresh(){},destroyAll(){}};
  const nativeTimeout = w.setTimeout.bind(w);
  w.setTimeout = (fn, ms, ...args) => nativeTimeout(fn, ms >= 1000 && ms <= 1250 ? 2 : ms, ...args);
  const layouts = new Map(), previews = new Map(), finals = new Map();
  w.fetch = async (url, opts={}) => {
    const path = new URL(url, w.location.origin).pathname, method = opts.method || 'GET';
    const [, , , id] = path.split('/');
    const body = opts.body ? JSON.parse(opts.body) : null;
    const call = {path, id, method, body};
    calls.push(call);
    const custom = intercept(call);
    if (custom !== undefined) return await custom;
    if (path === '/api/config') return reply({ffmpeg:true, transcribe_available:true, storage_free_bytes:2e9});
    if (path === '/api/videos') return reply({videos:[A,B].map(id => ({
      id, name:id[0]+'.mp4', duration:15, width:1280, height:720, size:1024, platform:'local',
      stream_url:'/api/videos/'+id+'/stream'}))});
    if (path === '/api/lives') return reply({lives:[]});
    if (path === '/api/layout-presets') return reply({presets:[], custom_presets:[]});
    if (path.endsWith('/activity')) return reply({activity:null});
    if (path.endsWith('/layout')) {
      if (method === 'PUT') layouts.set(id, body);
      return reply(layouts.get(id) || {layout:'fit', layout_config:{mode:'standard'}});
    }
    if (path.endsWith('/transcript')) return reply({available:false, words:[], state:{}, subtitle_style:style});
    if (path.endsWith('/timeline')) return reply({timeline:{schema:1,version:1,source_duration:15,
      segments:[{id:'seg_001',source_in:0,source_out:2}]},edited_duration:2, can_undo:false, can_redo:false});
    if (path.endsWith('/timeline/exports')) return reply({exports:{preview:previews.get(id),final:finals.get(id)}});
    if (path.endsWith('/manifest')) {
      const preview = previews.get(id);
      return reply({clips:[{id:'clip_01',title:id[0]+' clip',start:0,end:2,status:'ready',
        preview_options:preview && {...preview,burn:preview.subtitles},preview_url:preview && '/preview.mp4',
        final_options:null,final_url:null}]});
    }
    if (path.endsWith('/subtitle-style') || path.endsWith('/clips')) return reply({});
    if (path.includes('/render/')) {
      const quality = path.split('/').pop();
      const record = {layout:body.layout, layout_config:body.layout_config, subtitles:body.subtitles,
        subtitle_style:body.subtitle_style, render_id:quality+'-new', version:1, probe:{duration:2}};
      (quality === 'preview' ? previews : finals).set(id,record);
      return reply({task_id:'render'});
    }
    if (path === '/api/tasks/render') return reply({status:'done',result:{}});
    throw new Error('Unexpected '+path);
  };
  for (const file of ['video-activity.js','layout-editor.js',timeline ? 'timeline.js' : 'app.js'])
    w.eval(fs.readFileSync(path.join(dir,file), 'utf8'));
  function change(selector,value) {
    const element=$(selector);
    if (element.type==='checkbox') element.checked=value; else element.value=value;
    element.dispatchEvent(new w.Event('change',{bubbles:true}));
  }
  async function open(id) {
    w._openVideoById(id);
    await until(() => $('.c-title')?.value === id[0]+' clip', 'Cannot open '+id[0]);
  }
  return {w,$,calls,change,open,layouts};
}

for (const page of ['index','timeline']) test(page+': approved horizontal render carries the actual preview id', async t => {
  const f=fixture(t,page);
  if (page==='index') await f.open(A);
  else await until(() => f.calls.some(c=>c.path.endsWith('/timeline/exports')));
  f.change('[data-field=aspect]','16:9');
  if (page==='index') await f.w._renderClip('clip_01','preview');
  else { f.change('#approve-selection',true); f.$('#render-preview').click(); }
  if (page==='index') {
    await until(()=>f.$('.c-preview-approve') && !f.$('.c-preview-approve').disabled);
    f.$('.c-preview-approve').checked=true;
    f.w._approveClipPreview('clip_01');
    await f.w._renderClip('clip_01','final');
  } else {
    await until(()=>!f.$('#approve-preview').disabled);
    f.change('#approve-preview',true);
    f.$('#render-final').click();
  }
  await until(()=>f.calls.some(c=>c.path.endsWith('/render/final')));
  const renders=f.calls.filter(c=>c.path.includes('/render/'));
  assert.deepEqual(renders.map(c=>c.body.layout_config.aspect),['16:9','16:9']);
  assert.equal(renders[1].body.approved_preview_id,'preview-new');
  assert.equal(f.layouts.get(A).layout_config.aspect,'16:9');
});

test('editing video B cannot change persisted layout of video A', async t => {
  const f=fixture(t);
  await f.open(A); await f.open(B);
  f.change('[data-field=aspect]','16:9');
  await until(()=>f.layouts.has(B),'No saved layout for B');
  await sleep(25);
  assert.equal(f.layouts.has(A),false,'A received an edit made in B');
});

test('while video B loads the previous video has no editable clip cards', async t => {
  let release;
  const wait = new Promise(r=>release=r);
  const f=fixture(t,'index',c=>c.id===B && c.path.endsWith('/transcript') ?
    wait.then(()=>reply({available:false,words:[],state:{},subtitle_style:style})) : undefined);
  await f.open(A);
  f.w._openVideoById(B);
  try {
    await until(()=>f.calls.some(c=>c.id===B && c.path.endsWith('/transcript')));
    assert.equal(f.$('.clip-card'),null,'A clip is still interactive with B selected');
  } finally { release(); await until(()=>f.$('.c-title')?.value === 'b clip'); }
});

test('failed layout load does not trap the next video in the previous editor', async t => {
  const f=fixture(t,'index',c=>c.id===A && c.path.endsWith('/layout') ? reply({detail:'Falha no layout.'},503) : undefined);
  f.w._openVideoById(A);
  await until(()=>f.calls.some(c=>c.id===A && c.path.endsWith('/layout')));
  await sleep(20);
  await f.open(B);
  assert.equal(f.$('.c-title').value,'b clip');
});

function deferred() {
  let resolve;
  const promise=new Promise(done=>{resolve=done;});
  return {promise,resolve};
}

test('two clip preview clicks before the first save submit one render and block other video operations', async t => {
  const save=deferred(), poll=deferred();
  const f=fixture(t,'index',call=>{
    if (call.path.endsWith('/clips') && call.method==='PUT') return save.promise.then(()=>reply({}));
    if (call.path==='/api/tasks/render') return poll.promise.then(()=>reply({status:'done',result:{}}));
  });
  await f.open(A);
  const one=f.w._renderClip('clip_01','preview'), two=f.w._renderClip('clip_01','preview');
  assert.equal(f.calls.filter(c=>c.path.endsWith('/clips') && c.method==='PUT').length,1);
  save.resolve();
  await until(()=>f.calls.some(c=>c.path==='/api/tasks/render'));
  // Saving re-fetches the manifest and replaces the original buttons.
  assert.equal(f.$('button[onclick*="_renderClip"]').disabled,true);
  f.$('#btn-transcribe').click(); f.$('#btn-analyze').click(); f.$('#btn-manual').click();
  await f.w._saveAllClips(); await f.w._deleteClip('clip_01');
  assert.equal(f.calls.filter(c=>c.path.endsWith('/render/preview')).length,1);
  assert.equal(f.calls.filter(c=>c.method==='DELETE' || c.method==='POST' && !c.path.endsWith('/render/preview')).length,0);
  assert.equal(f.$('#editor-player').controls,true);
  assert.equal(f.$('#video-activity').hidden,false);
  poll.resolve(); await Promise.all([one,two]);
  assert.equal(f.$('#video-activity').hidden,true);
  assert.equal(f.$('button[onclick*="_renderClip"]').disabled,false);
  assert.equal(f.$('.c-preview-approve').checked,false);
});

test('resumed render blocks detection and editing, keeps player available and refreshes exports after completion', async t => {
  const poll=deferred(); let active=true;
  const f=fixture(t,'timeline',call=>{
    if (call.path.endsWith('/layout')) return reply({layout:'fit',layout_config:{mode:'pip',face:{x:0,y:0,w:.5,h:.5}}});
    if (call.path.endsWith('/activity')) return reply({activity:active ? {task_id:'busy',kind:'render-preview',status:'running'} : null});
    if (call.path==='/api/tasks/busy') return poll.promise.then(()=>reply({status:'done',result:{}}));
  });
  await until(()=>f.calls.some(c=>c.path.endsWith('/timeline/exports')));
  f.change('#approve-selection',true);
  assert.equal(f.$('#render-preview').disabled,true);
  assert.equal(f.$('#save-trim').disabled,true);
  assert.equal(f.$('.le-detect').matches(':disabled'),true);
  f.$('.le-detect').click(); f.$('#render-preview').click(); f.$('#save-trim').click();
  assert.equal(f.calls.filter(c=>c.method==='POST').length,0);
  assert.equal(f.$('#play-edit').disabled,false);
  const firstCount=f.calls.filter(c=>c.path.endsWith('/timeline/exports')).length;
  active=false; poll.resolve();
  await until(()=>f.calls.filter(c=>c.path.endsWith('/timeline/exports')).length>firstCount);
  assert.equal(f.$('#video-activity').hidden,true);
  assert.equal(f.$('#render-preview').disabled,false);
});

test('an unreadable activity status offers a retry and never claims a completed render', async t => {
  let offline=true;
  const f=fixture(t,'timeline',call=>{
    if (call.path.endsWith('/activity')) return offline ? reply({},503) : reply({activity:null});
  });
  await until(()=>f.calls.some(c=>c.path.endsWith('/timeline/exports')));
  f.change('#approve-selection',true);
  assert.equal(f.$('#render-preview').disabled,true);
  assert.equal(f.$('#video-activity button').hidden,false);
  assert.match(f.$('#video-activity').textContent,/Não foi possível confirmar/);
  offline=false;
  f.$('#video-activity button').click();
  await until(()=>!f.$('#render-preview').disabled);
  assert.equal(f.calls.filter(c=>c.path.endsWith('/activity')).length,2);
  assert.equal(f.calls.filter(c=>c.path.includes('/render/')).length,0);
});

test('terminal first task leaves the controls locked for a second task on the same video', async t => {
  const first=deferred(), second=deferred(); let current='one';
  const f=fixture(t,'timeline',c=>{
    if (c.path.endsWith('/activity')) return reply({activity:current && {kind:'publish-youtube',task_id:current,status:'running'}});
    if (c.path==='/api/tasks/one') return first.promise.then(()=>reply({status:'done',result:{}}));
    if (c.path==='/api/tasks/two') return second.promise.then(()=>reply({status:'error',error:'Falhou a segunda tarefa simulada.'}));
  });
  await until(()=>f.calls.some(c=>c.path.endsWith('/timeline/exports')));
  f.change('#approve-selection',true);
  current='two'; first.resolve();
  await until(()=>f.calls.some(c=>c.path==='/api/tasks/two'));
  assert.equal(f.$('#render-preview').disabled,true);
  assert.equal(f.$('#video-activity').hidden,false);
  current=null; second.resolve();
  await until(()=>!f.$('#render-preview').disabled);
  assert.match(f.$('#notice').textContent,/Falhou a segunda tarefa simulada/);
  assert.equal(f.calls.some(c=>c.method==='POST'),false);
});

test('lost render POST reply requires a deliberate status check without sending a second request', async t => {
  const f=fixture(t,'index',c=>{
    if (c.path.endsWith('/render/preview')) return Promise.reject(new TypeError('lost response'));
  });
  await f.open(A);
  await f.w._renderClip('clip_01','preview');
  assert.equal(f.calls.filter(c=>c.path.endsWith('/activity')).length,1,'no automatic idle read after an ambiguous POST');
  assert.equal(f.$('button[onclick*="_renderClip"]').disabled,true);
  assert.match(f.$('#video-activity').textContent,/pedido pode ter chegado/);
  f.$('#video-activity button').click();
  await until(()=>f.$('#video-activity button').textContent.includes('Liberar'));
  assert.equal(f.$('button[onclick*="_renderClip"]').disabled,true);
  f.$('#video-activity button').click();
  await until(()=>f.$('#video-activity').hidden);
  assert.equal(f.calls.filter(c=>c.path.endsWith('/render/preview')).length,1);
});

for (const page of ['index','timeline']) test(page+': explicit caption choice survives a new page', async t=>{
  let saved={layout:'fit',layout_config:{mode:'standard'},subtitles:null};
  let speech=true;
  const backend=c=>{
    if (c.path.endsWith('/transcript')) return reply({available:speech,words:speech?[{start:0,end:1,text:'fala'}]:[],state:{},subtitle_style:style});
    if (c.path.endsWith('/layout')) { if(c.method==='PUT') saved=c.body; return reply(saved); }
  };
  const selector=page==='index'?'#burn-subs':'#export-subtitles';
  async function visit() {
    const f=fixture(t,page,backend);
    if(page==='index') await f.open(A); else await until(()=>f.calls.some(c=>c.path.endsWith('/timeline/exports')));
    return f;
  }
  const original=await visit();
  original.change(selector,true);
  await until(()=>saved.subtitles===true);
  const reloaded=await visit(); assert.equal(reloaded.$(selector).checked,true);
  // A video without words cannot burn captions. An unrelated change must
  // preserve the earlier choice so it can apply once the transcript returns.
  speech=false;
  const silent=await visit(); assert.equal(silent.$(selector).checked,false);
  assert.equal(silent.$(selector).disabled,true);
  silent.change('[data-field=aspect]','16:9');
  await until(()=>saved.layout_config.aspect==='16:9');
  assert.equal(saved.subtitles,true);
  speech=true;
  reloaded.change(selector,false);
  await until(()=>saved.subtitles===false);
  const reopened=await visit(); assert.equal(reopened.$(selector).checked,false);
});

test('storage preflight 507 creates no task and a shorter selection can be submitted immediately', async t => {
  let rejectForSpace=true;
  const f=fixture(t,'index',c=>{
    if (rejectForSpace && c.path.endsWith('/render/preview'))
      return reply({detail:'Espaço insuficiente. Selecione um corte menor.'},507);
  });
  await f.open(A);
  await f.w._renderClip('clip_01','preview');
  assert.match(f.$('#notice').textContent,/Espaço insuficiente/);
  assert.equal(f.$('#video-activity').hidden,true);
  assert.equal(f.$('button[onclick*="_renderClip"]').disabled,false);
  assert.equal(f.calls.filter(c=>c.path.startsWith('/api/tasks/')).length,0);
  assert.equal(f.calls.filter(c=>c.path.endsWith('/activity')).length,1);
  assert.equal(f.w.sessionStorage.getItem('autoclips.pending-submit.'+A),null);

  rejectForSpace=false;
  f.$('.c-end').value='1';
  await f.w._renderClip('clip_01','preview');
  assert.equal(f.calls.filter(c=>c.path.endsWith('/clips') && c.method==='PUT').at(-1).body.clips[0].end,1);
  assert.equal(f.calls.filter(c=>c.path.endsWith('/render/preview')).length,2);
  assert.equal(f.calls.filter(c=>c.path.startsWith('/api/tasks/')).length,1);
  assert.equal(f.$('.c-preview-approve').checked,false);
});
