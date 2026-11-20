/**
 * Timeline subtitle behavior in a simulated DOM, not visual/browser QA.
 * No real transcription, video processing, provider calls or credentials.
 * NODE_PATH=/path/to/qa-dom-runtime/node_modules node --test qa/test_timeline_subtitles_dom.cjs
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');

const repo = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(repo, 'static/timeline.html'), 'utf8');
const script = fs.readFileSync(path.join(repo, 'static/timeline.js'), 'utf8');
const videoId = 'a'.repeat(32);
const base = '/api/videos/' + videoId;
const defaultStyle = { font_family: 'DejaVu Sans', font_size: 56, color: '#FFFFFF' };
const clone = (value) => JSON.parse(JSON.stringify(value));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const reply = (value, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => clone(value) });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
async function until(predicate, label = 'DOM did not settle', timeout = 4000) {
  const end = Date.now() + timeout;
  while (!predicate()) { if (Date.now() > end) throw new Error(label); await sleep(8); }
}
function renderMeta(style = defaultStyle, extra = {}) {
  return { version: 1, layout: 'blur', subtitles: false, subtitle_style: clone(style),
    render_id: 'initial-preview', probe: { duration: 12 }, ...extra };
}

async function fixture(options = {}) {
  const calls = [], errors = [], tasks = new Map();
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => errors.push(error.message));
  const dom = new JSDOM(html, { url: 'https://studio.example.test/studio/' + videoId, runScripts: 'outside-only',
    pretendToBeVisual: true, virtualConsole });
  const win = dom.window;
  win.HTMLMediaElement.prototype.load = function () {};
  win.HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
  win.HTMLMediaElement.prototype.pause = function () {};
  win.confirm = () => true;
  const originalTimeout = win.setTimeout.bind(win);
  win.setTimeout = (fn, ms, ...args) => originalTimeout(fn, [1250, 5000, 10000].includes(ms) ? (options.pollDelay || 8) : ms, ...args);
  if (options.now) win.Date.now = options.now;
  win.document.querySelector('#export-subtitles').checked = Boolean(options.subtitles);
  const state = {
    style: clone(options.style || defaultStyle),
    words: clone(options.words || []),
    transcription: clone(options.transcription || {}),
    transcriptNotice: options.transcriptNotice || '',
    exports: clone(options.exports || {}),
    taskNumber: 0,
    completedRenders: 0,
  };
  const transcriptResponse = () => ({
    available: state.words.length > 0, engine: state.words.length ? 'faster-whisper' : 'none',
    words: state.words, state: state.transcription, notice: state.transcriptNotice, subtitle_style: state.style,
  });
  win.fetch = async (url, init = {}) => {
    const parsed = new URL(url, win.location.origin);
    const call = { pathname: parsed.pathname, search: parsed.searchParams, method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null };
    calls.push(call);
    if (options.intercept) {
      const handled = options.intercept(call, { state, tasks, reply, transcriptResponse });
      if (handled !== undefined) return await handled;
    }
    if (call.pathname.endsWith('/activity')) return reply({activity:null});
    if (call.pathname === '/api/videos') return reply({ videos: [{ id: videoId, name: 'Gravação de teste', duration: 60, width: 1920, height: 1080 }] });
    if (call.pathname === '/api/config') return reply({ transcribe_available: options.available !== false });
    if (call.pathname === base + '/timeline' && call.method === 'GET') return reply({
      timeline: { schema: 1, version: 1, source_duration: 60, segments: [{ id: 's1', source_in: 0, source_out: 12 }] },
      edited_duration: 12, can_undo: false, can_redo: false,
    });
    if (call.pathname === base + '/transcript' && call.method === 'GET') return reply(transcriptResponse());
    if (call.pathname === base + '/subtitle-style' && call.method === 'PUT') {
      state.style = clone(call.body); return reply({ subtitle_style: state.style });
    }
    if (call.pathname === base + '/timeline/exports') return reply({ exports: state.exports });
    if (call.pathname === base + '/transcript' && call.method === 'POST') {
      const id = 'transcription-' + (++state.taskNumber);
      state.transcription = { status: 'running', message: 'Preparando transcrição.' };
      tasks.set(id, { kind: 'transcription', polls: 0 });
      return reply({ task_id: id });
    }
    if (call.pathname.startsWith(base + '/timeline/render/') && call.method === 'POST') {
      const quality = call.pathname.split('/').at(-1);
      const id = 'render-' + (++state.taskNumber);
      tasks.set(id, { kind: 'render', quality, body: clone(call.body), polls: 0 });
      return reply({ task_id: id });
    }
    if (call.pathname.startsWith('/api/tasks/')) {
      const task = tasks.get(call.pathname.split('/').at(-1));
      if (!task) throw new Error('Unknown task');
      task.polls++;
      if (task.kind === 'transcription') {
        state.words = clone(options.transcribedWords === undefined ? [{ start: 0.1, end: 0.6, text: 'Olá' }, { start: 0.7, end: 1.2, text: 'pessoal' }] : options.transcribedWords);
        state.transcription = { status: 'done' };
        state.transcriptNotice = options.completedNotice || '';
        state.exports = {};
        return reply({ status: 'done', result: { words: state.words.length, notice: state.transcriptNotice } });
      }
      state.exports[task.quality] = renderMeta(task.body.subtitle_style, {
        subtitles: task.body.subtitles, layout: task.body.layout, render_id: task.quality + '-' + (++state.completedRenders),
      });
      return reply({ status: 'done', result: { quality: task.quality } });
    }
    throw new Error('Unexpected fixture request: ' + call.method + ' ' + call.pathname);
  };
  win.eval(fs.readFileSync(path.join(repo, 'static/video-activity.js'), 'utf8'));
  win.eval(script);
  await until(() => !win.document.querySelector('#subtitle-font').disabled && calls.some((c) => c.pathname === base + '/timeline/exports'), 'initial project data');
  await sleep(0);
  const $ = (selector) => win.document.querySelector(selector);
  return { dom, win, state, calls, errors, $, reply,
    input(selector, value) { $(selector).value = value; $(selector).dispatchEvent(new win.Event('input', { bubbles: true })); },
    change(selector, value) { if ($(selector).type === 'checkbox') $(selector).checked = value; else $(selector).value = value; $(selector).dispatchEvent(new win.Event('change', { bubbles: true })); },
    close() { dom.window.close(); },
    saves() { return calls.filter((c) => c.pathname === base + '/subtitle-style' && c.method === 'PUT'); },
    renders() { return calls.filter((c) => c.pathname.startsWith(base + '/timeline/render/') && c.method === 'POST'); },
  };
}

test('loads saved font, size and color and exposes synchronized SRT/VTT downloads', async (t) => {
  const style = { font_family: 'Noto Sans', font_size: 60, color: '#44EEFF' };
  const f = await fixture({ style, words: [{ start: 0, end: 1, text: 'teste' }] }); t.after(() => f.close());
  assert.equal(f.$('#subtitle-font').value, style.font_family);
  assert.equal(f.$('#subtitle-size').value, '60');
  assert.equal(f.$('#subtitle-color').value, '#44eeff');
  assert.equal(f.$('#subtitle-sample').style.fontSize, '40px');
  assert.match(f.$('#subtitle-sample').style.fontFamily, /Noto Sans/);
  assert.equal(f.$('#download-srt').classList.contains('hidden'), false);
  assert.equal(f.$('#download-vtt').classList.contains('hidden'), false);
  assert.ok(f.$('#download-srt').href.endsWith('/timeline.srt'));
  assert.ok(f.$('#download-vtt').href.endsWith('/timeline.vtt'));
  assert.equal(f.saves().length, 0);
  assert.deepEqual(f.errors, []);
});

test('debounces font, size and color edits into a single canonical save', async (t) => {
  const f = await fixture({ subtitles:true, words:[{start:0,end:1,text:'Teste'}],
    exports: { preview: renderMeta(defaultStyle, {subtitles:true}) } }); t.after(() => f.close());
  f.change('#approve-preview', true);
  assert.equal(f.$('#render-final').disabled, false);
  f.input('#subtitle-font', 'Liberation Serif');
  f.input('#subtitle-size', '72');
  f.input('#subtitle-color', '#ee4455');
  assert.equal(f.$('#approve-preview').checked, false);
  assert.equal(f.$('#render-final').disabled, true);
  assert.equal(f.$('#preview-video').classList.contains('hidden'), true);
  await until(() => f.saves().length === 1 && /salva/.test(f.$('#subtitle-save-status').textContent));
  assert.deepEqual(f.saves()[0].body, { font_family: 'Liberation Serif', font_size: 72, color: '#EE4455' });
  assert.equal(f.$('#subtitle-sample').style.fontSize, '48px');
  assert.equal(f.$('#subtitle-sample').style.color, 'rgb(238, 68, 85)');
});

test('serializes pending saves so a late response cannot overwrite a newer color', async (t) => {
  const first = deferred();
  const f = await fixture({ intercept(call, { state, reply }) {
    if (call.pathname === base + '/subtitle-style' && call.method === 'PUT' && call.body.color === '#FF0000') {
      return first.promise.then(() => { state.style = clone(call.body); return reply({ subtitle_style: state.style }); });
    }
  }}); t.after(() => f.close());
  f.input('#subtitle-color', '#ff0000');
  await until(() => f.saves().length === 1);
  f.input('#subtitle-color', '#00aaff');
  await sleep(440);
  assert.equal(f.saves().length, 1);
  first.resolve();
  await until(() => f.saves().length === 2 && /salva/.test(f.$('#subtitle-save-status').textContent));
  assert.equal(f.saves()[1].body.color, '#00AAFF');
  assert.equal(f.state.style.color, '#00AAFF');
  assert.equal(f.$('#subtitle-color').value, '#00aaff');
});

test('render waits for style persistence and sends the current style exactly once', async (t) => {
  const save = deferred();
  const f = await fixture({ intercept(call, { state, reply }) {
    if (call.pathname === base + '/subtitle-style' && call.method === 'PUT') {
      return save.promise.then(() => { state.style = clone(call.body); return reply({ subtitle_style: state.style }); });
    }
  }}); t.after(() => f.close());
  f.input('#subtitle-size', '68');
  f.change('#approve-selection', true);
  f.$('#render-preview').click();
  f.$('#render-preview').click();
  await until(() => f.saves().length === 1);
  assert.equal(f.renders().length, 0);
  save.resolve();
  await until(() => f.renders().length === 1 && !f.$('#preview-video').classList.contains('hidden'));
  assert.deepEqual(f.renders()[0].body.subtitle_style, { ...defaultStyle, font_size: 68 });
  assert.equal(f.$('#approve-preview').checked, false);
  assert.equal(f.$('#render-final').disabled, true);
  assert.match(f.$('#preview-video').getAttribute('src'), /render_id=preview-1$/);
});

test('re-rendering the same timeline version reloads media with the new render id', async (t) => {
  const f = await fixture({ exports: { preview: renderMeta() } }); t.after(() => f.close());
  const old = f.$('#preview-video').getAttribute('src');
  assert.match(old, /render_id=initial-preview$/);
  f.change('#approve-selection', true);
  f.$('#render-preview').click();
  await until(() => /render_id=preview-1$/.test(f.$('#preview-video').getAttribute('src') || '') && !f.$('#render-preview').disabled);
  f.$('#render-preview').click();
  await until(() => /render_id=preview-2$/.test(f.$('#preview-video').getAttribute('src') || ''));
  assert.equal(f.renders().length, 2);
  assert.ok(f.$('#preview-link').href.endsWith('render_id=preview-2'));
});

test('changing the style, layout or subtitle inclusion invalidates final approval', async (t) => {
  const f = await fixture({ exports: { preview: renderMeta(), final: renderMeta(defaultStyle, { render_id: 'old-final' }) } }); t.after(() => f.close());
  f.change('#approve-preview', true);
  assert.equal(f.$('#render-final').disabled, false);
  f.change('#export-layout', 'crop');
  assert.equal(f.$('#approve-preview').checked, false);
  assert.equal(f.$('#render-final').disabled, true);
  assert.equal(f.$('#final-link').classList.contains('hidden'), true);
  f.$('#render-final').click();
  assert.equal(f.renders().length, 0);
  f.change('#export-layout', 'blur');
  assert.equal(f.$('#render-final').disabled, true);
  f.change('#approve-preview', true);
  f.change('#export-subtitles', true);
  assert.equal(f.$('#render-final').disabled, true);
  assert.equal(f.$('#approve-preview').checked, false);
});

test('an approved matching preview allows the final with the same subtitle style', async (t) => {
  const style = { font_family: 'DejaVu Serif', font_size: 62, color: '#FFCC22' };
  const f = await fixture({ style, subtitles: true,
    words: [{ start: 0, end: 1, text: 'legenda' }],
    exports: { preview: renderMeta(style, { subtitles: true }) },
  }); t.after(() => f.close());
  f.change('#approve-preview', true);
  assert.equal(f.$('#render-final').disabled, false);
  f.$('#render-final').click();
  await until(() => !f.$('#final-link').classList.contains('hidden'));
  assert.equal(f.renders().length, 1);
  assert.ok(f.renders()[0].pathname.endsWith('/final'));
  assert.equal(f.renders()[0].body.approved_preview, true);
  assert.equal(f.renders()[0].body.subtitles, true);
  assert.deepEqual(f.renders()[0].body.subtitle_style, style);
  assert.ok(f.$('#final-link').href.endsWith('render_id=final-1'));
});

test('transcription reloads words, both subtitle downloads and export validity', async (t) => {
  const f = await fixture({ exports: { preview: renderMeta(defaultStyle, { subtitles: true }) }, subtitles: true }); t.after(() => f.close());
  f.change('#approve-preview', true);
  f.$('#transcribe-local').click();
  f.$('#transcribe-local').click();
  await until(() => /2 palavras transcritas/.test(f.$('#transcript-info').textContent));
  assert.equal(f.calls.filter((c) => c.pathname === base + '/transcript' && c.method === 'POST').length, 1);
  assert.equal(f.$('#download-srt').classList.contains('hidden'), false);
  assert.equal(f.$('#download-vtt').classList.contains('hidden'), false);
  assert.equal(f.$('#preview-video').classList.contains('hidden'), true);
  assert.equal(f.$('#render-final').disabled, true);
  assert.equal(f.$('#approve-preview').checked, false);
  assert.equal(f.$('#transcript-words').querySelectorAll('[data-seek]').length, 2);
  assert.ok(f.calls.filter((c) => c.pathname === base + '/timeline/exports').length >= 2);
});

test('shows useful progress and continues polling a recording past twenty minutes', async (t) => {
  let clock = 1_000_000, poll = 0;
  const finish = deferred();
  const f = await fixture({ now: () => clock, intercept(call, { state, reply }) {
    if (call.pathname.startsWith('/api/tasks/transcription-')) {
      poll++;
      if (poll === 1) {
        clock += 25 * 60 * 1000;
        return reply({ status: 'running', phase: 'transcribing', progress: 42, message: 'Transcrevendo bloco de áudio' });
      }
      return finish.promise.then(() => {
        state.words = [{ start: 0, end: 1, text: 'feito' }]; state.transcription = { status: 'done' };
        return reply({ status: 'done', result: { words: 1 } });
      });
    }
  }}); t.after(() => f.close());
  f.$('#transcribe-local').click();
  await until(() => f.$('#transcript-info').textContent.includes('42%'));
  assert.match(f.$('#transcript-info').textContent, /Transcrevendo bloco de áudio/);
  assert.equal(/running|transcribing|queued/.test(f.$('#transcript-info').textContent), false);
  await until(() => poll >= 2);
  finish.resolve();
  await until(() => /1 palavras transcritas/.test(f.$('#transcript-info').textContent));
  assert.equal(/excedido/.test(f.$('#notice').textContent), false);
});

test('no speech is reported honestly and does not expose empty subtitle downloads', async (t) => {
  const f = await fixture({ transcribedWords: [] }); t.after(() => f.close());
  f.$('#transcribe-local').click();
  await until(() => /sem detectar falas/.test(f.$('#transcript-info').textContent));
  assert.equal(f.$('#download-srt').classList.contains('hidden'), true);
  assert.equal(f.$('#download-vtt').classList.contains('hidden'), true);
  assert.match(f.$('#notice').textContent, /nenhuma fala/i);
  assert.equal(f.$('#remove-pauses').disabled, true);
});

test('unavailable transcription is explained without advertising a working action', async (t) => {
  const f = await fixture({ available: false }); t.after(() => f.close());
  assert.equal(f.$('#transcribe-local').disabled, true);
  assert.match(f.$('#transcript-info').textContent, /indisponível neste servidor/);
  assert.equal(/faster-whisper|instale/i.test(f.$('#transcribe-local').textContent), false);
  f.$('#transcribe-local').click();
  assert.equal(f.calls.filter((c) => c.pathname === base + '/transcript' && c.method === 'POST').length, 0);
});

test('invalid size and failed persistence block rendering with visible feedback', async (t) => {
  const f = await fixture({ intercept(call, { reply }) {
    if (call.pathname === base + '/subtitle-style' && call.method === 'PUT') return reply({ detail: 'Não foi possível salvar o estilo.' }, 503);
  }}); t.after(() => f.close());
  f.change('#approve-selection', true);
  f.input('#subtitle-size', '17');
  assert.equal(f.$('#render-preview').disabled, true);
  assert.equal(f.$('#subtitle-size').checkValidity(), false);
  await sleep(430);
  assert.equal(f.saves().length, 0);
  f.input('#subtitle-size', '64');
  f.$('#render-preview').click();
  await until(() => /Não foi possível salvar/.test(f.$('#preview-status').textContent));
  assert.equal(f.renders().length, 0);
  assert.equal(f.$('#render-final').disabled, true);
  assert.match(f.$('#subtitle-save-status').textContent, /Não foi possível salvar/);
  assert.deepEqual(f.errors, []);
});

test('task errors keep the original failure visible and never claim a completed transcription', async (t) => {
  const f = await fixture({ intercept(call, { reply }) {
    if (call.pathname.startsWith('/api/tasks/transcription-')) return reply({ status: 'error', error: 'Não foi possível preparar o áudio.' });
  }}); t.after(() => f.close());
  f.$('#transcribe-local').click();
  await until(() => /não foi concluída/.test(f.$('#transcript-info').textContent));
  assert.match(f.$('#transcript-info').textContent, /preparar o áudio/);
  assert.match(f.$('#notice').textContent, /preparar o áudio/);
  assert.equal(f.$('#notice').classList.contains('ok'), false);
  assert.equal(f.$('#transcribe-local').disabled, false);
});
