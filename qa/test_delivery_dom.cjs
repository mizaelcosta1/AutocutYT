/**
 * Real shared delivery controller and editor integration in a simulated DOM.
 * Provider responses and browser download initiation are mocked: these tests
 * do not claim an actual Drive/GitHub upload or a browser-saved file.
 */
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {JSDOM, VirtualConsole} = require("jsdom");
const dir = path.join(__dirname, "..", "static");
const A = "a".repeat(32), B = "b".repeat(32);
const style = {font_family:"DejaVu Sans", font_size:56, color:"#FFFFFF"};
const script = name => fs.readFileSync(path.join(dir, name), "utf8");
const clone = value => JSON.parse(JSON.stringify(value));
const reply = (value, status = 200) => ({ok:status < 400, status,
  headers:{get:() => "application/json"}, json:async () => clone(value)});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
async function until(check, message = "DOM did not settle") {
  for (let index = 0; index < 250; index++) { if (check()) return; await sleep(8); }
  throw new Error(message);
}
const media = (quality = "final", extra = {}) => {
  const value = {video_id:A, kind:"clip", clip_id:"clip_01", quality, render_id:quality + "-123", ...extra};
  return {...value, url:"/api/videos/" + value.video_id + (value.kind === "timeline" ? "/timeline/rendered/" :
    "/rendered/" + value.clip_id + "/") + value.quality + "?" + (value.kind === "timeline" ? "render_id" : "v") + "=" + value.render_id};
};
const job = (extra = {}) => ({id:"job_1", provider:"drive", video_id:A, kind:"clip", clip_id:"clip_01",
  quality:"final", render_id:"final-123", status:"failed", error:"Falha temporária.", attempts:1,
  created_at:"2026-11-20T10:00:00Z", updated_at:"2026-11-20T10:01:00Z", ...extra});

