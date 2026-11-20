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
    auto_download:false, auto_upload:false, archive_after_upload:false, provider:"drive", qualities:["final"],
    drive:{configured:true, connected:false, folder_name:"AutoClips", folder_url:""},
    github:{configured:true, connected:false, repository:"", private:true},
    ...clone(options.settings || {})
  };
  const capacity = {
    local:{total_bytes:1e9, used_bytes:4e8, free_bytes:6e8, low_space:false, reserve_bytes:1e8},
    drive:{connected:settings.drive.connected, status:settings.drive.connected ? "ready" : "disconnected",
      total_bytes:2e10, used_bytes:2e9, free_bytes:18e9, max_upload_bytes:5e12, error:null},
    archive:{archived_bytes:0, archived_count:0, pending_count:0},
    ...clone(options.capacity || {})
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
  w.setTimeout = (fn, ms, ...args) => nativeTimeout(fn,
    ms === 1250 || options.fastArchivePoll && ms === 10000 ? 2 : ms, ...args);
  let renderResult = null;
  w.fetch = async (url, init = {}) => {
    const parsed = new URL(url, w.location.origin), pathname = parsed.pathname, method = init.method || "GET";
    const body = init.body ? JSON.parse(init.body) : null;
    const call = {pathname, method, body, signal:init.signal};
    calls.push(call);
    const custom = options.intercept?.(call, {settings, capacity, jobs, exports});
    if (custom !== undefined) return await custom;
    if (pathname === "/api/storage/settings") {
      if (method === "PATCH") Object.assign(settings, body);
      return reply(settings);
    }
    if (pathname === "/api/storage/capacity") return reply(capacity);
    if (pathname === "/api/storage/optimize-drive") {
      Object.assign(settings,{provider:"drive",auto_upload:true,archive_after_upload:true,qualities:["final"]});
      return reply({settings,summary:options.optimizeSummary || {queued:0,already_archived:0,errors:0}});
    }
    if (pathname === "/api/storage/github") {
      settings.github = {configured:true, connected:true, private:true, repository:body.repository};
      return reply(settings);
    }
    if (/^\/api\/storage\/(drive|github)\/disconnect$/.test(pathname)) {
      settings[pathname.split("/")[3]].connected = false;
      settings.auto_upload = false;
      settings.archive_after_upload = false;
      if (pathname.includes("/drive/")) { capacity.drive.connected = false; capacity.drive.status = "disconnected"; }
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
  return {w,$,calls,downloads,jobs,exports,settings,capacity,change,mount,setVideo};
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
    auto_download:true,auto_upload:true,archive_after_upload:false,provider:"drive",qualities:["preview","final"]
  });
  assert.equal(f.downloads.length,0);
  assert.match(f.$("[data-delivery-summary]").textContent,/Google Drive/);
  f.change("[data-storage-field=provider]","github");
  assert.equal(f.$("[data-storage-field=auto_upload]").checked,false);
  assert.equal(f.$("[data-storage-field=auto_upload]").disabled,true);
});

test("capacity separates editing space from the connected account and reports confirmed archives", async t => {
  const f=await fixture(t,{settings:{drive:{configured:true,connected:true}},capacity:{
    local:{total_bytes:500e6,used_bytes:460e6,free_bytes:40e6,low_space:true,reserve_bytes:100e6},
    drive:{connected:true,status:"ready",total_bytes:200e9,used_bytes:170e9,free_bytes:30e9},
    archive:{archived_bytes:1.5e9,archived_count:3,pending_count:2}
  }});
  assert.equal(f.$("[data-local-free]").textContent,"40 MB livres");
  assert.equal(f.$("[data-local-usage]").textContent,"460 MB usados de 500 MB");
  assert.equal(f.$("[data-local-meter]").value,92);
  assert.equal(f.$("[data-capacity-local]").classList.contains("is-warning"),true);
  assert.match(f.$("[data-local-detail]").textContent,/Pouco espaço/);
  assert.equal(f.$("[data-drive-free]").textContent,"30 GB livres");
  assert.equal(f.$("[data-drive-meter]").value,85);
  assert.match(f.$("[data-archive-total]").textContent,/3 finais arquivados no Drive.*1,5 GB ao todo/);
  assert.match(f.$("[data-archive-pending]").textContent,/2 finais aguardando.*atualizado automaticamente/);
  assert.equal(f.$("[data-storage-capacity]").getAttribute("aria-busy"),"false");
  assert.equal(f.calls.some(call=>call.method!=="GET"),false);
});

