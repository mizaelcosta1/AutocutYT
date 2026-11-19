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
  for (const file of ['layout-editor.js',timeline ? 'timeline.js' : 'app.js'])
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
