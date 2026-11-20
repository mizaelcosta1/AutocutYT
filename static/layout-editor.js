/* Shared composition controls for the clip editor and timeline. */
(function () {
  'use strict';
  const defaults = () => ({ mode: 'standard', aspect: '9:16', face: null, gameplay: null, split_ratio: 0.34, pip_position: 'top-right', pip_scale: 0.28, game_fit: 'contain', caption_position: 'bottom' });
  const clone = v => JSON.parse(JSON.stringify(v));
  function normalize(value) {
    const source = value || {}, base = defaults(), result = {};
    for (const key of Object.keys(base)) {
      const v = source[key] === undefined ? base[key] : source[key];
      result[key] = (key === 'face' || key === 'gameplay') && v ? {x:v.x,y:v.y,w:v.w,h:v.h} : v;
    }
    return result;
  }
  const same = (a,b) => JSON.stringify(normalize(a)) === JSON.stringify(normalize(b));
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
  const pct = n => Math.round(n * 1000) / 10;
  function create({container, videoId = null, layoutSelect, onChange = () => {}, onPresetSubtitles = () => {}, getSubtitles = () => undefined, getTime = () => 0}) {
    const root = typeof container === 'string' ? document.querySelector(container) : container;
    const select = typeof layoutSelect === 'string' ? document.querySelector(layoutSelect) : layoutSelect;
    if (!root || !select) throw new Error('Editor de composição sem elemento necessário.');
    let id = videoId, config = defaults(), presets = [], custom = [], region = 'face', candidates = [], destroyed = false, saveTimer, requestGeneration = 0, dirtyCoordinates = false, loaded = false, revision = 0, persistedRevision = 0, saveQueue = Promise.resolve();
    let preferredSubtitles = null;
    const restoreSubtitles = () => {
      if (loaded && typeof preferredSubtitles === 'boolean') onPresetSubtitles(preferredSubtitles);
    };
    const subtitleSetting = () => preferredSubtitles ?? getSubtitles() ?? null;
    function subtitlesChanged() {
      if (!loaded || destroyed) return;
      preferredSubtitles = getSubtitles() ?? null;
      notify();
    }
    const listeners = new AbortController();
    const activity = window.VideoActivity;
    const selectLabel = document.querySelector(`label[for="${select.id}"]`) || select.closest('label');
    root.innerHTML = `<fieldset class="le-shell" disabled><div class="le-head"><strong>Formato e composição</strong><span>Escolha um modelo e ajuste como quiser.</span></div>
      <p class="le-status" role="status" aria-live="polite"></p><div class="le-presets" aria-label="Modelos de composição"></div>
      <div class="le-custom"><label>Salvar modelo <input class="le-name" type="text" maxlength="60" placeholder="Nome do modelo"></label><button type="button" class="le-save">Salvar</button></div>
      <div class="le-controls"><label>Formato <select data-field="aspect"><option value="9:16">Vertical · 9:16</option><option value="16:9">Horizontal · 16:9</option></select></label>
      <label class="le-split">Altura do rosto <input type="range" min="20" max="65" step="1" data-field="split_ratio"><output></output></label>
      <label class="le-pip">Posição do rosto <select data-field="pip_position"><option value="top-left">Superior esquerda</option><option value="top-right">Superior direita</option><option value="bottom-left">Inferior esquerda</option><option value="bottom-right">Inferior direita</option></select></label>
      <label class="le-pip">Tamanho do rosto <input type="range" min="18" max="45" step="1" data-field="pip_scale"><output></output></label>
      <label class="le-composite">Ajuste do jogo <select data-field="game_fit"><option value="contain">Mostrar inteiro</option><option value="cover">Preencher área</option></select></label>
      <label class="le-caption-position">Posição da legenda <select data-field="caption_position"><option value="bottom">Embaixo</option><option value="middle">No meio</option><option value="top">Em cima</option></select></label></div>
      <details class="le-areas"><summary>Ajustar áreas do vídeo original</summary><p>Escolha a área do rosto e a do jogo. Desenhe uma área no quadro, arraste o contorno ou ajuste os valores abaixo.</p>
      <div class="le-row"><button type="button" data-region="face">Rosto</button><button type="button" data-region="gameplay">Jogo</button><button type="button" class="le-detect">Procurar rostos</button><button type="button" class="le-use-frame">Usar quadro do player</button></div>
      <div class="le-candidates"></div>
      <div class="le-stage"><img class="le-frame" alt="Quadro do vídeo original para ajustar as áreas"><div class="le-box" tabindex="0" role="group" aria-label="Área selecionada. Arraste ou use os campos abaixo."><span class="le-resize" aria-hidden="true"></span></div></div>
      <div class="le-numbers"><label>Esquerda %<input type="number" min="0" max="100" step="0.1" data-coordinate="x"></label><label>Topo %<input type="number" min="0" max="100" step="0.1" data-coordinate="y"></label><label>Largura %<input type="number" min="2" max="100" step="0.1" data-coordinate="w"></label><label>Altura %<input type="number" min="2" max="100" step="0.1" data-coordinate="h"></label></div>
      <p class="le-error" role="status" aria-live="polite"></p><button type="button" class="le-reset">Usar vídeo inteiro nesta área</button></details></fieldset>`;
    const $ = s => root.querySelector(s);
    const status = msg => { if (!destroyed) $('.le-status').textContent = msg; };
    const enable = active => { active = active && !activity.busy(id); if (!destroyed) { $('.le-shell').disabled = !active; select.disabled = !active; } };
    window.addEventListener('video-activity', ({detail}) => {
      if (id === detail.id) enable(loaded);
    }, {signal:listeners.signal});
    window.addEventListener('video-task-restored', ({detail}) => {
      if (id === detail.id && loaded && detail.kind === 'detect-faces' && detail.result) {
        candidates = detail.result.candidates || []; paint();
        status(candidates.length ? 'Escolha um rosto sugerido.' : 'Nenhum rosto encontrado. Ajuste a área manualmente.');
      }
    }, {signal:listeners.signal});
    enable(false);
    async function json(url, opts) { const response = await fetch(url, opts); if (!response.ok) { let detail; try { const body = await response.json(); if (typeof body.detail === 'string') detail = body.detail; } catch (_) {} throw Object.assign(new Error(detail || 'Não foi possível concluir esta ação.'), {status:response.status}); } return response.json(); }
    const send = (url, method, body) => json(url, {method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
    function notify() { revision++; onChange(); clearTimeout(saveTimer); if (loaded) saveTimer = setTimeout(() => flush().catch(e => status(e.message)), 700); }
    function paint() {
      $('.le-presets').innerHTML = [...presets, ...custom].map(p => `<div class="le-card-wrap"><button type="button" class="le-card" data-preset="${esc(p.id)}" aria-label="${esc(p.name)}: ${esc(p.description || '')}"><span class="le-thumb le-thumb-${esc(p.layout_config?.mode || 'standard')}" aria-hidden="true"><i></i><b></b><em></em></span><strong>${esc(p.name)}</strong><small>${esc(p.description || '')}</small></button>${custom.includes(p) ? `<button type="button" class="le-delete" data-delete="${esc(p.id)}" aria-label="Excluir modelo ${esc(p.name)}">×</button>` : ''}</div>`).join('');
      root.querySelectorAll('[data-field]').forEach(el => { const v = config[el.dataset.field]; el.value = el.type === 'range' ? Math.round(v * 100) : v; if (el.nextElementSibling?.tagName === 'OUTPUT') el.nextElementSibling.textContent = `${el.value}%`; });
      root.querySelectorAll('.le-split').forEach(el => el.hidden = !['split','split_caption'].includes(config.mode));
      root.querySelectorAll('.le-pip').forEach(el => el.hidden = config.mode !== 'pip');
      root.querySelectorAll('.le-caption-position').forEach(el => el.hidden = ['split','split_caption'].includes(config.mode));
      root.querySelectorAll('.le-composite').forEach(el => el.hidden = config.mode === 'standard');
      $('.le-areas').hidden = config.mode === 'standard';
      select.hidden = config.mode !== 'standard'; if (selectLabel) selectLabel.hidden = select.hidden;
      root.querySelectorAll('[data-region]').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.region === region)));
      const box = config[region]; const graphic = $('.le-box'); graphic.hidden = !box;
      if (box) { Object.assign(graphic.style, {left:`${box.x*100}%`,top:`${box.y*100}%`,width:`${box.w*100}%`,height:`${box.h*100}%`}); }
      root.querySelectorAll('[data-coordinate]').forEach(el => { el.value = box ? pct(box[el.dataset.coordinate]) : ''; });
      $('.le-candidates').innerHTML = candidates.map((c, i) => `<button type="button" data-candidate="${i}">Rosto ${i+1}${c.likely_facecam ? ' · possível câmera' : ''}</button>`).join('');
    }
    function frame(at = 0) { $('.le-frame').src = id ? `/api/videos/${encodeURIComponent(id)}/frame?at=${encodeURIComponent(at)}` : ''; }
    async function load() {
      if (!id) return;
      const generation = ++requestGeneration; loaded = false; enable(false); status('Carregando modelos...');
      try {
        const [saved, catalog] = await Promise.all([json(`/api/videos/${encodeURIComponent(id)}/layout`),json('/api/layout-presets')]);
        if (generation !== requestGeneration || destroyed) return;
        preferredSubtitles = typeof saved.subtitles === 'boolean' ? saved.subtitles : null;
        config = normalize(saved.layout_config); select.value = saved.layout || 'blur'; presets = catalog.presets || []; custom = catalog.custom_presets || []; candidates = activity.detection(id)?.candidates || []; revision = 0; persistedRevision = 0; dirtyCoordinates = false; loaded = true; enable(true); restoreSubtitles(); frame(); paint(); status(''); onChange();
      } catch (e) { if (generation === requestGeneration && !destroyed) { status(e.message || 'Não foi possível carregar os modelos. Tente novamente.'); throw e; } }
    }
    async function flush() {
      clearTimeout(saveTimer);
      if (destroyed || !id || !loaded) throw new Error('Carregue as opções de composição antes de continuar.');
      if (dirtyCoordinates) root.querySelector('[data-coordinate]').dispatchEvent(new Event('change', {bubbles:true}));
      if ($('.le-error').textContent) throw new Error('Corrija os valores da área selecionada.');
      const target = id;
      while (!destroyed && revision > persistedRevision && target === id) {
        const version = revision, payload = {layout:select.value, layout_config:clone(config), subtitles:subtitleSetting()};
        const operation = saveQueue.catch(() => {}).then(() => send(`/api/videos/${encodeURIComponent(target)}/layout`, 'PUT', payload));
        saveQueue = operation;
        await operation;
        if (!destroyed && target === id) persistedRevision = Math.max(persistedRevision, version);
      }
    }
    root.addEventListener('change', e => {
      if (!loaded || destroyed) return;
      const el = e.target, field = el.dataset.field;
      if (field) { config[field] = el.type === 'range' ? Number(el.value)/100 : el.value; paint(); notify(); return; }
      if (el.dataset.coordinate) {
        dirtyCoordinates = false;
        const inputs = [...root.querySelectorAll('[data-coordinate]')]; const values = Object.fromEntries(inputs.map(i => [i.dataset.coordinate, Number(i.value)/100]));
        if (inputs.some(i => !i.value || !i.checkValidity()) || values.x+values.w > 1.000001 || values.y+values.h > 1.000001 || values.w < .02 || values.h < .02) { $('.le-error').textContent = 'Confira os valores: a área deve caber dentro do vídeo.'; notify(); return; }
        $('.le-error').textContent = ''; config[region] = values; paint(); notify();
      }
    }, {signal:listeners.signal});
    root.addEventListener('input', e => { if (!loaded || destroyed) return; if (e.target.dataset.coordinate) { dirtyCoordinates = true; notify(); return; } if (e.target.type === 'range' && e.target.dataset.field) { config[e.target.dataset.field] = Number(e.target.value)/100; e.target.nextElementSibling.textContent = `${e.target.value}%`; notify(); } }, {signal:listeners.signal});
    const legacyChange = () => { if (loaded && !destroyed) notify(); }; select.addEventListener('change', legacyChange, {signal:listeners.signal});
    root.addEventListener('click', async e => {
      if (!loaded || destroyed) return;
      const actionGeneration = requestGeneration, actionId = id;
      const currentAction = () => !destroyed && loaded && actionGeneration === requestGeneration && actionId === id;
      const preset = e.target.closest('[data-preset]');
      if (preset) { const p = [...presets,...custom].find(x => String(x.id) === preset.dataset.preset); if (!p) return; config = normalize(p.layout_config); select.value = p.layout || 'blur'; if (typeof p.subtitles === 'boolean') { preferredSubtitles = p.subtitles; restoreSubtitles(); } if (config.mode !== 'standard' && !config.face) { region = 'face'; $('.le-areas').open = true; status('Selecione ou ajuste a área do rosto antes de gerar a prévia.'); } else status(''); paint(); notify(); return; }
      const del = e.target.closest('[data-delete]');
      if (del) { try { await json(`/api/layout-presets/${encodeURIComponent(del.dataset.delete)}`, {method:'DELETE'}); if (!currentAction()) return; custom = custom.filter(p => String(p.id) !== del.dataset.delete); paint(); } catch (error) { if (currentAction()) status(error.message); } return; }
      const which = e.target.closest('[data-region]'); if (which) { region = which.dataset.region; paint(); return; }
      const candidate = e.target.closest('[data-candidate]'); if (candidate) { const c = candidates[Number(candidate.dataset.candidate)]; region = 'face'; config.face = clone(c.region); frame(c.timestamp || 0); paint(); notify(); return; }
      if (e.target.closest('.le-use-frame')) { const at = Number(getTime()); frame(Number.isFinite(at) && at >= 0 ? at : 0); status('Quadro do player selecionado.'); return; }
      if (e.target.closest('.le-reset')) { config[region] = {x:0,y:0,w:1,h:1}; $('.le-error').textContent = ''; paint(); notify(); return; }
      if (e.target.closest('.le-save')) { const name = $('.le-name').value.trim(); if (!name) { status('Digite um nome para salvar o modelo.'); return; } try { await flush(); if (!currentAction()) return; await send('/api/layout-presets','POST',{name, layout:select.value, layout_config:clone(config), subtitles:subtitleSetting()}); if (!currentAction()) return; const catalog = await json('/api/layout-presets'); if (!currentAction()) return; custom = catalog.custom_presets || []; $('.le-name').value = ''; paint(); status('Modelo salvo.'); } catch (error) { if (currentAction()) status(error.message); } return; }
      if (e.target.closest('.le-detect')) {
        const job = activity.reserve(id, 'detect-faces');
        if (!job) return;
        status('Procurando rostos...');
        try {
          job.submitted = true;
          const started = await send(`/api/videos/${encodeURIComponent(id)}/faces`, 'POST', {});
          const result = await activity.track(job, started.task_id, message => { if (currentAction()) status(message); });
          if (!currentAction()) return;
          candidates = result?.candidates || []; paint();
          status(candidates.length ? 'Escolha um rosto sugerido ou ajuste a área manualmente.' : 'Nenhum rosto encontrado. Você pode ajustar a área manualmente.');
        } catch (error) {
          activity.failure(job,error);
          if (currentAction()) status(error.message || 'Não foi possível procurar rostos.');
        } finally { activity.release(job); }
      }
    }, {signal:listeners.signal});
    let drag = null;
    const stage = $('.le-stage');
    const point = e => { const bounds = stage.getBoundingClientRect(); if (!bounds.width || !bounds.height) return null; return {x:clamp((e.clientX-bounds.left)/bounds.width,0,1), y:clamp((e.clientY-bounds.top)/bounds.height,0,1)}; };
    stage.addEventListener('pointerdown', e => {
      if (!loaded || destroyed || (e.button !== undefined && e.button !== 0)) return;
      const p = point(e); if (!p) return;
      const box = config[region] ? clone(config[region]) : null;
      const mode = e.target.closest('.le-resize') && box ? 'resize' : e.target.closest('.le-box') && box ? 'move' : 'draw';
      drag = {start:p, box, region, mode, moved:false};
      if (stage.setPointerCapture) stage.setPointerCapture(e.pointerId);
      e.preventDefault();
    }, {signal:listeners.signal});
    stage.addEventListener('pointermove', e => {
      if (!loaded || destroyed || !drag) return;
      const p = point(e); if (!p) return;
      const dx = p.x-drag.start.x, dy = p.y-drag.start.y;
      if (Math.abs(dx) < .001 && Math.abs(dy) < .001 && !drag.moved) return;
      drag.moved = true;
      if (drag.mode === 'move') config[drag.region] = {...drag.box,x:clamp(drag.box.x+dx,0,1-drag.box.w),y:clamp(drag.box.y+dy,0,1-drag.box.h)};
      else if (drag.mode === 'resize') config[drag.region] = {...drag.box,w:clamp(drag.box.w+dx,.02,1-drag.box.x),h:clamp(drag.box.h+dy,.02,1-drag.box.y)};
      else {
        const x=clamp(Math.min(drag.start.x,p.x),0,.98), y=clamp(Math.min(drag.start.y,p.y),0,.98);
        config[drag.region] = {x,y,w:clamp(Math.abs(dx),.02,1-x),h:clamp(Math.abs(dy),.02,1-y)};
      }
      paint();
    }, {signal:listeners.signal});
    function endDrag() { if (drag && loaded && !destroyed) { const changed = drag.moved; drag = null; if (changed) { $('.le-error').textContent = ''; notify(); } } }
    stage.addEventListener('pointerup',endDrag,{signal:listeners.signal});
    stage.addEventListener('pointercancel',endDrag,{signal:listeners.signal});
    paint();
    return {load, subtitlesChanged, restoreSubtitles, getConfig:() => clone(config), getLayout:() => select.value, isReady:() => loaded, flush, setVideo:async next => { ++requestGeneration; enable(false); if (id && loaded) await flush(); if (destroyed) return; id = next; loaded = false; $('.le-detect').disabled = false; config=defaults(); candidates=[]; if (id) await load(); }, destroy:() => { destroyed=true; loaded=false; requestGeneration++; clearTimeout(saveTimer); listeners.abort(); select.disabled=false; root.innerHTML=''; }};
  }
  window.LayoutEditor = {create, normalize, same};
})();
