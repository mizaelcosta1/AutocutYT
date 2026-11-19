/**
 * Component behavior in jsdom with controlled HLS events; not browser/visual QA.
 * Run: NODE_PATH=/path/to/jsdom/node_modules node --test qa/test_live_preview_dom.cjs
 * Playback itself is exercised by the FFmpeg tests and a real browser separately.
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');
const script = fs.readFileSync(path.join(__dirname, '../static/live-preview.js'), 'utf8');

function fixture(options = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => errors.push(error.message));
  const dom = new JSDOM('<div id="lives-list"></div><div id="lives-empty">Nenhuma gravação</div>', {
    url: 'https://studio.example.test/', runScripts: 'outside-only', virtualConsole,
  });
  const win = dom.window;
  const instances = [];
  const timers = new Map();
  const calls = { play: 0, pause: 0, load: 0, stop: [], open: [] };
  let timerId = 0;
  win.setTimeout = (fn) => { const id = ++timerId; timers.set(id, fn); return id; };
  win.clearTimeout = (id) => timers.delete(id);
  win.HTMLMediaElement.prototype.play = function () { calls.play++; return Promise.resolve(); };
  win.HTMLMediaElement.prototype.pause = function () { calls.pause++; };
  win.HTMLMediaElement.prototype.load = function () { calls.load++; };
  win.HTMLMediaElement.prototype.canPlayType = function () { return options.native ? 'probably' : ''; };
  class FakeHls {
    static Events = { MANIFEST_PARSED: 'parsed', ERROR: 'error' };
    static ErrorTypes = { NETWORK_ERROR: 'networkError', MEDIA_ERROR: 'mediaError' };
    static isSupported() { return options.hlsSupported !== false; }
    constructor(config) {
      this.config = config;
      this.events = new Map();
      this.sources = [];
      this.loads = [];
      this.recoveries = 0;
      this.destroyed = 0;
      instances.push(this);
    }
    on(name, callback) { this.events.set(name, callback); }
    emit(name, data) { this.events.get(name)?.(name, data); }
    loadSource(url) { this.sources.push(url); }
    attachMedia(video) { this.video = video; }
    startLoad(position) { this.loads.push(position); }
    recoverMediaError() { this.recoveries++; }
    destroy() { this.destroyed++; }
  }
  if (!options.noHls) win.Hls = FakeHls;
  win._stopLive = options.stop || (async (id) => calls.stop.push(id));
  win._openVideoById = async (id) => calls.open.push(id);
  win.eval(script);
  const $ = (selector) => win.document.querySelector(selector);
  return {
    win, dom, $, instances, timers, calls, errors, FakeHls,
    refresh(lives) { win.AutoClipsLive.refresh(lives); },
    button(label) { return [...win.document.querySelectorAll('button')].find((b) => b.textContent === label); },
    flushRetry() { const [id, callback] = timers.entries().next().value || []; if (callback) { timers.delete(id); callback(); } },
    close() { win.AutoClipsLive.destroyAll(); dom.window.close(); assert.deepEqual(errors, []); },
  };
}

function live(overrides = {}) {
  return { id: 'live-a', platform: 'youtube', status: 'recording', bytes: 1048576,
    preview_ready: true, preview_has_video: true,
    preview_url: '/api/live/live-a/preview/index.m3u8', ...overrides };
}

test('waiting state makes no player request until real preview media is ready', (t) => {
  const f = fixture(); t.after(() => f.close());
  f.refresh([]);
  assert.equal(f.$('#lives-empty').hidden, false);
  f.refresh([live({ status: 'starting', preview_ready: false })]);
  assert.equal(f.$('#lives-empty').hidden, true);
  assert.equal(f.instances.length, 0);
  assert.equal(f.$('video').hidden, true);
  assert.match(f.$('.live-preview-status').textContent, /primeiros segundos/);
  assert.equal(f.$('.live-badge').textContent, 'Conectando');
  assert.equal(f.button('Encerrar gravação').disabled, false);
});

test('polling preserves video, HLS instance, playback position and user audio controls', (t) => {
  const f = fixture(); t.after(() => f.close());
  f.refresh([live()]);
  const video = f.$('video');
  const player = f.instances[0];
  player.emit(f.FakeHls.Events.MANIFEST_PARSED);
  video.currentTime = 7;
  video.muted = false;
  video.volume = 0.35;
  f.refresh([live({ bytes: 2097152 })]);
  f.refresh([live({ bytes: 3145728, preview_ready: false })]);
  assert.equal(f.$('video'), video);
  assert.equal(f.instances.length, 1);
  assert.equal(player.destroyed, 0);
  assert.equal(video.currentTime, 7);
  assert.equal(video.muted, false);
  assert.equal(video.volume, 0.35);
  assert.equal(f.calls.play, 1);
  assert.match(f.$('.live-meta').textContent, /3 MB/);
  assert.deepEqual(player.sources, ['https://studio.example.test/api/live/live-a/preview/index.m3u8']);
  const xhr = {};
  player.config.xhrSetup(xhr);
  assert.equal(xhr.withCredentials, true);
  assert.equal(video.controls, true);
  assert.equal(video.playsInline, true);
  assert.equal(f.$('.live-preview-placeholder').hidden, true);
});

test('multiple recordings stay keyed, ordered and independent', (t) => {
  const f = fixture(); t.after(() => f.close());
  f.refresh([live()]);
  const firstVideo = f.$('video');
  const other = live({ id: 'live-b', platform: 'twitch', preview_url: '/api/live/live-b/preview/index.m3u8' });
  f.refresh([other, live()]);
  assert.equal(f.instances.length, 2);
  assert.equal(f.win.document.querySelectorAll('video')[1], firstVideo);
  assert.equal(f.instances[0].destroyed, 0);
  f.refresh([other]);
  assert.equal(f.instances[0].destroyed, 1);
  assert.equal(f.instances[1].destroyed, 0);
  assert.equal(f.win.document.querySelectorAll('.live-card').length, 1);
});

test('stopping ends playback while retaining processing progress and the saved recording action', async (t) => {
  const f = fixture(); t.after(() => f.close());
  f.refresh([live()]);
  f.refresh([live({ status: 'stopping' })]);
  assert.equal(f.button('Encerrando gravação…').disabled, true);
  assert.equal(f.instances[0].destroyed, 0);
  f.refresh([live({ status: 'processing', preview_ready: false, video_id: 'saved-video',
    analysis_status: 'running', analysis_progress: 35, analysis_message: 'Transcrevendo a gravação.' })]);
  assert.equal(f.instances[0].destroyed, 1);
  assert.equal(f.$('video').hidden, true);
  assert.equal(f.$('progress').value, 35);
  assert.equal(f.$('progress').hidden, false);
  assert.match(f.$('.live-card').textContent, /Transcrevendo a gravação/);
  assert.equal(f.button('Encerrar gravação').hidden, true);
  f.button('Abrir gravação').click();
  await Promise.resolve();
  assert.deepEqual(f.calls.open, ['saved-video']);
  f.instances[0].emit(f.FakeHls.Events.ERROR, { fatal: true, type: 'networkError' });
  assert.equal(f.timers.size, 0);
  f.refresh([live({ status: 'completed', video_id: 'saved-video', analysis_status: 'done' })]);
  assert.equal(f.instances.length, 1);
  assert.equal(f.$('progress').hidden, true);
  assert.equal(f.$('.live-badge').textContent, 'Gravação salva');
});

test('stop has one purpose and rejects duplicate clicks during its request', async (t) => {
  let complete;
  const stopped = [];
  const f = fixture({ stop: (id) => { stopped.push(id); return new Promise((resolve) => { complete = resolve; }); } });
  t.after(() => f.close());
  f.refresh([live()]);
  const stop = f.button('Encerrar gravação');
  stop.click(); stop.click();
  assert.deepEqual(stopped, ['live-a']);
  assert.equal(stop.disabled, true);
  assert.equal(f.instances[0].destroyed, 0);
  complete();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(stop.disabled, false);
});

test('fatal network retries are bounded and manual retry does not stop the recording', (t) => {
  const f = fixture(); t.after(() => f.close());
  f.refresh([live()]);
  const player = f.instances[0];
  const error = { fatal: true, type: 'networkError', details: 'levelLoadError' };
  for (let attempt = 0; attempt < 2; attempt++) {
    player.emit('error', error);
    player.emit('error', error);
    assert.equal(f.timers.size, 1);
    f.flushRetry();
  }
  assert.equal(player.loads.length, 2);
  player.emit('error', error);
  assert.equal(player.destroyed, 1);
  assert.equal(f.timers.size, 0);
  assert.equal(f.button('Tentar prévia novamente').hidden, false);
  f.refresh([live({ bytes: 9000000 })]);
  assert.equal(f.instances.length, 1);
  f.button('Tentar prévia novamente').click();
  assert.equal(f.instances.length, 2);
  assert.equal(f.button('Tentar prévia novamente').hidden, true);
  assert.deepEqual(f.calls.stop, []);
});

test('media recovery is limited and incompatible codecs have a clear explanation', (t) => {
  const f = fixture(); t.after(() => f.close());
  f.refresh([live()]);
  const player = f.instances[0];
  player.emit('error', { fatal: true, type: 'mediaError', details: 'bufferAppendError' });
  f.flushRetry();
  assert.equal(player.recoveries, 1);
  player.emit('error', { fatal: true, type: 'mediaError', details: 'manifestIncompatibleCodecsError' });
  assert.equal(player.destroyed, 1);
  assert.match(f.$('.live-preview-status').textContent, /formato da fonte/);
  assert.equal(f.$('.live-badge').textContent, 'Gravando');
  assert.deepEqual(f.calls.stop, []);
});

test('audio-only sources explain the absent video without requesting or stopping a player', (t) => {
  const f = fixture(); t.after(() => f.close());
  f.refresh([live({ preview_has_video: false, preview_ready: false })]);
  assert.equal(f.instances.length, 0);
  assert.match(f.$('.live-preview-status').textContent, /apenas áudio/);
  assert.equal(f.button('Encerrar gravação').hidden, false);
  assert.deepEqual(f.calls.stop, []);
});

test('native HLS fallback uses the same authenticated origin and preserves native controls', (t) => {
  const f = fixture({ hlsSupported: false, native: true }); t.after(() => f.close());
  f.refresh([live()]);
  const video = f.$('video');
  assert.equal(f.instances.length, 0);
  assert.equal(video.src, 'https://studio.example.test/api/live/live-a/preview/index.m3u8');
  assert.equal(video.muted, true);
  assert.equal(video.autoplay, true);
  assert.equal(video.controls, true);
  video.dispatchEvent(new f.win.Event('playing'));
  assert.match(f.$('.live-preview-status').textContent, /controle de som/);
  f.refresh([live({ status: 'completed', video_id: 'saved' })]);
  assert.equal(video.hasAttribute('src'), false);
  assert.equal(video.hidden, true);
});

test('missing HLS support is a preview error while the recording controls remain usable', (t) => {
  const f = fixture({ noHls: true }); t.after(() => f.close());
  f.refresh([live()]);
  assert.match(f.$('.live-preview-status').textContent, /navegador/);
  assert.equal(f.button('Tentar prévia novamente').hidden, false);
  assert.equal(f.button('Encerrar gravação').disabled, false);
});

test('playback rejects external, script and cross-recording URLs without leaking them', (t) => {
  for (const url of ['https://untrusted.example/token', 'javascript:alert(1)',
    '/api/live/other/preview/index.m3u8', '/api/live/live-a/preview/index.m3u8?token=private']) {
    const f = fixture();
    try {
      f.refresh([live({ preview_url: url, message: '<img src=x onerror=alert(1)>' })]);
      assert.equal(f.instances.length, 0);
      assert.equal(f.$('video').hasAttribute('src'), false);
      assert.equal(f.$('img'), null);
      assert.equal(f.$('iframe'), null);
      assert.doesNotMatch(f.$('.live-preview-status').textContent, /untrusted|private|javascript/);
      assert.match(f.$('.live-card').textContent, /<img src=x/);
    } finally { f.close(); }
  }
});

test('expired authentication has a Portuguese message and no automatic retry loop', (t) => {
  const f = fixture(); t.after(() => f.close());
  f.refresh([live()]);
  f.instances[0].emit('error', { fatal: true, type: 'networkError', response: { code: 401 } });
  assert.equal(f.instances[0].destroyed, 1);
  assert.equal(f.timers.size, 0);
  assert.match(f.$('.live-preview-status').textContent, /entre novamente/);
});

test('destroyAll clears players and scheduled recovery, allowing a fresh rendering later', (t) => {
  const f = fixture(); t.after(() => f.close());
  f.refresh([live()]);
  f.instances[0].emit('error', { fatal: true, type: 'networkError' });
  assert.equal(f.timers.size, 1);
  f.win.AutoClipsLive.destroyAll();
  assert.equal(f.instances[0].destroyed, 1);
  assert.equal(f.timers.size, 0);
  assert.equal(f.$('.live-card'), null);
  assert.equal(f.$('#lives-empty').hidden, false);
  f.refresh([live()]);
  assert.equal(f.instances.length, 2);
});
