/** Simulated DOM tests, not browser visual QA. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const path=require('node:path');
const repo=path.resolve(__dirname,'..');
function fixture(page, extra = {}) {
 const dom=new JSDOM(fs.readFileSync(path.join(repo,'static',page),'utf8'),{url:'https://studio.example/',runScripts:'outside-only'});
 const w=dom.window, calls=[], changes=[], subtitle=[], canvasCalls=[];
 const context = {measureText:text=>({width:text.length*30})};
 for (const method of ['scale','fillRect','drawImage','save','restore','strokeText','fillText'])
   context[method]=(...args)=>canvasCalls.push({method,args,fillStyle:context.fillStyle,font:context.font});
 w.HTMLCanvasElement.prototype.getContext=()=>context;
 const options={mode:'split_caption',aspect:'9:16',face:null,gameplay:null,split_ratio:.34,pip_position:'top-right',pip_scale:.28,game_fit:'contain',caption_position:'middle'};
 const reply=body=>({ok:true,json:async()=>body});
 const catalog={presets:[{id:'talk',name:'Rosto e jogo',layout:'fit',layout_config:options,subtitles:true}],custom_presets:[]};
 w.fetch=async(url,opts={})=>{calls.push({url,opts}); if(url==='/api/layout-presets') return reply(catalog); if(url.endsWith('/layout') && opts.method==='PUT') return reply({ok:true}); if(url.endsWith('/layout')) return reply({layout:'blur',layout_config:{...options,mode:'standard'}}); throw new Error(url);};
 w.eval(fs.readFileSync(path.join(repo,'static/video-activity.js'),'utf8'));
 w.eval(fs.readFileSync(path.join(repo,'static/layout-editor.js'),'utf8'));
 const editor=w.LayoutEditor.create({container:'#layout-editor',videoId:'abc',layoutSelect:page==='index.html'?'#layout':'#export-layout',onChange:()=>changes.push(1),onPresetSubtitles:v=>subtitle.push(v),...extra});
 const loadFrame=(width=1920,height=1080)=>{
   const image=w.document.querySelector('.le-frame');
   Object.defineProperty(image,'naturalWidth',{value:width,configurable:true});
   Object.defineProperty(image,'naturalHeight',{value:height,configurable:true});
   image.dispatchEvent(new w.Event('load'));
 };
 return {w,editor,calls,changes,subtitle,options,catalog,canvasCalls,loadFrame,$:s=>w.document.querySelector(s),close:()=>w.close()};
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
 const f=fixture('index.html');t.after(f.close);await f.editor.load();f.loadFrame();f.$('[data-preset=talk]').click();
 const stage=f.$('.le-stage');stage.getBoundingClientRect=()=>({left:10,top:20,width:200,height:100});
 const ptr=(target,type,x,y)=>target.dispatchEvent(new f.w.MouseEvent(type,{bubbles:true,clientX:x,clientY:y,button:0}));
 ptr(stage,'pointerdown',30,30);ptr(stage,'pointermove',110,70);ptr(stage,'pointerup',110,70);
 const first=f.editor.getConfig().face;assert.ok(Math.abs(first.x-.1)<1e-9);assert.ok(Math.abs(first.y-.1)<1e-9);assert.ok(Math.abs(first.w-.4)<1e-9);assert.ok(Math.abs(first.h-.4)<1e-9);
 ptr(f.$('.le-box'),'pointerdown',50,40);ptr(stage,'pointermove',70,50);ptr(stage,'pointerup',70,50);
 const moved=f.editor.getConfig().face;assert.ok(Math.abs(moved.x-.2)<1e-9);assert.ok(Math.abs(moved.y-.2)<1e-9);
 ptr(f.$('.le-resize'),'pointerdown',130,70);ptr(stage,'pointermove',150,80);ptr(stage,'pointerup',150,80);
 const resized=f.editor.getConfig().face;assert.ok(Math.abs(resized.w-.5)<1e-9);assert.ok(Math.abs(resized.h-.5)<1e-9);
});

test('switching built-in models preserves chosen regions while custom models restore their own regions',async t=>{
 const f=fixture('index.html');t.after(f.close);
 f.catalog.presets.push({id:'pip',name:'Janela lateral',layout:'fit',layout_config:{mode:'pip',aspect:'16:9'}});
 const ownFace={x:.6,y:.1,w:.2,h:.3};
 f.catalog.custom_presets.push({id:'mine',name:'Meu enquadramento',layout:'fit',layout_config:{mode:'pip',face:ownFace,gameplay:null}});
 await f.editor.load();f.$('[data-preset=talk]').click();f.$('.le-reset').click();
 f.$('[data-region=gameplay]').click();f.$('.le-reset').click();
 const before=JSON.parse(JSON.stringify(f.editor.getConfig()));
 f.$('[data-preset=pip]').click();
 const next=JSON.parse(JSON.stringify(f.editor.getConfig()));
 assert.deepEqual(next.face,before.face);assert.deepEqual(next.gameplay,before.gameplay);assert.equal(next.aspect,'16:9');
 assert.equal(f.$('.le-current-name').textContent,'Janela lateral');
 f.$('[data-preset=mine]').click();
 assert.deepEqual(JSON.parse(JSON.stringify(f.editor.getConfig().face)),ownFace);assert.equal(f.editor.getConfig().gameplay,null);
});

test('composition frame uses real source crops, centered captions and the selected contain or cover geometry',async t=>{
 const f=fixture('index.html',{getSubtitles:()=>true,getSubtitleStyle:()=>({font_family:'DejaVu Serif',font_size:64,color:'#00EEFF'}),getSubtitleText:()=> 'Boa jogada'});t.after(f.close);
 await f.editor.load();f.loadFrame();f.$('[data-preset=talk]').click();
 for(const [key,value] of Object.entries({x:70,y:5,w:20,h:20})) f.$(`[data-coordinate=${key}]`).value=value;
 f.$('[data-coordinate=x]').dispatchEvent(new f.w.Event('change',{bubbles:true}));
 f.canvasCalls.length=0;f.editor.refreshPreview();
 const image=f.$('.le-frame'), draws=f.canvasCalls.filter(c=>c.method==='drawImage');
 assert.equal(draws.length,2);assert.equal(draws[0].args[0],image);assert.deepEqual(draws[0].args.slice(1,5),[1344,54,384,216]);
 assert.ok(draws[0].args[6]>=0 && draws[0].args[6]<652);assert.ok(draws[1].args[6]>844);
 const band=f.canvasCalls.find(c=>c.method==='fillRect' && c.fillStyle==='#121820');assert.deepEqual(band.args,[0,652,1080,192]);
 const caption=f.canvasCalls.find(c=>c.method==='fillText');assert.deepEqual(caption.args,['Boa jogada',540,748,952]);assert.equal(caption.fillStyle,'#00EEFF');assert.match(caption.font,/64px "DejaVu Serif"/);
 const fit=f.$('[data-field=game_fit]');fit.value='cover';fit.dispatchEvent(new f.w.Event('change',{bubbles:true}));
 f.canvasCalls.length=0;f.editor.refreshPreview();
 const cover=f.canvasCalls.filter(c=>c.method==='drawImage')[1];assert.deepEqual(cover.args.slice(5),[0,844,1080,1076]);assert.ok(cover.args[1]>0 && cover.args[3]<1920);
 assert.equal(f.$('.le-composition').width,432);assert.equal(f.$('.le-composition').height,768);
 assert.match(f.$('.le-preview-help').textContent,/amostra de estilo/);
});

test('horizontal picture-in-picture follows the selected corner and size without requesting another frame',async t=>{
 const f=fixture('timeline.html');t.after(f.close);
 f.catalog.presets.push({id:'pip',name:'PiP',layout:'fit',layout_config:{mode:'pip',aspect:'16:9',face:{x:0,y:0,w:1,h:1},pip_scale:.3,pip_position:'bottom-left',game_fit:'cover'}});
 await f.editor.load();f.loadFrame();const source=f.$('.le-frame').src,requests=f.calls.length;
 f.$('[data-preset=pip]').click();f.canvasCalls.length=0;f.editor.refreshPreview();
 const faceBox=f.canvasCalls.filter(c=>c.method==='fillRect').at(-1);
 assert.deepEqual(faceBox.args,[32,616,576,432]);
 assert.equal(f.$('.le-composition').width,768);assert.equal(f.$('.le-composition').height,432);
 assert.equal(f.$('.le-frame').src,source);assert.equal(f.calls.length,requests);
 const position=f.$('[data-field=pip_position]');position.value='top-right';position.dispatchEvent(new f.w.Event('change',{bubbles:true}));
 assert.deepEqual(f.canvasCalls.filter(c=>c.method==='fillRect').at(-1).args,[1312,32,576,432]);
});

test('a missing face or unavailable frame is explained and updating the frame uses the player time',async t=>{
 const f=fixture('index.html',{getTime:()=>42.5});t.after(f.close);await f.editor.load();
 f.$('[data-preset=talk]').click();f.loadFrame();assert.match(f.$('.le-preview-state').textContent,/Selecione a área do rosto/);
 f.$('.le-frame').dispatchEvent(new f.w.Event('error'));assert.match(f.$('.le-preview-state').textContent,/Não foi possível/);
 f.$('.le-preview-frame').click();assert.match(f.$('.le-frame').src,/at=42.5$/);assert.match(f.$('.le-preview-meta').textContent,/00:42/);
 f.loadFrame();f.$('.le-reset').click();assert.equal(f.$('.le-preview-state').hidden,true);
});

test('updating from an ended player selects a decodable frame inside the video',async t=>{
 const f=fixture('index.html',{getTime:()=>42.5,getDuration:()=>42.5});t.after(f.close);await f.editor.load();
 f.$('.le-preview-frame').click();assert.match(f.$('.le-frame').src,/at=42.4$/);
});

test('a render reservation prevents pointer edits and cancels an in-progress drag without changing its region',async t=>{
 const f=fixture('index.html');t.after(f.close);await f.editor.load();f.loadFrame();f.$('[data-preset=talk]').click();f.$('.le-reset').click();
 const stage=f.$('.le-stage');stage.getBoundingClientRect=()=>({left:0,top:0,width:200,height:100});
 const pointer=(type,x,y)=>stage.dispatchEvent(new f.w.MouseEvent(type,{bubbles:true,clientX:x,clientY:y,button:0}));
 const before=JSON.stringify(f.editor.getConfig().face);
 pointer('pointerdown',10,10);pointer('pointermove',100,80);assert.notEqual(JSON.stringify(f.editor.getConfig().face),before);
 const job=f.w.VideoActivity.reserve('abc','render-preview');assert.equal(JSON.stringify(f.editor.getConfig().face),before);
 pointer('pointerup',100,80);pointer('pointerdown',20,20);pointer('pointermove',150,80);pointer('pointerup',150,80);
 assert.equal(JSON.stringify(f.editor.getConfig().face),before);assert.equal(stage.getAttribute('aria-disabled'),'true');
 f.w.VideoActivity.release(job);assert.equal(stage.getAttribute('aria-disabled'),'false');
 pointer('pointerdown',20,20);pointer('pointermove',150,80);pointer('pointerup',150,80);assert.notEqual(JSON.stringify(f.editor.getConfig().face),before);
});

test('applying a valid model clears a rejected coordinate edit instead of leaving render permanently blocked',async t=>{
 const f=fixture('index.html');t.after(f.close);await f.editor.load();f.$('[data-preset=talk]').click();f.$('.le-reset').click();
 const x=f.$('[data-coordinate=x]');x.value=50;x.dispatchEvent(new f.w.Event('change',{bubbles:true}));
 await assert.rejects(f.editor.flush(),/Corrija/);f.$('[data-preset=talk]').click();await f.editor.flush();
 assert.equal(f.$('.le-error').textContent,'');assert.equal(f.editor.getConfig().face.x,0);
});

test('0.8.0: supplied black coral white palette is the active studio theme',()=>{
 const appCss=fs.readFileSync(path.join(repo,'static/app.css'),'utf8');
 const timelineCss=fs.readFileSync(path.join(repo,'static/timeline.css'),'utf8');
 const layoutCss=fs.readFileSync(path.join(repo,'static/layout-editor.css'),'utf8');
 assert.match(appCss,/--bg:\s*#080808/i);
 assert.match(appCss,/--accent:\s*#FF422A/i);
 assert.match(timelineCss,/--cyan:\s*#FF422A/i);
 assert.match(layoutCss,/#FF422A/i);
});

for(const page of ['index.html','timeline.html']) test(`${page}: legacy framing selector is hidden for composed presets`,async t=>{
 const f=fixture(page);t.after(f.close);await f.editor.load();
 assert.equal(f.$(page==='index.html'?'#layout':'#export-layout').hidden,false);
 f.$('.le-open-presets').click();
 f.$('[data-preset="talk"]').click();
 assert.equal(f.$(page==='index.html'?'#layout':'#export-layout').hidden,true);
});
