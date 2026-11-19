/** Simulated DOM tests, not browser visual QA. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const path=require('node:path');
const repo=path.resolve(__dirname,'..');
function fixture(page) {
 const dom=new JSDOM(fs.readFileSync(path.join(repo,'static',page),'utf8'),{url:'https://studio.example/',runScripts:'outside-only'});
 const w=dom.window, calls=[], changes=[], subtitle=[];
 const options={mode:'split_caption',aspect:'9:16',face:null,gameplay:null,split_ratio:.34,pip_position:'top-right',pip_scale:.28,game_fit:'contain',caption_position:'middle'};
 const reply=body=>({ok:true,json:async()=>body});
 w.fetch=async(url,opts={})=>{calls.push({url,opts}); if(url==='/api/layout-presets') return reply({presets:[{id:'talk',name:'Rosto e jogo',layout:'fit',layout_config:options,subtitles:true}],custom_presets:[]}); if(url.endsWith('/layout') && opts.method==='PUT') return reply({ok:true}); if(url.endsWith('/layout')) return reply({layout:'blur',layout_config:{...options,mode:'standard'}}); throw new Error(url);};
 w.eval(fs.readFileSync(path.join(repo,'static/layout-editor.js'),'utf8'));
 const editor=w.LayoutEditor.create({container:'#layout-editor',videoId:'abc',layoutSelect:page==='index.html'?'#layout':'#export-layout',onChange:()=>changes.push(1),onPresetSubtitles:v=>subtitle.push(v)});
 return {w,editor,calls,changes,subtitle,options,$:s=>w.document.querySelector(s),close:()=>w.close()};
}
for(const page of ['index.html','timeline.html']) test(`${page}: preset, manual ROI and persistence`,async t=>{
 const f=fixture(page);t.after(f.close);await f.editor.load();
 assert.equal(f.editor.getConfig().mode,'standard');
 f.$('[data-preset="talk"]').click(); assert.equal(f.editor.getConfig().mode,'split_caption'); assert.equal(f.editor.getLayout(),'fit'); assert.deepEqual(f.subtitle,[true]);
 f.$('.le-reset').click(); assert.deepEqual(JSON.parse(JSON.stringify(f.editor.getConfig().face)),{x:0,y:0,w:1,h:1});
 const x=f.$('[data-coordinate=x]'); x.value='30';x.dispatchEvent(new f.w.Event('change',{bubbles:true}));
 assert.match(f.$('.le-error').textContent,/Confira/); await assert.rejects(f.editor.flush());
 x.value='0';x.dispatchEvent(new f.w.Event('change',{bubbles:true}));
 await f.editor.flush(); const put=f.calls.find(c=>c.opts.method==='PUT'); const payload=JSON.parse(put.opts.body); assert.equal(payload.layout_config.mode,'split_caption'); assert.equal(payload.layout_config.face.w,1); assert.ok(f.changes.length>=2);
});

test('failed load prevents saving and reports server detail',async t=>{
 const f=fixture('index.html');t.after(f.close);
 f.w.fetch=async url=>url.endsWith('/layout')?{ok:false,json:async()=>({detail:'Vídeo indisponível.'})}:{ok:true,json:async()=>({presets:[],custom_presets:[]})};
 await assert.rejects(f.editor.load(),/Vídeo indisponível/);
 assert.match(f.$('.le-status').textContent,/Vídeo indisponível/);
 await assert.rejects(f.editor.flush(),/Carregue/);
});

test('writes serialize and switching video waits for the old save',async t=>{
 const f=fixture('index.html');t.after(f.close);await f.editor.load();
 let release, first=true; const calls=[];
 f.w.fetch=async(url,opts={})=>{
   if (opts.method==='PUT') { calls.push({url,body:JSON.parse(opts.body)}); if(first){ first=false; await new Promise(r=>release=r); } return {ok:true,json:async()=>({})}; }
   if(url==='/api/layout-presets') return {ok:true,json:async()=>({presets:[],custom_presets:[]})};
   return {ok:true,json:async()=>({layout:'blur',layout_config:{mode:'standard'}})};
 };
 const aspect=f.$('[data-field=aspect]');aspect.value='16:9';aspect.dispatchEvent(new f.w.Event('change',{bubbles:true}));
 const one=f.editor.flush(); await Promise.resolve();await Promise.resolve();
 aspect.value='9:16';aspect.dispatchEvent(new f.w.Event('change',{bubbles:true}));
 const two=f.editor.flush(); const switcher=f.editor.setVideo('next');
 assert.equal(calls.length,1); release(); await Promise.all([one,two,switcher]);
 assert.ok(calls.length>=2); assert.ok(calls.every(c=>c.url.includes('/abc/')));
 assert.equal(calls.at(-1).body.layout_config.aspect,'9:16');
 assert.equal(f.editor.getConfig().mode,'standard');
});

test('late response from old video cannot overwrite the new video',async t=>{
 const f=fixture('index.html');t.after(f.close);let release;
 f.w.fetch=async url=>{
  if(url==='/api/layout-presets') return {ok:true,json:async()=>({presets:[],custom_presets:[]})};
  if(url.includes('/abc/')) { await new Promise(r=>release=r); return {ok:true,json:async()=>({layout:'crop',layout_config:{mode:'pip'}})}; }
  return {ok:true,json:async()=>({layout:'fit',layout_config:{mode:'standard',aspect:'16:9'}})};
 };
 const old=f.editor.load(); await Promise.resolve(); await f.editor.setVideo('next'); release(); await old;
 assert.equal(f.editor.getConfig().aspect,'16:9');assert.equal(f.editor.getLayout(),'fit');
});

test('composition defaults compare independent of key order',async t=>{
 const f=fixture('index.html');t.after(f.close);
 assert.equal(f.w.LayoutEditor.same(null,{aspect:'9:16',mode:'standard'}),true);
 assert.equal(f.w.LayoutEditor.same({face:{w:.2,x:.1,h:.3,y:.2}},{face:{y:.2,h:.3,x:.1,w:.2}}),true);
});

test('late face detection cannot alter the next video',async t=>{
 const f=fixture('index.html');t.after(f.close);await f.editor.load();let release;
 f.w.fetch=async(url,opts={})=>{
  if(url.includes('/faces')) {await new Promise(r=>release=r);return {ok:true,json:async()=>({task_id:'old'})};}
  if(url==='/api/layout-presets') return {ok:true,json:async()=>({presets:[],custom_presets:[]})};
  if(opts.method==='PUT') return {ok:true,json:async()=>({})};
  if(url.includes('/layout')) return {ok:true,json:async()=>({layout:'fit',layout_config:{mode:'standard'}})};
  throw new Error('old task must not be requested');
 };
 f.$('.le-detect').click(); await Promise.resolve(); await f.editor.setVideo('next'); release(); await new Promise(r=>setTimeout(r,0));
 assert.equal(f.editor.getConfig().face,null); assert.equal(f.$('.le-candidates').children.length,0);
});

test('destroy and recreate in same container only affects the new video',async t=>{
 const f=fixture('index.html');t.after(f.close);await f.editor.load();
 f.editor.destroy();
 const changed=[];
 const b=f.w.LayoutEditor.create({container:'#layout-editor',videoId:'bbb',layoutSelect:'#layout',onChange:()=>changed.push(1)});
 await b.load(); const before=changed.length; f.$('[data-preset=talk]').click(); await b.flush();
 const writes=f.calls.filter(c=>c.opts.method==='PUT');
 assert.equal(writes.length,1);assert.match(writes[0].url,/\/bbb\/layout$/);assert.equal(changed.length,before+1);
 b.destroy();
});

test('controls cannot change settings while loading or after load failure',async t=>{
 const f=fixture('index.html');t.after(f.close);
 assert.equal(f.$('.le-shell').disabled,true); assert.equal(f.$('#layout').disabled,true);
 f.$('[data-field=aspect]').value='16:9';f.$('[data-field=aspect]').dispatchEvent(new f.w.Event('change',{bubbles:true}));
 assert.equal(f.changes.length,0);
 f.w.fetch=async()=>({ok:false,json:async()=>({detail:'Falhou.'})});
 await assert.rejects(f.editor.load()); assert.equal(f.$('.le-shell').disabled,true); assert.equal(f.$('#layout').disabled,true);
 f.$('[data-field=aspect]').dispatchEvent(new f.w.Event('change',{bubbles:true}));assert.equal(f.changes.length,0);await assert.rejects(f.editor.flush());
});

test('selecting a face preset displays the face controls even after editing gameplay',async t=>{
 const f=fixture('index.html');t.after(f.close);await f.editor.load();
 f.$('[data-preset=talk]').click();f.$('[data-region=gameplay]').click();
 f.$('.le-reset').click(); assert.equal(f.$('[data-region=gameplay]').getAttribute('aria-pressed'),'true');
 f.$('[data-preset=talk]').click();
 assert.equal(f.$('[data-region=face]').getAttribute('aria-pressed'),'true');
 assert.equal(f.$('[data-coordinate=w]').value,'');
});

test('draw, move and resize source region with pointer',async t=>{
 const f=fixture('index.html');t.after(f.close);await f.editor.load();f.$('[data-preset=talk]').click();
 const stage=f.$('.le-stage');stage.getBoundingClientRect=()=>({left:10,top:20,width:200,height:100});
 const ptr=(target,type,x,y)=>target.dispatchEvent(new f.w.MouseEvent(type,{bubbles:true,clientX:x,clientY:y,button:0}));
 ptr(stage,'pointerdown',30,30);ptr(stage,'pointermove',110,70);ptr(stage,'pointerup',110,70);
 const first=f.editor.getConfig().face;assert.ok(Math.abs(first.x-.1)<1e-9);assert.ok(Math.abs(first.y-.1)<1e-9);assert.ok(Math.abs(first.w-.4)<1e-9);assert.ok(Math.abs(first.h-.4)<1e-9);
 ptr(f.$('.le-box'),'pointerdown',50,40);ptr(stage,'pointermove',70,50);ptr(stage,'pointerup',70,50);
 const moved=f.editor.getConfig().face;assert.ok(Math.abs(moved.x-.2)<1e-9);assert.ok(Math.abs(moved.y-.2)<1e-9);
 ptr(f.$('.le-resize'),'pointerdown',130,70);ptr(stage,'pointermove',150,80);ptr(stage,'pointerup',150,80);
 const resized=f.editor.getConfig().face;assert.ok(Math.abs(resized.w-.5)<1e-9);assert.ok(Math.abs(resized.h-.5)<1e-9);
});