async function fixture(t, options = {}) {
  const page = options.page || "index", calls = [], downloads = [], jobs = new Map(), exports = {};
  const settings = {
    auto_download:false, auto_upload:false, provider:"drive", qualities:["final"],
    drive:{configured:true, connected:false, folder_name:"AutoClips", folder_url:""},
    github:{configured:true, connected:false, repository:"", private:true},
    ...clone(options.settings || {})
  };
  const dom = new JSDOM(script(page + ".html"), {
    url:"https://studio.example.test/" + (page === "timeline" ? "studio/" + A : ""),
    runScripts:"outside-only", pretendToBeVisual:true, virtualConsole:new VirtualConsole()
  });
  t.after(() => dom.window.close());
  const w = dom.window, $ = selector => w.document.querySelector(selector);
  for (const name of ["load","play","pause"]) w.HTMLMediaElement.prototype[name] = () => Promise.resolve();
  w.HTMLAnchorElement.prototype.click = function() {
    if (this.download) {
      downloads.push({href:this.href, filename:this.download});
      if (options.blockDownload) throw new Error("Download denied by browser");
    }
  };
  w.AutoClipsLive = {refresh(){}, destroyAll(){}};
  const nativeTimeout = w.setTimeout.bind(w);
  w.setTimeout = (fn, ms, ...args) => nativeTimeout(fn, ms === 1250 ? 2 : ms, ...args);
  let renderResult = null;
  w.fetch = async (url, init = {}) => {
    const parsed = new URL(url, w.location.origin), pathname = parsed.pathname, method = init.method || "GET";
    const body = init.body ? JSON.parse(init.body) : null;
    const call = {pathname, method, body, signal:init.signal};
    calls.push(call);
    const custom = options.intercept?.(call, {settings, jobs, exports});
    if (custom !== undefined) return await custom;
    if (pathname === "/api/storage/settings") {
      if (method === "PATCH") Object.assign(settings, body);
      return reply(settings);
    }
    if (pathname === "/api/storage/github") {
      settings.github = {configured:true, connected:true, private:true, repository:body.repository};
      return reply(settings);
    }
    if (/^\/api\/storage\/(drive|github)\/disconnect$/.test(pathname)) {
      settings[pathname.split("/")[3]].connected = false;
      settings.auto_upload = false;
      return reply(settings);
    }
    if (/\/deliveries$/.test(pathname)) {
      const id = pathname.split("/")[3];
      if (method === "GET") return reply({jobs:jobs.get(id) || []});
      const entry = job({video_id:id, ...body, status:"queued", error:null});
      jobs.set(id,[entry]); return reply({job:entry});
    }
    if (/\/api\/storage\/jobs\/[^/]+\/(retry|cancel)$/.test(pathname)) {
      const entry = jobs.get(A)?.find(item => item.id === pathname.split("/")[4]);
      if (entry) { entry.status = pathname.endsWith("/cancel") ? "cancelled" : "queued"; entry.error = null; }
      return reply({job:entry});
    }
    if (pathname === "/api/config") return reply({ffmpeg:true, transcribe_available:true, storage_free_bytes:2e9});
    if (pathname === "/api/videos") return reply({videos:[A,B].map(id => ({
      id, name:id[0] + ".mp4", duration:15, width:1280, height:720, size:1024,
      platform:"local", stream_url:"/api/videos/" + id + "/stream"
    }))});
    if (pathname === "/api/lives") return reply({lives:[]});
    if (pathname.endsWith("/activity")) return reply({activity:null});
    if (pathname.endsWith("/transcript")) return reply({available:false, words:[], state:{}, subtitle_style:style});
    if (pathname.endsWith("/timeline")) return reply({timeline:{schema:1,version:1,source_duration:15,
      segments:[{id:"seg_001",source_in:0,source_out:2}]},edited_duration:2,can_undo:false,can_redo:false});
    if (pathname.endsWith("/timeline/exports")) return reply({exports});
    if (pathname.endsWith("/manifest")) return reply({clips:[{
      id:"clip_01", title:"Test clip", start:0, end:2, status:"ready",
      preview_options:exports.preview && {...exports.preview,burn:exports.preview.subtitles},
      final_options:exports.final && {...exports.final,burn:exports.final.subtitles},
      preview_url:exports.preview ? media("preview",{render_id:exports.preview.render_id}).url : null,
      final_url:exports.final ? media("final",{render_id:exports.final.render_id}).url : null
    }]});
    if (pathname.endsWith("/subtitle-style") || pathname.endsWith("/clips")) return reply({});
    if (pathname.includes("/render/")) {
      const quality = pathname.split("/").pop(), kind = pathname.includes("/timeline/") ? "timeline" : "clip";
      exports[quality] = {version:1, layout:body.layout, subtitles:body.subtitles, subtitle_style:body.subtitle_style,
        render_id:quality + "-123", probe:{duration:2}};
      renderResult = media(quality,{kind});
      return reply({task_id:"render"});
    }
    if (pathname === "/api/tasks/render") return reply({status:"done",result:renderResult});
    throw new Error("Unexpected request: " + method + " " + pathname);
  };
  if (options.editor) w.eval(script("video-activity.js"));
  w.eval(script("delivery.js"));
  if (options.editor) w.eval(script(page === "timeline" ? "timeline.js" : "app.js"));
  await w.StorageDeliveryUI.ready;
  const change = (selector, value) => {
    const element = $(selector);
    if (element.type === "checkbox") element.checked = value;
    else element.value = value;
    element.dispatchEvent(new w.Event("change",{bubbles:true}));
  };
  const mount = input => {
    const node = w.document.createElement("span"); w.document.body.append(node);
    w.StorageDeliveryUI.mountSend(node,input); return node;
  };
  const setVideo = async id => {
    w.StorageDeliveryUI.setVideo(id);
    await until(() => calls.some(call => call.pathname === "/api/videos/" + id + "/deliveries"));
    await sleep(0);
  };
  return {w,$,calls,downloads,jobs,exports,settings,change,mount,setVideo};
}

test("connections start off, unavailable Drive is truthful and existing files do not auto-download", async t => {
  const f = await fixture(t,{settings:{drive:{configured:false,connected:false}}});
  assert.match(f.$("[data-drive-status]").textContent,/Indisponível/);
  assert.equal(f.$("[data-drive-connect]").hidden,true);
  assert.equal(f.$("[data-storage-field=auto_download]").checked,false);
  assert.equal(f.$("[data-storage-field=auto_upload]").disabled,true);
  assert.equal(f.$("[data-storage-field=qualities]").value,"final");
  await f.setVideo(A);
  const output=f.mount(media());
  assert.equal(output.querySelector("button").disabled,true);
  assert.equal(f.downloads.length,0);
  assert.equal(f.calls.filter(call => call.method !== "GET").length,0);
});