test("unknown Drive quota is never replaced with fixed free space or an unlimited claim", async t => {
  const f=await fixture(t,{settings:{drive:{configured:true,connected:true}},capacity:{
    drive:{connected:true,status:"ready",total_bytes:null,used_bytes:"2500000000",free_bytes:null}
  }});
  assert.equal(f.$("[data-drive-free]").textContent,"Espaço livre não informado");
  assert.match(f.$("[data-drive-usage]").textContent,/2,5 GB usados.*limite não informado/);
  assert.equal(f.$("[data-drive-meter]").hidden,true);
  assert.doesNotMatch(f.$("[data-capacity-drive]").textContent,/ilimitado|15 GB|0 B livres/i);
  f.capacity.drive.total_bytes=10e9; f.capacity.drive.used_bytes=10e9; f.capacity.drive.free_bytes=0;
  await f.w.StorageDeliveryUI.refreshCapacity();
  assert.equal(f.$("[data-drive-free]").textContent,"0 B livres");
  assert.equal(f.$("[data-capacity-drive]").classList.contains("is-warning"),true);
});

test("Drive quota errors keep connection state separate and escape provider text", async t => {
  const f=await fixture(t,{settings:{drive:{configured:true,connected:true}},capacity:{
    drive:{connected:true,status:"error",error:"Quota indisponível <img src=x onerror=alert(1)>"}
  }});
  assert.equal(f.$("[data-drive-status]").textContent,"Conectado");
  assert.equal(f.$("[data-drive-free]").textContent,"Não foi possível consultar");
  assert.match(f.$("[data-drive-usage]").textContent,/Quota indisponível/);
  assert.equal(f.$("[data-capacity-drive] img"),null);
  assert.equal(f.$("[data-drive-meter]").hidden,true);
});

test("capacity endpoint failure is recoverable and does not invent capacity or disconnect Drive", async t => {
  let fail=true;
  const f=await fixture(t,{settings:{drive:{configured:true,connected:true}},intercept:call=>
    fail && call.pathname==="/api/storage/capacity" ? reply({detail:"Tente novamente."},503) : undefined});
  assert.equal(f.$("[data-local-free]").textContent,"Indisponível no momento");
  assert.match(f.$("[data-capacity-error]").textContent,/Tente novamente/);
  assert.equal(f.$("[data-drive-status]").textContent,"Conectado");
  fail=false; f.$("[data-storage-refresh]").click();
  await until(()=>f.$("[data-capacity-error]").hidden);
  assert.equal(f.$("[data-local-free]").textContent,"600 MB livres");
});

test("disconnected Drive uses its own OAuth action without silently enabling archiving", async t => {
  const f=await fixture(t);
  assert.equal(f.$("[data-drive-connect]").hidden,false);
  assert.equal(new URL(f.$("[data-drive-connect]").href).pathname,"/auth/storage/drive/start");
  assert.equal(f.$("[data-drive-optimize]").hidden,true);
  assert.equal(f.$("[data-storage-field=archive_after_upload]").disabled,true);
  assert.equal(f.$("[data-storage-field=archive_after_upload]").checked,false);
  assert.match(f.$("[data-drive-detail]").textContent,/ChatGPT/);
  assert.equal(f.$("[data-drive-free]").textContent,"Conecte sua conta");
  assert.equal(f.calls.some(call=>call.method!=="GET"),false);
});

test("Drive optimization is one explicit action and renders server settings without duplicate submissions", async t => {
  const pending=deferred();
  const f=await fixture(t,{settings:{auto_download:true,provider:"github",qualities:["preview","final"],
    drive:{configured:true,connected:true},github:{connected:true,repository:"tester/private"}},
    intercept:call=>call.pathname==="/api/storage/optimize-drive" ? pending.promise : undefined});
  const button=f.$("[data-drive-optimize]");
  assert.equal(button.hidden,false);
  assert.equal(f.$("[data-storage-field=archive_after_upload]").checked,false);
  button.click(); button.click();
  assert.equal(f.calls.filter(call=>call.pathname==="/api/storage/optimize-drive").length,1);
  assert.equal(button.disabled,true);
  assert.equal(f.$("[data-storage-save]").disabled,true);
  assert.equal(f.$('[data-storage-disconnect="drive"]').disabled,true);
  Object.assign(f.settings,{provider:"drive",auto_upload:true,archive_after_upload:true,qualities:["final"]});
  pending.resolve(reply({settings:f.settings,summary:{queued:2,already_archived:1,errors:0}}));
  await until(()=>!button.disabled);
  assert.equal(f.$("[data-storage-field=auto_download]").checked,true);
  assert.equal(f.$("[data-storage-field=auto_upload]").checked,true);
  assert.equal(f.$("[data-storage-field=archive_after_upload]").checked,true);
  assert.equal(f.$("[data-storage-field=provider]").value,"drive");
  assert.equal(f.$("[data-storage-field=qualities]").value,"final");
  assert.match(f.$("[data-delivery-feedback]").textContent,/2 arquivos finais na fila.*1 final já estava arquivado/);
  assert.equal(f.calls.filter(call=>call.method==="PATCH").length,0);
  assert.ok(f.calls.filter(call=>call.pathname==="/api/storage/capacity").length>=2);
});

