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
  function create({container, videoId = null, layoutSelect, onChange = () => {}, onPresetSubtitles = () => {}, getSubtitles = () => undefined, getSubtitleStyle = () => null, getSubtitleText = () => '', getTime = () => 0, getDuration = () => Infinity}) {
    const root = typeof container === 'string' ? document.querySelector(container) : container;
    const select = typeof layoutSelect === 'string' ? document.querySelector(layoutSelect) : layoutSelect;
    if (!root || !select) throw new Error('Editor de composição sem elemento necessário.');
    let id = videoId, config = defaults(), presets = [], custom = [], region = 'face', candidates = [], activePanel = 'models', destroyed = false, saveTimer, requestGeneration = 0, dirtyCoordinates = false, loaded = false, revision = 0, persistedRevision = 0, saveQueue = Promise.resolve();
    let preferredSubtitles = null, frameTime = 0, frameReady = false, frameFailed = false, drag = null;
    const restoreSubtitles = () => {
      if (loaded && typeof preferredSubtitles === 'boolean') onPresetSubtitles(preferredSubtitles);
      if (loaded) refreshPreview();
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
    root.innerHTML = `<fieldset class="le-shell" disabled>
      <div class="le-head"><div><span class="le-kicker">EDIÇÃO VISUAL</span><strong>Monte seu corte</strong><span>Escolha uma etapa. Os ajustes aparecem somente quando você precisa deles.</span></div><p class="le-status" role="status" aria-live="polite"></p></div>
      <nav class="le-nav" aria-label="Etapas de edição">
        <button type="button" data-panel="models" aria-pressed="true"><span>01</span> Modelos</button>
        <button type="button" data-panel="composition" aria-pressed="false"><span>02</span> Composição</button>
        <button type="button" data-panel="regions" aria-pressed="false"><span>03</span> Rosto e jogo</button>
      </nav>
      <section class="le-panel" data-panel-content="models">
        <div class="le-current-card"><span class="le-current-thumb le-thumb-standard" aria-hidden="true"><i></i><b></b><em></em></span><div><small>MODELO ATUAL</small><strong class="le-current-name">Personalizado</strong><p class="le-current-desc">Escolha uma composição pronta ou continue com seus próprios ajustes.</p></div></div>
        <div class="le-model-actions"><button type="button" class="le-open-presets">Escolher modelo</button><button type="button" class="le-save-current">Salvar como meu modelo</button></div>
        <p class="le-help">Os modelos são pontos de partida. Depois de aplicar, você pode ajustar cada área sem alterar o vídeo original.</p>
      </section>
      <section class="le-panel" data-panel-content="composition" hidden>
        <div class="le-section-title"><strong>Formato e distribuição</strong><span>Como o vídeo final será organizado.</span></div>
        <div class="le-controls">
          <label>Formato do vídeo<select data-field="aspect"><option value="9:16">Vertical · 9:16</option><option value="16:9">Horizontal · 16:9</option></select></label>
          <label class="le-split">Altura do quadro do rosto<input type="range" min="20" max="65" step="1" data-field="split_ratio"><output></output></label>
          <label class="le-pip">Posição do rosto<select data-field="pip_position"><option value="top-left">Superior esquerda</option><option value="top-right">Superior direita</option><option value="bottom-left">Inferior esquerda</option><option value="bottom-right">Inferior direita</option></select></label>
          <label class="le-pip">Tamanho do rosto<input type="range" min="18" max="45" step="1" data-field="pip_scale"><output></output></label>
          <label class="le-composite">Ajuste da imagem do jogo<select data-field="game_fit"><option value="contain">Mostrar imagem inteira</option><option value="cover">Preencher o quadro</option></select></label>
          <label class="le-caption-position">Posição da legenda<select data-field="caption_position"><option value="bottom">Embaixo</option><option value="middle">No meio</option><option value="top">Em cima</option></select></label>
        </div>
        <p class="le-help">Dica: escolha primeiro o formato e a distribuição. Depois ajuste o rosto e o jogo na etapa seguinte.</p>
      </section>
      <section class="le-panel" data-panel-content="regions" hidden>
        <div class="le-section-title"><strong>Escolha as áreas na gravação original</strong><span>Arraste no quadro para desenhar, mover ou redimensionar a região.</span></div>
        <details class="le-areas" open><summary>Selecionar área do rosto ou do jogo</summary>
          <p>Escolha uma área abaixo. No vídeo, arraste para desenhar; arraste a borda para mover; use a alça coral para redimensionar.</p>
          <div class="le-row"><button type="button" data-region="face" aria-pressed="true">Rosto</button><button type="button" data-region="gameplay" aria-pressed="false">Tela do jogo</button></div>
          <div class="le-row le-tools"><button type="button" class="le-detect">Procurar rosto automaticamente</button><button type="button" class="le-use-frame">Usar quadro do player</button></div>
          <div class="le-candidates"></div>
          <div class="le-stage"><img class="le-frame" alt="Quadro da gravação original para selecionar rosto ou jogo"><div class="le-box" tabindex="0" role="group" aria-label="Área selecionada. Arraste para mover ou use a alça para redimensionar."><span class="le-resize" aria-hidden="true"></span></div></div>
          <div class="le-numbers"><label>Esquerda %<input type="number" min="0" max="100" step="0.1" data-coordinate="x"></label><label>Topo %<input type="number" min="0" max="100" step="0.1" data-coordinate="y"></label><label>Largura %<input type="number" min="2" max="100" step="0.1" data-coordinate="w"></label><label>Altura %<input type="number" min="2" max="100" step="0.1" data-coordinate="h"></label></div>
          <p class="le-error" role="status" aria-live="polite"></p><button type="button" class="le-reset">Restaurar área inteira</button>
        </details>
      </section>
      <section class="le-preview" aria-label="Prévia da composição do quadro">
        <div class="le-preview-head"><div><strong>Prévia do quadro</strong><span class="le-preview-meta">Escolha um quadro no player.</span></div><button type="button" class="le-preview-frame">Atualizar quadro da prévia</button></div>
        <div class="le-preview-screen"><canvas class="le-composition" width="432" height="768" role="img" aria-label="Composição do quadro selecionado"></canvas><p class="le-preview-state" role="status" aria-live="polite">Carregando quadro...</p></div>
        <p class="le-preview-help">Os ajustes aparecem neste quadro. A legenda é uma amostra de estilo; fonte e quebra de linha podem variar. Gere a prévia 720p para conferir o vídeo e o áudio.</p>
      </section>
      <dialog class="le-modal" aria-labelledby="le-modal-title"><div class="le-modal-head"><div><span class="le-kicker">GALERIA</span><strong id="le-modal-title">Escolha um modelo pronto</strong><p>Você poderá editar o modelo depois de aplicá-lo.</p></div><button type="button" class="le-close-presets" aria-label="Fechar modelos">×</button></div>
        <div class="le-presets" aria-label="Modelos de composição"></div>
        <div class="le-custom"><label>Nome do meu modelo<input class="le-name" type="text" maxlength="60" placeholder="Ex.: Meu corte de gameplay"></label><button type="button" class="le-save">Salvar modelo</button></div>
      </dialog>
    </fieldset>`;
    const $ = s => root.querySelector(s);
    function setPanel(panel) {
      activePanel = ['models','composition','regions'].includes(panel) ? panel : 'models';
      root.querySelectorAll('[data-panel]').forEach(button => {
        const active = button.dataset.panel === activePanel;
        button.setAttribute('aria-pressed', String(active));
      });
      root.querySelectorAll('[data-panel-content]').forEach(section => {
        section.hidden = section.dataset.panelContent !== activePanel;
      });
    }
    function closePresets() {
      const dialog = $('.le-modal');
      if (dialog?.open && typeof dialog.close === 'function') dialog.close();
      else dialog?.removeAttribute('open');
    }
    function openPresets() {
      const dialog = $('.le-modal');
      if (!dialog) return;
      if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
      else dialog.setAttribute('open','');
    }
    const status = msg => { if (!destroyed) $('.le-status').textContent = msg; };
    const canEdit = () => loaded && !destroyed && !activity.busy(id);
    const enable = active => { active = active && !activity.busy(id); if (!destroyed) { $('.le-shell').disabled = !active; select.disabled = !active; $('.le-stage').setAttribute('aria-disabled', String(!active)); } };
    window.addEventListener('video-activity', ({detail}) => {
      if (id === detail.id) {
        if (activity.busy(id) && drag) { config[drag.region] = drag.box; drag = null; paint(); }
        enable(loaded);
      }
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
    function notify() { revision++; refreshPreview(); onChange(); clearTimeout(saveTimer); if (loaded) saveTimer = setTimeout(() => flush().catch(e => status(e.message)), 700); }
    function paint() {
      $('.le-presets').innerHTML = [...presets, ...custom].map(p => `<div class="le-card-wrap"><button type="button" class="le-card" data-preset="${esc(p.id)}" aria-label="${esc(p.name)}: ${esc(p.description || '')}"><span class="le-thumb le-thumb-${esc(p.layout_config?.mode || 'standard')}" aria-hidden="true"><i></i><b></b><em></em></span><strong>${esc(p.name)}</strong><small>${esc(p.description || '')}</small></button>${custom.includes(p) ? `<button type="button" class="le-delete" data-delete="${esc(p.id)}" aria-label="Excluir modelo ${esc(p.name)}">×</button>` : ''}</div>`).join('');
      const activePreset = custom.find(p => p.layout === select.value && same(p.layout_config,config))
        || presets.find(p => p.layout === select.value && same({...p.layout_config,face:config.face,gameplay:config.gameplay},config));
      $('.le-current-name').textContent = activePreset?.name || 'Personalizado';
      $('.le-current-desc').textContent = activePreset?.description || 'Ajustes próprios. Salve como modelo para reutilizar em outros vídeos.';
      const thumb = $('.le-current-thumb');
      thumb.className = `le-current-thumb le-thumb le-thumb-${esc(config.mode)}`;
      root.querySelectorAll('[data-preset]').forEach(button => button.classList.toggle('is-active', activePreset && button.dataset.preset === String(activePreset.id)));
      setPanel(activePanel);
      root.querySelectorAll('[data-field]').forEach(el => { const v = config[el.dataset.field]; el.value = el.type === 'range' ? Math.round(v * 100) : v; if (el.nextElementSibling?.tagName === 'OUTPUT') el.nextElementSibling.textContent = `${el.value}%`; });
      root.querySelectorAll('.le-split').forEach(el => el.hidden = !['split','split_caption'].includes(config.mode));
      root.querySelectorAll('.le-pip').forEach(el => el.hidden = config.mode !== 'pip');
      root.querySelectorAll('.le-caption-position').forEach(el => el.hidden = ['split','split_caption'].includes(config.mode));
      root.querySelectorAll('.le-composite').forEach(el => el.hidden = config.mode === 'standard');
      select.hidden = config.mode !== 'standard'; if (selectLabel) selectLabel.hidden = select.hidden;
      root.querySelectorAll('[data-region]').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.region === region)));
      const box = config[region]; const graphic = $('.le-box'); graphic.hidden = !box;
      if (box) { Object.assign(graphic.style, {left:`${box.x*100}%`,top:`${box.y*100}%`,width:`${box.w*100}%`,height:`${box.h*100}%`}); }
      root.querySelectorAll('[data-coordinate]').forEach(el => { el.value = box ? pct(box[el.dataset.coordinate]) : ''; });
      $('.le-candidates').innerHTML = candidates.map((c, i) => `<button type="button" data-candidate="${i}">Rosto ${i+1}${c.likely_facecam ? ' · possível câmera' : ''}</button>`).join('');
      refreshPreview();
    }
    function refreshPreview() {
      if (destroyed) return;
      const canvas = $('.le-composition'), source = $('.le-frame'), notice = $('.le-preview-state');
      const width = config.aspect === '16:9' ? 1920 : 1080;
      const height = config.aspect === '16:9' ? 1080 : 1920;
      canvas.dataset.aspect = config.aspect;
      canvas.width = width * .4; canvas.height = height * .4;
      const timestamp = `${String(Math.floor(frameTime / 60)).padStart(2,'0')}:${String(Math.floor(frameTime % 60)).padStart(2,'0')}`;
      $('.le-preview-meta').textContent = `${config.aspect} · quadro em ${timestamp}`;
      const missingFace = config.mode !== 'standard' && !config.face;
      notice.textContent = frameFailed ? 'Não foi possível carregar o quadro. Atualize a prévia para tentar novamente.'
        : !frameReady ? 'Carregando quadro...'
        : missingFace ? 'Selecione a área do rosto na etapa Rosto e jogo.' : '';
      notice.hidden = !notice.textContent;
      // No request is made when changing controls: every panel uses the same
      // decoded image already shown in the source-region editor.
      if (!frameReady || !source.naturalWidth || !source.naturalHeight) return;
      let ctx;
      try { ctx = canvas.getContext('2d'); } catch (_) { /* Rendered previews remain available. */ }
      if (!ctx) { notice.hidden = false; notice.textContent = 'Este navegador não oferece a prévia do quadro. Use a prévia 720p.'; return; }
      ctx.scale(.4,.4);
      ctx.fillStyle = '#000'; ctx.fillRect(0,0,width,height);
      function panel(box, x, y, w, h, fit, empty = false) {
        ctx.fillStyle = '#000'; ctx.fillRect(x,y,w,h);
        if (empty) return;
        const roi = box || {x:0,y:0,w:1,h:1};
        let sx = roi.x * source.naturalWidth, sy = roi.y * source.naturalHeight;
        let sw = roi.w * source.naturalWidth, sh = roi.h * source.naturalHeight;
        if (fit === 'cover') {
          if (sw / sh > w / h) { const next = sh * w / h; sx += (sw-next)/2; sw = next; }
          else { const next = sw * h / w; sy += (sh-next)/2; sh = next; }
          ctx.drawImage(source,sx,sy,sw,sh,x,y,w,h);
        } else {
          const scale = Math.min(w/sw,h/sh), dw = sw*scale, dh = sh*scale;
          ctx.drawImage(source,sx,sy,sw,sh,x+(w-dw)/2,y+(h-dh)/2,dw,dh);
        }
      }
      const even = value => Math.max(2, 2*Math.round(value/2));
      const faceHeight = even(height*config.split_ratio);
      const bandHeight = config.mode === 'split_caption' ? even(height*.1) : 0;
      if (config.mode === 'standard') {
        if (select.value === 'blur') {
          ctx.save(); ctx.filter = 'blur(12px)'; panel(null,0,0,width,height,'cover'); ctx.restore();
          // The foreground must leave the blurred background visible.
          const scale = Math.min(width/source.naturalWidth,height/source.naturalHeight);
          const w = source.naturalWidth*scale, h = source.naturalHeight*scale;
          ctx.drawImage(source,0,0,source.naturalWidth,source.naturalHeight,(width-w)/2,(height-h)/2,w,h);
        } else panel(null,0,0,width,height,select.value === 'crop' ? 'cover' : 'contain');
      } else if (config.mode === 'pip') {
        panel(config.gameplay,0,0,width,height,config.game_fit);
        const w = even(width*config.pip_scale), h = even(w*.75), margin = even(Math.min(width,height)*.03);
        const x = config.pip_position.endsWith('left') ? margin : width-w-margin;
        const y = config.pip_position.startsWith('top') ? margin : height-h-margin;
        panel(config.face,x,y,w,h,'contain',missingFace);
      } else {
        panel(config.face,0,0,width,faceHeight,'contain',missingFace);
        ctx.fillStyle = '#121820'; ctx.fillRect(0,faceHeight,width,bandHeight);
        panel(config.gameplay,0,faceHeight+bandHeight,width,height-faceHeight-bandHeight,config.game_fit);
      }
      if (getSubtitles()) {
        const style = getSubtitleStyle() || {font_family:'DejaVu Sans',font_size:56,color:'#FFFFFF'};
        const fontSize = clamp(Number(style.font_size)||56,18,96);
        const text = String(getSubtitleText(frameTime) || 'A legenda aparece assim').replace(/\s+/g,' ').trim();
        ctx.font = `bold ${fontSize}px "${style.font_family || 'DejaVu Sans'}", sans-serif`;
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
        ctx.strokeStyle = '#101218'; ctx.lineWidth = 6; ctx.fillStyle = /^#[0-9a-f]{6}$/i.test(style.color) ? style.color : '#FFFFFF';
        const words = text.split(' '), lines = []; let line = '';
        for (const word of words) {
          const candidate = line ? `${line} ${word}` : word;
          if (line && ctx.measureText(candidate).width > width-128) { lines.push(line); line = word; }
          else line = candidate;
        }
        if (line) lines.push(line);
        const visible = lines.slice(0,3), lineHeight = fontSize*1.18;
        let y = ['split','split_caption'].includes(config.mode) ? faceHeight+bandHeight/2
          : config.caption_position === 'middle' ? height/2
          : config.caption_position === 'top' ? height*112/1920 + visible.length*lineHeight/2
          : height-height*112/1920-visible.length*lineHeight/2;
        visible.forEach((line,index) => {
          const baseline = y+(index-(visible.length-1)/2)*lineHeight;
          ctx.strokeText(line,width/2,baseline,width-128); ctx.fillText(line,width/2,baseline,width-128);
        });
      }
      canvas.setAttribute('aria-label', `Composição ${config.aspect} do quadro em ${timestamp}${getSubtitles() ? ', com amostra de legenda' : ', sem legendas'}`);
    }
    function frame(at = 0) {
      const duration = Number(getDuration());
      frameTime = Number.isFinite(at) ? Math.max(0,at) : 0;
      if (Number.isFinite(duration) && duration > 0) frameTime = Math.min(frameTime,Math.max(0,duration-.1));
      frameReady = false; frameFailed = false;
      $('.le-frame').src = id ? `/api/videos/${encodeURIComponent(id)}/frame?at=${encodeURIComponent(frameTime)}` : '';
      refreshPreview();
    }
    $('.le-frame').addEventListener('load', () => { frameReady = true; frameFailed = false; refreshPreview(); }, {signal:listeners.signal});
    $('.le-frame').addEventListener('error', () => { frameReady = false; frameFailed = true; refreshPreview(); }, {signal:listeners.signal});
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
      const panel = e.target.closest('[data-panel]');
      if (panel) { setPanel(panel.dataset.panel); return; }
      if (e.target.closest('.le-open-presets')) { openPresets(); return; }
      if (e.target.closest('.le-close-presets')) { closePresets(); return; }
      if (e.target.closest('.le-save-current')) { openPresets(); $('.le-name').focus(); return; }
      const preset = e.target.closest('[data-preset]');
      if (preset) {
        const p = [...presets,...custom].find(x => String(x.id) === preset.dataset.preset); if (!p) return;
        closePresets(); const previous = config; config = normalize(p.layout_config);
        if (presets.includes(p)) for (const key of ['face','gameplay']) if (!config[key] && previous[key]) config[key] = clone(previous[key]);
        dirtyCoordinates = false; $('.le-error').textContent = ''; select.value = p.layout || 'blur';
        if (typeof p.subtitles === 'boolean') { preferredSubtitles = p.subtitles; restoreSubtitles(); }
        if (config.mode !== 'standard' && !config.face) { region = 'face'; setPanel('regions'); $('.le-areas').open = true; status('Selecione ou ajuste a área do rosto antes de gerar a prévia.'); }
        else { setPanel('composition'); status(presets.includes(p) && (previous.face || previous.gameplay) ? 'Modelo aplicado. As áreas do rosto e do jogo foram mantidas.' : 'Modelo aplicado. Ajuste o resultado na etapa Composição.'); }
        paint(); notify(); return;
      }
      const del = e.target.closest('[data-delete]');
      if (del) { try { await json(`/api/layout-presets/${encodeURIComponent(del.dataset.delete)}`, {method:'DELETE'}); if (!currentAction()) return; custom = custom.filter(p => String(p.id) !== del.dataset.delete); paint(); } catch (error) { if (currentAction()) status(error.message); } return; }
      const which = e.target.closest('[data-region]'); if (which) { dirtyCoordinates = false; $('.le-error').textContent = ''; region = which.dataset.region; paint(); return; }
      const candidate = e.target.closest('[data-candidate]'); if (candidate) { const c = candidates[Number(candidate.dataset.candidate)]; dirtyCoordinates = false; $('.le-error').textContent = ''; region = 'face'; config.face = clone(c.region); frame(c.timestamp || 0); paint(); notify(); return; }
      if (e.target.closest('.le-use-frame,.le-preview-frame')) { const at = Number(getTime()); frame(Number.isFinite(at) && at >= 0 ? at : 0); status('Quadro do player selecionado.'); return; }
      if (e.target.closest('.le-reset')) { dirtyCoordinates = false; config[region] = {x:0,y:0,w:1,h:1}; $('.le-error').textContent = ''; paint(); notify(); return; }
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
    const stage = $('.le-stage');
    const point = e => { const bounds = stage.getBoundingClientRect(); if (!bounds.width || !bounds.height) return null; return {x:clamp((e.clientX-bounds.left)/bounds.width,0,1), y:clamp((e.clientY-bounds.top)/bounds.height,0,1)}; };
    stage.addEventListener('pointerdown', e => {
      if (!canEdit() || !frameReady || (e.button !== undefined && e.button !== 0)) return;
      const p = point(e); if (!p) return;
      const box = config[region] ? clone(config[region]) : null;
      const mode = e.target.closest('.le-resize') && box ? 'resize' : e.target.closest('.le-box') && box ? 'move' : 'draw';
      drag = {start:p, box, region, mode, moved:false};
      if (stage.setPointerCapture) stage.setPointerCapture(e.pointerId);
      e.preventDefault();
    }, {signal:listeners.signal});
    stage.addEventListener('pointermove', e => {
      if (!canEdit() || !drag) return;
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
    function endDrag() { if (drag && canEdit()) { const changed = drag.moved; drag = null; if (changed) { dirtyCoordinates = false; $('.le-error').textContent = ''; notify(); } } }
    stage.addEventListener('pointerup',endDrag,{signal:listeners.signal});
    stage.addEventListener('pointercancel',endDrag,{signal:listeners.signal});
    paint();
    return {load, subtitlesChanged, restoreSubtitles, refreshPreview, getConfig:() => clone(config), getLayout:() => select.value, isReady:() => loaded, flush, setVideo:async next => { ++requestGeneration; enable(false); if (id && loaded) await flush(); if (destroyed) return; id = next; loaded = false; frameReady = false; drag = null; $('.le-detect').disabled = false; config=defaults(); candidates=[]; if (id) await load(); }, destroy:() => { destroyed=true; loaded=false; requestGeneration++; clearTimeout(saveTimer); listeners.abort(); select.disabled=false; root.innerHTML=''; }};
  }
  window.LayoutEditor = {create, normalize, same};
})();