test("saving automation requires explicit opt-in and preserves both quality selections", async t => {
  const f=await fixture(t,{settings:{drive:{configured:true,connected:true}}});
  f.change("[data-storage-field=auto_download]",true);
  f.change("[data-storage-field=auto_upload]",true);
  f.change("[data-storage-field=qualities]","both");
  assert.equal(f.calls.filter(call => call.method === "PATCH").length,0);
  f.$("[data-storage-save]").click();
  await until(()=>f.$("[data-delivery-feedback]").textContent.includes("Automação salva"));
  assert.deepEqual(f.calls.find(call=>call.method==="PATCH").body,{
    auto_download:true,auto_upload:true,provider:"drive",qualities:["preview","final"]
  });
  assert.equal(f.downloads.length,0);
  assert.match(f.$("[data-delivery-summary]").textContent,/Google Drive/);
  f.change("[data-storage-field=provider]","github");
  assert.equal(f.$("[data-storage-field=auto_upload]").checked,false);
  assert.equal(f.$("[data-storage-field=auto_upload]").disabled,true);
});

test("failed save keeps server preferences and offers an actionable error", async t => {
  const f=await fixture(t,{intercept:call=>call.method==="PATCH" ? reply({detail:"Servidor indisponível."},503) : undefined});
  f.change("[data-storage-field=auto_download]",true);
  f.$("[data-storage-save]").click();
  await until(()=>f.$("[data-delivery-feedback]").textContent.includes("indisponível"));
  assert.equal(f.$("[data-storage-save]").disabled,false);
  await f.w.StorageDeliveryUI.onRendered(media());
  assert.equal(f.downloads.length,0);
  assert.match(f.$("[data-delivery-summary]").textContent,/Download automático: desativado/);
});

test("GitHub validates token+repository without retaining or echoing the token", async t => {
  const f=await fixture(t), token="github_test_only_fake_secret_123";
  f.$("[data-github-form] [name=repository]").value="tester/private-videos";
  f.$("[data-github-form] [name=token]").value=token;
  f.$("[data-github-submit]").click();
  await until(()=>f.$("[data-github-status]").textContent.includes("Conectado"));
  assert.deepEqual(f.calls.find(call=>call.pathname==="/api/storage/github").body,{token,repository:"tester/private-videos"});
  assert.equal(f.$("[name=token]").value,"");
  assert.equal(f.w.document.body.innerHTML.includes(token),false);
  assert.equal(f.w.localStorage.length,0);
  assert.equal(f.w.sessionStorage.length,0);
  assert.equal(f.$("[data-storage-field=auto_upload]").checked,false);
  assert.equal(f.$("[data-github-repository]").href,"https://github.com/tester/private-videos");
});

test("GitHub rejection does not echo a token even if an upstream error repeats it", async t => {
  const token="github_test_only_no_echo";
  const f=await fixture(t,{intercept:call=>call.pathname==="/api/storage/github" ? reply({detail:token},400) : undefined});
  f.$("[name=repository]").value="tester/public-repo";
  f.$("[name=token]").value=token;
  f.$("[data-github-submit]").click();
  await until(()=>f.$("[data-delivery-feedback]").textContent.includes("Não foi possível"));
  assert.equal(f.w.document.body.innerHTML.includes(token),false);
  assert.equal(f.$("[name=token]").value,"");
  assert.equal(f.$("[data-github-submit]").disabled,false);
  assert.match(f.$("[data-github-status]").textContent,/Não conectado/);
});

test("completion downloads the exact final once, including recovered task events", async t => {
  const f=await fixture(t,{settings:{auto_download:true}});
  await f.setVideo(A); f.mount(media());
  assert.equal(f.downloads.length,0);
  await f.w.StorageDeliveryUI.onRendered(media());
  f.w.dispatchEvent(new f.w.CustomEvent("video-task-restored",{detail:{id:A,result:media(),error:""}}));
  await f.w.StorageDeliveryUI.onRendered(media());
  assert.equal(f.downloads.length,1);
  const url=new URL(f.downloads[0].href);
  assert.equal(url.pathname,"/api/videos/"+A+"/rendered/clip_01/final");
  assert.equal(url.searchParams.get("v"),"final-123");
  assert.equal(url.searchParams.get("download"),"1");
  assert.match(f.$("[data-download-fallbacks]").textContent,/Se não iniciar/);
  assert.equal(f.calls.some(call=>call.pathname.endsWith("/deliveries")&&call.method==="POST"),false);
  // A fresh controller in the same browser session still sees the completed ID.
  f.w.eval(script("delivery.js")); await f.w.StorageDeliveryUI.ready;
  await f.w.StorageDeliveryUI.onRendered(media());
  assert.equal(f.downloads.length,1);
});