test("failed optimization preserves opt-outs and a partial queue reports attention without fake archiving", async t => {
  let fail=true;
  const f=await fixture(t,{settings:{drive:{configured:true,connected:true}},optimizeSummary:{queued:1,already_archived:0,errors:[{message:"in use"}]},
    intercept:call=>fail && call.pathname==="/api/storage/optimize-drive" ? reply({detail:"Reconecte o Drive."},409) : undefined});
  f.$("[data-drive-optimize]").click();
  await until(()=>!f.$("[data-drive-optimize]").disabled);
  assert.match(f.$("[data-delivery-feedback]").textContent,/Reconecte o Drive/);
  assert.equal(f.$("[data-storage-field=archive_after_upload]").checked,false);
  assert.equal(f.$("[data-storage-field=auto_upload]").checked,false);
  fail=false; f.$("[data-drive-optimize]").click();
  await until(()=>!f.$("[data-drive-optimize]").disabled);
  assert.match(f.$("[data-delivery-feedback]").textContent,/1 arquivo final na fila.*1 arquivo precisa de atenção/);
  assert.match(f.$("[data-delivery-feedback]").textContent,/cópias locais.*preservadas/);
  assert.equal(f.$("[data-archive-total]").hidden,true);
});

test("archiving preferences require Drive and auto-upload and remain an explicit saved opt-in", async t => {
  const f=await fixture(t,{settings:{drive:{configured:true,connected:true},github:{connected:true,repository:"tester/private"}}});
  const checkbox=f.$("[data-storage-field=archive_after_upload]");
  assert.equal(checkbox.disabled,true);
  f.change("[data-storage-field=auto_upload]",true);
  assert.equal(checkbox.disabled,false);
  assert.equal(checkbox.checked,false);
  f.change("[data-storage-field=archive_after_upload]",true);
  assert.equal(f.calls.some(call=>call.method==="PATCH"),false);
  f.$("[data-storage-save]").click();
  await until(()=>f.$("[data-delivery-feedback]").textContent.includes("Automação salva"));
  assert.equal(f.calls.find(call=>call.method==="PATCH").body.archive_after_upload,true);
  f.change("[data-storage-field=provider]","github");
  assert.equal(checkbox.checked,false);
  assert.equal(checkbox.disabled,true);
  f.change("[data-storage-field=provider]","drive");
  f.change("[data-storage-field=archive_after_upload]",true);
  f.change("[data-storage-field=auto_upload]",false);
  assert.equal(checkbox.checked,false);
  assert.equal(checkbox.disabled,true);
});

test("archived deliveries retain successful uploads, destination links and exact download routes", async t => {
  const f=await fixture(t);
  f.jobs.set(A,[
    job({id:"archived",status:"succeeded",error:null,archived:true,archived_at:"2026-11-20T10:02:00Z",url:"https://drive.google.com/file/d/archive/view"}),
    job({id:"kept",status:"succeeded",error:null,archived:false,archive_error:"Arquivo em uso <img src=x onerror=alert(1)>",url:"https://drive.google.com/file/d/kept/view"})
  ]);
  await f.setVideo(A);
  assert.match(f.$("[data-delivery-history]").textContent,/Arquivado no Drive · espaço local liberado/);
  assert.match(f.$("[data-delivery-history]").textContent,/Enviado ao Drive.*cópia local foi preservada/);
  assert.equal(f.w.document.querySelectorAll("[data-delivery-history] .delivery-badge.is-connected").length,2);
  assert.equal(f.w.document.querySelectorAll("[data-delivery-history] a").length,2);
  assert.equal(f.$("[data-delivery-retry]"),null);
  assert.equal(f.$("[data-delivery-history] img"),null);
  assert.equal(f.w.StorageDeliveryUI.downloadUrl(media()),media().url+"&download=1");
});

test("capacity refreshes pending archives and stops after the last confirmation", async t => {
  let reads=0;
  const f=await fixture(t,{fastArchivePoll:true,settings:{drive:{configured:true,connected:true}},
    intercept:(call,{capacity})=>{
      if (call.pathname!=="/api/storage/capacity") return;
      reads++;
      capacity.archive=reads===1 ? {archived_bytes:0,archived_count:0,pending_count:1}
        : {archived_bytes:20e6,archived_count:1,pending_count:0};
      return reply(capacity);
    }});
  await until(()=>reads===2);
  await sleep(20);
  assert.equal(reads,2);
  assert.match(f.$("[data-archive-total]").textContent,/1 final arquivado no Drive.*20 MB/);
});

test("completed uploads keep polling only while archiving is pending, then surface preserved local copies", async t => {
  let reads=0;
  const f=await fixture(t,{fastArchivePoll:true,intercept:call=>{
    if (!call.pathname.endsWith("/deliveries")) return;
    reads++;
    return reply({jobs:[job({status:"succeeded",error:null,archived:false,
      archive_pending:reads===1,archive_error:reads===1 ? null : "Aguarde o fim da edição."})]});
  }});
  await f.setVideo(A);
  await until(()=>reads===2);
  await sleep(20);
  assert.equal(reads,2);
  assert.match(f.$("[data-delivery-history]").textContent,/cópia local foi preservada/);
  assert.equal(f.$("[data-delivery-retry]"),null);
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
