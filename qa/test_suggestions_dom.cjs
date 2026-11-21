/** DOM-level regression checks for Suggestions (mock public APIs). */
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {JSDOM} = require("jsdom");
const fs = require("node:fs");
const path = require("node:path");
const repoRoot = path.resolve(__dirname, "..");
const fixtureData = () => ({
  period: "daily", region: "BR", coverage_days: 1, partial: false,
  status: "ready", updated_at: "2026-11-21T12:00:00+00:00",
  methodology: "Amostra transparente",
  sources: [{id: "twitch", label: "Twitch em português",
             status: "ready", message: "Medições ao vivo."}],
  topics: [{id: "twitch:minecraft", name: "Minecraft", platform: "twitch",
            metric: 90, metric_unit: "sampled_live_viewers"}],
  creators: [{id: "twitch:boalive", platform: "twitch", name: "BoaLive",
              headline: "<img src=x onerror=alert(1)>", topic: "Minecraft",
              metric: 90, metric_unit: "viewers_live",
              url: "https://www.twitch.tv/boalive",
              thumbnail_url: "https://static-cdn.jtvnw.net/previews-ttv/live_user_boalive-440x248.jpg"}],
  videos: []
});
function fixture(data=fixtureData()) {
  const html=fs.readFileSync(path.join(repoRoot,"static/index.html"),"utf8");
  const source=fs.readFileSync(path.join(repoRoot,"static/suggestions.js"),"utf8");
  const dom=new JSDOM(html,{url:"https://autoclips.example/#sources",runScripts:"outside-only"});
  const w=dom.window,calls=[];
  w.fetch=async url=>{ calls.push(String(url));return {ok:true,json:async()=>data}; };
  w.eval(source);
  return {w,calls,$:sel=>w.document.querySelector(sel),close:()=>w.close()};
}
async function flush(){await new Promise(resolve=>setImmediate(resolve));}
test("desktop/mobile navigation and live data render",async t=>{
  const f=fixture();t.after(f.close);
  assert.equal(f.w.document.querySelectorAll('[data-tab="suggestions"]').length,2);
  f.$('[data-tab="suggestions"]').click();
  await flush();
  assert.match(f.$("#suggestions-streamers").textContent,/BoaLive/);
  assert.match(f.$("#suggestions-topics").textContent,/Minecraft/);
  assert.equal(f.calls.length,1);
});
test("thumbnail preview safely overlays editable text",async t=>{
  const f=fixture();t.after(f.close);
  f.$('[data-tab="suggestions"]').click();await flush();
  const card=f.$("#suggestions-streamers .tr-card");
  assert.ok(card);
  assert.match(card.textContent,/<img src=x onerror=alert\(1\)>/);
  assert.equal(card.querySelector("img[onerror]"),null);
  const btn=card.querySelector(".tr-card-thumb");assert.ok(btn);
  btn.click();
  assert.equal(f.$("#suggestion-thumbnail-dialog").hasAttribute("open"),true);
  assert.match(f.$("#suggestions-preview-image").src,/jtvnw.net/);
  const field=f.$("#suggestions-cover-text");
  field.value="MEU NOVO GANCHO";field.dispatchEvent(new f.w.Event("input",{bubbles:true}));
  assert.equal(f.$("#suggestions-cover-overlay").textContent,"MEU NOVO GANCHO");
});
test("incomplete weekly data and platform filters are explicit",async t=>{
  const data=fixtureData();data.period="weekly";data.partial=true;data.coverage_days=1;
  const f=fixture(data);t.after(f.close);
  f.$('[data-tab="suggestions"]').click();await flush();
  f.$('[data-trend-period="weekly"]').click();await flush();
  assert.match(f.$("#suggestions-week-note").textContent,/1 dia/);
  f.$("#suggestions-platform").value="youtube";
  f.$("#suggestions-platform").dispatchEvent(new f.w.Event("change",{bubbles:true}));
  assert.match(f.$("#suggestions-streamers").textContent,/Não há dados/);
});