test("quality filter and prepared artifacts only download opted-in previews", async t => {
  const f=await fixture(t,{settings:{auto_download:true,qualities:["preview"]}});
  await f.w.StorageDeliveryUI.onRendered(media("final"));
  await f.w.StorageDeliveryUI.onRendered({artifacts:[
    media("preview"),media("preview",{clip_id:"clip_02",render_id:"preview-456"}),media("final")
  ]});
  assert.equal(f.downloads.length,2);
  assert.ok(f.downloads.every(item=>new URL(item.href).pathname.endsWith("/preview")));
});

test("render completion honors automation enabled or disabled in another tab", async t => {
  const f=await fixture(t);
  assert.equal(f.$("[data-storage-field=auto_download]").checked,false);
  // These edits affect the server response, not this editor's cached state.
  f.settings.auto_download=true;
  await f.w.StorageDeliveryUI.onRendered(media());
  assert.equal(f.downloads.length,1);
  assert.equal(f.$("[data-storage-field=auto_download]").checked,true);
  f.settings.auto_download=false;
  await f.w.StorageDeliveryUI.onRendered(media("final",{render_id:"final-456"}));
  assert.equal(f.downloads.length,1);
  assert.equal(f.$("[data-storage-field=auto_download]").checked,false);
});

test("returning to the editor refreshes preferences and coalesces a simultaneous completion", async t => {
  const pending=deferred(); let defer=false;
  const f=await fixture(t,{intercept:call=>defer && call.pathname==="/api/storage/settings" ? pending.promise : undefined});
  const previous=f.calls.filter(call=>call.pathname==="/api/storage/settings").length;
  f.settings.auto_download=true; defer=true;
  Object.defineProperty(f.w.document,"hidden",{value:false,configurable:true});
  f.w.document.dispatchEvent(new f.w.Event("visibilitychange"));
  const completion=f.w.StorageDeliveryUI.onRendered(media());
  assert.equal(f.calls.filter(call=>call.pathname==="/api/storage/settings").length,previous+1);
  pending.resolve(reply(f.settings));
  await completion;
  assert.equal(f.downloads.length,1);
  assert.match(f.$("[data-delivery-summary]").textContent,/Download automático: ativado/);
});

test("failed preference refresh never downloads using an old opt-in and remains recoverable", async t => {
  let offline=false;
  const f=await fixture(t,{settings:{auto_download:true},intercept:call=>
    offline && call.pathname==="/api/storage/settings" ? reply({detail:"Offline."},503) : undefined});
  offline=true;
  await f.w.StorageDeliveryUI.onRendered(media());
  assert.equal(f.downloads.length,0);
  assert.match(f.$("[data-delivery-feedback]").textContent,/não foi iniciado/);
  assert.equal(f.w.sessionStorage.length,0);
  offline=false;
  await f.w.StorageDeliveryUI.onRendered(media());
  assert.equal(f.downloads.length,1);
});

test("background preference refresh preserves unsaved automation choices", async t => {
  const f=await fixture(t);
  f.change("[data-storage-field=qualities]","both");
  await f.w.StorageDeliveryUI.onRendered(media());
  assert.equal(f.$("[data-storage-field=qualities]").value,"both");
  assert.match(f.$("[data-storage-saved]").textContent,/ainda não salvas/);
  assert.equal(f.downloads.length,0);
});

test("foreign, wrong-version and incomplete artifacts never initiate a download or upload", async t => {
  const f=await fixture(t,{settings:{auto_download:true,drive:{configured:true,connected:true}}});
  for (const value of [
    {...media(),url:"https://attacker.example/video.mp4"},
    {...media(),url:media().url.replace("final-123","other-version")},
    {...media(),url:"javascript:alert(1)"},
    {...media(),render_id:""}, {...media(),video_id:"../other"}
  ]) {
    assert.equal(f.w.StorageDeliveryUI.downloadUrl(value),"");
    await f.w.StorageDeliveryUI.onRendered(value);
    await f.w.StorageDeliveryUI.send(value);
  }
  assert.equal(f.downloads.length,0);
  assert.equal(f.calls.filter(call=>call.method==="POST").length,0);
});

