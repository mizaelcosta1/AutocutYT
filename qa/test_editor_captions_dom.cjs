/** Simulated DOM behavior tests. This is not visual or authenticated browser QA. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM, VirtualConsole } = require('jsdom');
const fs = require('node:fs');
const path = require('node:path');
const repo = path.resolve(__dirname, '..');
const VID = 'a'.repeat(32);
const copy = (value) => JSON.parse(JSON.stringify(value));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(fn) { for (let i = 0; i < 300; i++) { if (fn()) return; await sleep(10); } throw new Error('DOM did not settle'); }

async function fixture() {
  const console = new VirtualConsole();
  const errors = [];
  console.on('jsdomError', (e) => errors.push(e.message));
  const dom = new JSDOM(fs.readFileSync(path.join(repo, 'static/index.html'), 'utf8'), {
    url: 'https://studio.example.test/', runScripts: 'outside-only', virtualConsole: console,
  });
  const win = dom.window;
  win.HTMLMediaElement.prototype.load = function () {};
  win.HTMLMediaElement.prototype.play = () => Promise.resolve();
  win.HTMLMediaElement.prototype.pause = function () {};
  const calls = [];
  const style = { font_family: 'DejaVu Sans', font_size: 56, color: '#FFFFFF' };
  const data = { style, words: [{ start: 0, end: 1, text: 'Teste' }],
    clip: { id: 'clip_01', title: 'Momento', start: 0, end: 2, status: 'preview_ready',
      preview_url: '/preview.mp4', final_url: null,
      preview_options: { layout: 'blur', burn: true, subtitle_style: copy(style) } } };
  const reply = (body, status = 200) => ({ ok: status < 400, status,
    headers: { get: () => 'application/json' }, json: async () => copy(body) });
  win.AutoClipsLive = { refresh: (lives) => { data.lives = lives; }, destroyAll() {} };
  const video = { id: VID, name: 'Teste.mp4', duration: 10, width: 640, height: 360,
    size: 1000, platform: 'fixture', stream_url: '/source.mp4' };
  win.fetch = async (url, options = {}) => {
    const pathname = new URL(url, win.location.origin).pathname;
    const call = { path: pathname, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined };
    calls.push(call);
    if (pathname === '/api/config') return reply({ transcribe_available: true, ffmpeg: true, streamlink: true, ytdlp: true, storage_free_bytes: 100000 });
    if (pathname.endsWith('/activity')) return reply({activity:null});
    if (pathname === '/api/videos') return reply({ videos: [video] });
    if (pathname === '/api/lives') return reply({ lives: [] });
    if (pathname.endsWith('/manifest')) return reply({ clips: [data.clip], transcription_engine: 'fixture' });
    if (pathname.endsWith('/transcript') && call.method === 'GET') return reply({ available: Boolean(data.words.length), words: data.words, engine: 'fixture', subtitle_style: data.style });
    if (pathname.endsWith('/subtitle-style')) { data.style = call.body; return reply({ subtitle_style: data.style }); }
    if (pathname.endsWith('/clips') && call.method === 'PUT') return reply({ ok: true });
    if (pathname.endsWith('/render/preview')) {
      data.clip.preview_options = { layout: call.body.layout, burn: call.body.subtitles, subtitle_style: copy(call.body.subtitle_style) };
      return reply({ task_id: 'render' });
    }
    if (pathname === '/api/live/start') return reply({ id: 'live-test', status: 'starting' });
    if (pathname === '/api/source/auto') return reply({ kind: 'live', live_id: 'live-test' });
    if (pathname === '/api/tasks/render') return reply({ status: 'done', result: {} });
    if (pathname.endsWith('/transcript') && call.method === 'POST') return reply({ task_id: 'transcribe' });
    if (pathname === '/api/tasks/transcribe') return reply({ status: 'done', result: { words: data.words.length } });
    throw new Error(`Unexpected request ${call.method} ${pathname}`);
  };
  win.eval(fs.readFileSync(path.join(repo, 'static/video-activity.js'), 'utf8'));
  win.eval(fs.readFileSync(path.join(repo, 'static/app.js'), 'utf8'));
  await until(() => calls.some((c) => c.path === '/api/lives'));
  const $ = (selector) => win.document.querySelector(selector);
  async function open() { await win._openVideoById(VID); await until(() => $('.clip-card')); }
  function input(selector, value) { const el = $(selector); el.value = value; el.dispatchEvent(new win.Event('input', { bubbles: true })); }
  return { win, $, calls, data, errors, open, input, close: () => dom.window.close() };
}

test('recording and link import request transcription using the checked option', async (t) => {
  const f = await fixture(); t.after(f.close);
  f.$('#live-url').value = 'https://www.twitch.tv/test';
  f.$('#live-authorized').checked = true;
  f.$('#btn-live').click();
  await until(() => f.calls.some((c) => c.path === '/api/live/start'));
  const start = f.calls.find((c) => c.path === '/api/live/start');
  assert.equal(start.body.transcribe_after, true);
  assert.equal(start.body.auto_preview, true);
  await until(() => !f.$('#btn-live').disabled);
  assert.equal(f.$('#btn-live').textContent, '● Gravar live');
  f.$('#vod-url').value = 'https://www.youtube.com/watch?v=test';
  f.$('#vod-authorized').checked = true;
  f.$('#vod-transcribe').checked = false;
  f.$('#btn-vod').click();
  await until(() => f.calls.some((c) => c.path === '/api/source/auto'));
  assert.equal(f.calls.find((c) => c.path === '/api/source/auto').body.transcribe_after, false);
});

test('editor restores style and exposes both caption formats', async (t) => {
  const f = await fixture(); t.after(f.close);
  f.data.style = { font_family: 'Liberation Serif', font_size: 70, color: '#22D3EE' };
  await f.open();
  assert.equal(f.$('#subtitle-font').value, 'Liberation Serif');
  assert.equal(f.$('#subtitle-size').value, '70');
  assert.equal(f.$('#subtitle-color').value, '#22d3ee');
  assert.equal(f.$('#burn-subs').checked, true);
  assert.equal(f.$('#subtitle-download-srt').classList.contains('hidden'), false);
  assert.match(f.$('#subtitle-download-vtt').href, /subtitles\.vtt$/);
  assert.equal(f.$('.c-render-final').disabled, true);
  assert.deepEqual(f.errors, []);
});

test('changing style revokes approval and persists the exact validated choices', async (t) => {
  const f = await fixture(); t.after(f.close); await f.open();
  f.$('.c-preview-approve').checked = true;
  f.win._approveClipPreview('clip_01');
  assert.equal(f.$('.c-render-final').disabled, false);
  f.input('#subtitle-color', '#22d3ee');
  f.input('#subtitle-size', '72');
  f.input('#subtitle-font', 'Noto Sans');
  assert.equal(f.$('.c-preview-approve').checked, false);
  assert.equal(f.$('.c-render-final').disabled, true);
  assert.equal(f.$('.c-preview-settings-note').classList.contains('hidden'), false);
  await until(() => f.calls.some((c) => c.path.endsWith('/subtitle-style')));
  const saved = f.calls.filter((c) => c.path.endsWith('/subtitle-style')).at(-1).body;
  assert.deepEqual(saved, { font_family: 'Noto Sans', font_size: 72, color: '#22D3EE' });
});

test('preview request flushes style and carries the same caption configuration', async (t) => {
  const f = await fixture(); t.after(f.close); await f.open();
  f.input('#subtitle-color', '#ff5555');
  await f.win._renderClip('clip_01', 'preview');
  const render = f.calls.find((c) => c.path.endsWith('/render/preview'));
  assert.equal(render.body.subtitle_style.color, '#FF5555');
  assert.equal(render.body.subtitles, true);
  assert.ok(f.calls.findIndex((c) => c.path.endsWith('/subtitle-style')) < f.calls.indexOf(render));
  assert.equal(f.$('.c-preview-approve').disabled, false);
  assert.equal(f.$('.c-render-final').disabled, true, 'A new preview still requires human review');
});

test('invalid font size cannot be saved or submitted for rendering', async (t) => {
  const f = await fixture(); t.after(f.close); await f.open();
  f.input('#subtitle-size', '999');
  await f.win._renderClip('clip_01', 'preview');
  await sleep(400);
  assert.equal(f.calls.some((c) => c.path.endsWith('/render/preview')), false);
  assert.equal(f.calls.some((c) => c.path.endsWith('/subtitle-style')), false);
  assert.equal(f.$('#subtitle-size').checkValidity(), false);
});

test('video without detected speech does not offer empty caption downloads', async (t) => {
  const f = await fixture(); t.after(f.close); f.data.words = []; await f.open();
  assert.equal(f.$('#burn-subs').checked, false);
  assert.equal(f.$('#burn-subs').disabled, true);
  assert.equal(f.$('#subtitle-download-srt').classList.contains('hidden'), true);
  assert.equal(f.$('#subtitle-download-vtt').classList.contains('hidden'), true);
});