test("blocked automatic download keeps an explicit manual link and avoids repeated attempts", async t => {
  const f=await fixture(t,{settings:{auto_download:true},blockDownload:true});
  await f.setVideo(A);
  await f.w.StorageDeliveryUI.onRendered(media());
  await f.w.StorageDeliveryUI.onRendered(media());
  assert.equal(f.downloads.length,1);
  assert.match(f.$("[data-download-fallbacks]").textContent,/não iniciou/);
  const link=f.$("[data-download-fallbacks] a");
  assert.equal(new URL(link.href).searchParams.get("v"),"final-123");
  assert.ok(link.download.endsWith(".mp4"));
});

test("manual send uses the selected provider and exact render ID, without double submission", async t => {
  const pending=deferred();
  const f=await fixture(t,{settings:{provider:"github",github:{connected:true,repository:"tester/private",private:true}},
    intercept:call=>call.pathname.endsWith("/deliveries")&&call.method==="POST" ? pending.promise : undefined});
  await f.setVideo(A);
  const output=f.mount(media()), button=output.querySelector("button");
  button.click(); button.click();
  const posts=f.calls.filter(call=>call.pathname.endsWith("/deliveries")&&call.method==="POST");
  assert.equal(posts.length,1);
  assert.deepEqual(posts[0].body,{kind:"clip",clip_id:"clip_01",quality:"final",render_id:"final-123",provider:"github"});
  assert.equal(button.disabled,true);
  pending.resolve(reply({job:job({provider:"github",status:"queued"})}));
  await until(()=>!button.disabled);
  assert.match(f.$("[data-delivery-feedback]").textContent,/Envio solicitado/);
});

test("manual send failure releases the control and explains a stale render", async t => {
  const f=await fixture(t,{settings:{drive:{configured:true,connected:true}},
    intercept:call=>call.pathname.endsWith("/deliveries")&&call.method==="POST" ? reply({detail:"Esta versão mudou. Gere um novo arquivo."},409) : undefined});
  const output=f.mount(media()), button=output.querySelector("button");
  button.click();
  await until(()=>f.$("[data-delivery-feedback]").textContent.includes("versão mudou"));
  assert.equal(button.disabled,false);
  assert.equal(f.downloads.length,0);
});

test("history shows real progress, a safe destination link and retry/cancel states", async t => {
  const f=await fixture(t);
  f.jobs.set(A,[
    job(),
    job({id:"done",status:"succeeded",error:null,url:"https://drive.google.com/file/d/example/view"}),
    job({id:"bad",status:"succeeded",error:"<img src=x onerror=alert(1)>",url:"javascript:alert(1)"}),
    job({id:"sending",status:"running",error:null,progress:37,bytes_sent:37,total_bytes:100})
  ]);
  await f.setVideo(A);
  assert.equal(f.$("[data-delivery-history] progress").value,37);
  assert.equal(f.$("[data-delivery-history] img"),null);
  assert.equal(f.w.document.querySelectorAll("[data-delivery-history] a").length,1);
  assert.equal(f.w.document.querySelectorAll("[data-delivery-retry]").length,1);
  f.$("[data-delivery-retry]").click();
  await until(()=>f.jobs.get(A)[0].status==="queued" && !f.$("[data-delivery-retry]"));
  assert.match(f.$("[data-delivery-history]").textContent,/Na fila/);
  f.$("[data-delivery-cancel]").click();
  await until(()=>f.jobs.get(A)[0].status==="cancelled" && !f.$("[data-delivery-cancel]"));
  assert.match(f.$("[data-delivery-history]").textContent,/Cancelado/);
  assert.equal(f.$("[data-delivery-retry]"),null);
});

test("history request racing with a video switch cannot replace the selected project's jobs", async t => {
  const pending=deferred();
  const f=await fixture(t,{intercept:call=>call.pathname==="/api/videos/"+A+"/deliveries" ? pending.promise : undefined});
  f.w.StorageDeliveryUI.setVideo(A);
  f.jobs.set(B,[job({id:"video_b",video_id:B,clip_id:"clip_B"})]);
  await f.setVideo(B);
  pending.resolve(reply({jobs:[job({clip_id:"clip_A"})]}));
  await sleep(10);
  assert.match(f.$("[data-delivery-history]").textContent,/clip_B/);
  assert.doesNotMatch(f.$("[data-delivery-history]").textContent,/clip_A/);
});

test("failed history refresh retains known jobs and surfaces a recoverable message", async t => {
  let fail=false;
  const f=await fixture(t,{intercept:call=>fail&&call.pathname.endsWith("/deliveries") ? reply({detail:"Offline."},503) : undefined});
  f.jobs.set(A,[job()]);
  await f.setVideo(A);
  fail=true; await f.w.StorageDeliveryUI.refreshJobs();
  assert.match(f.$("[data-delivery-history]").textContent,/Offline/);
  assert.match(f.$("[data-delivery-history]").textContent,/clip_01/);
  fail=false; f.$("[data-delivery-refresh]").click();
  await until(()=>!f.$("[data-delivery-history]").textContent.includes("Offline"));
});

test("live finishing between polls downloads new previews without touching old completed lives", async t => {
  let finished=false, taskReads=0, failOnce=true;
  const f=await fixture(t,{editor:true,settings:{auto_download:true,qualities:["preview"]},intercept:call=>{
    if (call.pathname==="/api/lives") return reply({lives:[
      {id:"old",video_id:B,status:"completed",analysis_task_id:"old-task",analysis_status:"done"},
      {id:"current",video_id:A,status:finished?"completed":"recording",
        analysis_task_id:finished?"new-task":null,analysis_status:finished?"done":null}
    ]});
    if (call.pathname==="/api/live/current/stop") { finished=true; return reply({}); }
    if (call.pathname==="/api/tasks/new-task") {
      taskReads++;
      if (failOnce) { failOnce=false; return reply({detail:"Temporary network error"},503); }
      return reply({status:"done",result:{artifacts:[media("preview")]}});
    }
  }});
  await until(()=>f.calls.some(call=>call.pathname==="/api/lives"));
  await sleep(0);
  assert.equal(f.downloads.length,0);
  await f.w._stopLive("current");
  await until(()=>taskReads===1);
  assert.equal(f.downloads.length,0);
  await f.w._stopLive("current");
  await until(()=>f.downloads.length===1);
  await f.w._stopLive("current");
  await sleep(10);
  assert.equal(f.downloads.length,1);
  assert.equal(f.calls.some(call=>call.pathname==="/api/tasks/old-task"),false);
});

for (const page of ["index","timeline"]) test(page + ": editor render completion exposes downloads while preserving final approval", async t => {
  const f=await fixture(t,{page,editor:true,settings:{auto_download:true,qualities:["preview","final"]}});
  if (page === "index") {
    await until(()=>f.w._openVideoById);
    f.w._openVideoById(A);
    await until(()=>f.$(".c-title"));
    await f.w._renderClip("clip_01","final");
    assert.equal(f.calls.some(call=>call.pathname.endsWith("/render/final")),false);
    await f.w._renderClip("clip_01","preview");
    await until(()=>f.$(".c-preview-approve"));
    assert.equal(f.$(".c-render-final").disabled,true);
    assert.match(f.$(".delivery-download").textContent,/prévia 720p/);
    f.$(".c-preview-approve").checked=true; f.w._approveClipPreview("clip_01");
    await f.w._renderClip("clip_01","final");
    await until(()=>f.w.document.querySelectorAll(".delivery-download").length===2);
    assert.ok(!new URL(f.$(".clip-media video").src).searchParams.has("download"));
  } else {
    await until(()=>f.calls.some(call=>call.pathname.endsWith("/timeline/exports")));
    assert.equal(f.$("#render-final").disabled,true);
    f.change("#approve-selection",true); f.$("#render-preview").click();
    await until(()=>!f.$("#approve-preview").disabled);
    assert.equal(f.$("#render-final").disabled,true);
    assert.equal(new URL(f.$("#preview-link").href).searchParams.get("download"),"1");
    assert.ok(!new URL(f.$("#preview-video").src).searchParams.has("download"));
    f.change("#approve-preview",true); f.$("#render-final").click();
    await until(()=>!f.$("#final-link").classList.contains("hidden"));
    assert.equal(new URL(f.$("#final-link").href).searchParams.get("render_id"),"final-123");
  }
  await until(()=>f.downloads.length===2);
  assert.deepEqual(f.downloads.map(item=>new URL(item.href).pathname.split("/").pop()),["preview","final"]);
  const final=f.calls.find(call=>call.pathname.endsWith("/render/final"));
  assert.equal(final.body.approved_preview,true);
  assert.equal(final.body.approved_preview_id,"preview-123");
  assert.equal(f.calls.some(call=>call.pathname.endsWith("/deliveries")&&call.method==="POST"),false);
});
