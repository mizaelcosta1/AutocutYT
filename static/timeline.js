(() => {
  "use strict";
  const match = location.pathname.match(/^\/studio\/([0-9a-f]{32})$/);
  const videoId = match && match[1];
  const $ = (selector) => document.querySelector(selector);
  const defaultStyle = {font_family:"DejaVu Sans", font_size:56, color:"#FFFFFF"};
  const supportedFonts = new Set(Array.from($("#subtitle-font").options, (option) => option.value));
  const state = { timeline: null, duration: 0, selectedId: null, words: [], transcriptEngine: "none",
    canUndo: false, canRedo: false, exports: {}, playing: false, playIndex: 0,
    transcribeAvailable: false, transcribing: false, transcriptState: {}, transcriptNotice: "",
    rendering: null, styleLoaded: false, savedStyle: null };
  const video = $("#source-player");
  let toastTimer = null;
  let styleSaveTimer = null, styleSavePromise = null, styleRevision = 0;
  let transcriptRequest = 0, exportsRequest = 0, transcriptRefreshTimer = null;
  let layoutEditor = null;
  const styleStatus = document.createElement("p");
  styleStatus.id = "subtitle-save-status";
  styleStatus.className = "hint";
  styleStatus.setAttribute("role", "status");
  styleStatus.setAttribute("aria-live", "polite");
  $("#subtitle-style-help").after(styleStatus);
  const styleInputs = [$("#subtitle-font"), $("#subtitle-size"), $("#subtitle-color")];
  styleInputs.forEach((input) => input.disabled = true);

  function notice(message, kind = "info") {
    const el = $("#notice");
    el.textContent = message;
    el.className = `notice ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 6200);
  }
  function fmtTime(seconds) {
    const sec = Math.max(0, Number(seconds) || 0);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    return (h ? `${h}:` : "") + `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
  }
  function escapeHtml(text) {
    return String(text ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function parseError(data, code) {
    const detail = data && data.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ") || `HTTP ${code}`;
    return `Falha HTTP ${code}`;
  }
  async function api(path, options = {}) {
    const res = await fetch(path, {credentials:"same-origin", ...options,
      headers: {"Content-Type":"application/json", ...(options.headers || {})}});
    let data = null;
    try { data = await res.json(); } catch (_) { /* Non-JSON server error */ }
    if (!res.ok) throw new Error(parseError(data, res.status));
    return data;
  }
  function setBusy(button, busy, label) {
    if (!button) return;
    if (busy) { button.dataset.oldText = button.textContent; button.disabled = true; button.textContent = label; }
    else { button.textContent = button.dataset.oldText || button.textContent; button.disabled = false; }
  }
  function taskMessage(task) {
    const phases = {queued:"Na fila", waiting:"Aguardando o transcritor", running:"Processando",
      loading_model:"Preparando a transcrição", transcribing:"Transcrevendo as falas",
      extracting_audio:"Preparando o áudio", rendering:"Gerando o vídeo", done:"Concluído"};
    const message = task.message || phases[task.phase] || phases[task.status] || "Processando";
    const progress = Number(task.progress);
    return task.progress !== undefined && task.progress !== null && Number.isFinite(progress)
      ? `${message} · ${Math.round(Math.max(0, Math.min(100, progress)))}%` : message;
  }
  async function pollTask(taskId, onProgress = null) {
    // Long recordings can take hours on a CPU. A polling limit is not a job failure.
    const deadline = Date.now() + 12 * 60 * 60 * 1000;
    let failures = 0;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, document.hidden ? 5000 : 1250));
      let task;
      try { task = await api(`/api/tasks/${taskId}`); failures = 0; }
      catch (error) {
        failures += 1;
        if (failures >= 5) throw new Error("Não foi possível acompanhar a tarefa. Ela pode continuar no servidor; atualize esta página para conferir antes de tentar novamente.");
        if (onProgress) onProgress("Reconectando ao processamento...");
        continue;
      }
      if (task.status === "done") return task.result;
      if (["error", "interrupted"].includes(task.status)) throw new Error(task.error || "Tarefa interrompida");
      if (onProgress) onProgress(taskMessage(task));
    }
    throw new Error("O acompanhamento desta página foi encerrado. A tarefa pode continuar no servidor; atualize o projeto para conferir seu resultado.");
  }
  function normalizedStyle(value) {
    return {font_family:supportedFonts.has(value?.font_family) ? value.font_family : defaultStyle.font_family,
      font_size:Number.isInteger(value?.font_size) && value.font_size >= 18 && value.font_size <= 96 ? value.font_size : defaultStyle.font_size,
      color:/^#[0-9a-f]{6}$/i.test(value?.color || "") ? value.color.toUpperCase() : defaultStyle.color};
  }
  function sameStyle(a, b) {
    return Boolean(a && b && a.font_family === b.font_family && a.font_size === b.font_size && a.color.toUpperCase() === b.color.toUpperCase());
  }
  function readStyle() {
    const size = Number($("#subtitle-size").value);
    const valid = Number.isInteger(size) && size >= 18 && size <= 96;
    $("#subtitle-size").setCustomValidity(valid ? "" : "Escolha um tamanho inteiro entre 18 e 96.");
    if (!valid || !supportedFonts.has($("#subtitle-font").value)) return null;
    return {font_family:$("#subtitle-font").value, font_size:size, color:$("#subtitle-color").value.toUpperCase()};
  }
  function applyStyle(value) {
    const style = normalizedStyle(value);
    $("#subtitle-font").value = style.font_family;
    $("#subtitle-size").value = style.font_size;
    $("#subtitle-color").value = style.color.toLowerCase();
    updateStyleSample();
  }
  function updateStyleSample() {
    const style = readStyle();
    if (!style) return;
    const sample = $("#subtitle-sample");
    sample.style.fontFamily = `"${style.font_family}", sans-serif`;
    sample.style.fontSize = `${Math.round(style.font_size * 720 / 1080 * 100) / 100}px`;
    sample.style.color = style.color;
  }
  async function flushStyle() {
    clearTimeout(styleSaveTimer);
    if (!state.styleLoaded) throw new Error("Aguarde o carregamento das opções de legenda.");
    // Serialize writes and recheck the controls after every response. A late save
    // must never overwrite a newer color/font or race the render request.
    while (true) {
      if (styleSavePromise) { await styleSavePromise; continue; }
      const wanted = readStyle();
      if (!wanted) throw new Error("Escolha um tamanho de legenda inteiro entre 18 e 96.");
      if (sameStyle(wanted, state.savedStyle)) return wanted;
      const revision = styleRevision;
      styleStatus.textContent = "Salvando aparência das legendas...";
      const pending = api(`/api/videos/${videoId}/subtitle-style`, {method:"PUT", body:JSON.stringify(wanted)})
        .then((result) => {
          state.savedStyle = normalizedStyle(result.subtitle_style || wanted);
          if (revision === styleRevision) styleStatus.textContent = "Aparência salva neste vídeo.";
        });
      styleSavePromise = pending;
      try { await pending; }
      catch (error) { styleStatus.textContent = `Não foi possível salvar: ${error.message}`; throw error; }
      finally { if (styleSavePromise === pending) styleSavePromise = null; }
    }
  }
  function styleChanged() {
    styleRevision += 1;
    clearTimeout(styleSaveTimer);
    $("#approve-preview").checked = false;
    updateStyleSample();
    updateApproval();
    if (!readStyle()) { styleStatus.textContent = "Escolha um tamanho inteiro entre 18 e 96."; return; }
    styleStatus.textContent = "Aplicando alteração...";
    styleSaveTimer = setTimeout(() => flushStyle().catch((error) => notice(error.message, "error")), 400);
  }
  function segments() { return state.timeline?.segments || []; }
  function selected() { return segments().find((segment) => segment.id === state.selectedId) || null; }
  function updateSelection(seek = false) {
    const seg = selected();
    if (seg) {
      $("#selected-label").textContent = `Trecho ${segments().indexOf(seg) + 1}`;
      $("#trim-start").value = seg.source_in;
      $("#trim-end").value = seg.source_out;
      if (seek) video.currentTime = seg.source_in;
    } else {
      $("#selected-label").textContent = "—";
    }
    document.querySelectorAll("[data-seg]").forEach((el) => el.classList.toggle("selected", el.dataset.seg === state.selectedId));
  }
  function refreshTimeline(data) {
    state.timeline = data.timeline;
    state.duration = data.timeline.source_duration;
    state.canUndo = Boolean(data.can_undo);
    state.canRedo = Boolean(data.can_redo);
    if (!selected()) state.selectedId = segments()[0]?.id || null;
    $("#segment-count").textContent = `${segments().length} trecho(s)`;
    $("#edited-time").textContent = `${fmtTime(data.edited_duration)} editados`;
    $("#timeline-version").textContent = `Versão ${state.timeline.version}`;
    $("#undo").disabled = !state.canUndo;
    $("#redo").disabled = !state.canRedo;
    $("#time-indicator").textContent = `${fmtTime(video.currentTime)} / ${fmtTime(state.duration)}`;
    const track = $("#video-track");
    track.innerHTML = segments().map((segment, i) => {
      const dur = segment.source_out - segment.source_in;
      return `<button type="button" class="timeline-clip" style="flex:${Math.max(1, dur / 3)} 1 0" data-seg="${segment.id}"
          aria-label="Selecionar trecho ${i + 1} entre ${fmtTime(segment.source_in)} e ${fmtTime(segment.source_out)}">
          <span>🎞 Trecho ${i + 1}</span><small>${fmtTime(segment.source_in)} → ${fmtTime(segment.source_out)} · ${dur.toFixed(1)}s</small></button>`;
    }).join("");
    track.querySelectorAll("[data-seg]").forEach((btn) => btn.addEventListener("click", () => {
      state.selectedId = btn.dataset.seg;
      state.playing = false;
      updateSelection(true);
    }));
    updateSelection(false);
    $("#caption-track").textContent = state.words.length
      ? `${state.words.length} palavras disponíveis · legendas SRT e VTT`
      : "Gere a transcrição abaixo para criar legendas";
    updateApproval();
  }
  function invalidateApproval() {
    $("#approve-selection").checked = false;
    $("#approve-preview").checked = false;
    state.playing = false;
    state.exports = {};
    updateApproval();
  }
  async function command(action, values = {}) {
    try {
      const data = await api(`/api/videos/${videoId}/timeline`, {
        method:"POST", body: JSON.stringify({ action, ...values, expected_version:state.timeline.version }),
      });
      invalidateApproval();
      refreshTimeline(data);
      await loadExports();
      notice("Alteração salva. A montagem foi atualizada.", "ok");
    } catch (error) {
      notice(error.message, "error");
      if (error.message.includes("foi alterada")) await loadTimeline();
    }
  }
  async function loadTimeline() {
    const data = await api(`/api/videos/${videoId}/timeline`);
    refreshTimeline(data);
    $("#add-end").max = data.timeline.source_duration;
    $("#add-start").max = data.timeline.source_duration;
    $("#trim-start").max = data.timeline.source_duration;
    $("#trim-end").max = data.timeline.source_duration;
  }
  async function loadExports() {
    const request = ++exportsRequest;
    const data = await api(`/api/videos/${videoId}/timeline/exports`);
    if (request !== exportsRequest) return;
    if (state.exports.preview?.render_id !== data.exports?.preview?.render_id)
      $("#approve-preview").checked = false;
    state.exports = data.exports || {};
    updateApproval();
  }
  function exportMatches(meta, options = currentOptions()) {
    if (layoutEditor && !layoutEditor.isReady()) return false;
    return Boolean(meta && meta.version === state.timeline?.version && meta.layout === options.layout &&
      meta.subtitles === options.subtitles && (!window.LayoutEditor || window.LayoutEditor.same(meta.layout_config, options.layout_config)) &&
      (!options.subtitles || sameStyle(normalizedStyle(meta.subtitle_style), options.subtitle_style)));
  }
  function renderedUrl(quality, meta) {
    return `/api/videos/${videoId}/timeline/rendered/${quality}${meta.render_id ? `?render_id=${encodeURIComponent(meta.render_id)}` : ""}`;
  }
  function updateApproval() {
    const options = currentOptions();
    const preview = state.exports.preview;
    const matching = exportMatches(preview, options);
    const busy = Boolean(state.rendering || state.transcribing || state.transcriptState.status === "running");
    $("#render-preview").disabled = busy || !state.timeline || !state.styleLoaded || !options.subtitle_style
      || (layoutEditor && !layoutEditor.isReady()) || !$("#approve-selection").checked;
    $("#approve-preview").disabled = busy || !matching;
    if (!matching) $("#approve-preview").checked = false;
    $("#render-final").disabled = busy || !matching || !$("#approve-preview").checked;
    const layouts = {blur:"fundo desfocado", fit:"vídeo com bordas", crop:"recorte central"};
    const compositions = {split:"duas telas", split_caption:"rosto, legenda e jogo", pip:"rosto sobre o vídeo"};
    const layoutLabel = compositions[preview?.layout_config?.mode] || layouts[preview?.layout];
    if (state.rendering !== "preview") $("#preview-status").textContent = matching
      ? `Prévia de ${fmtTime(preview.probe?.duration)} disponível: ${layoutLabel || "vídeo"} · ${preview.layout_config?.aspect || "9:16"}${preview.subtitles ? ", com legendas" : ""}.`
      : preview ? "Configuração alterada. Gere uma nova prévia para revisar." : "Prévia ainda não gerada para esta montagem.";
    $("#preview-link").classList.toggle("hidden", !matching);
    $("#preview-video").classList.toggle("hidden", !matching);
    if (matching) {
      const url = renderedUrl("preview", preview);
      $("#preview-link").href = url;
      if ($("#preview-video").getAttribute("src") !== url) {
        $("#preview-video").src = url;
        $("#preview-video").load();
      }
    } else if ($("#preview-video").hasAttribute("src")) {
      $("#preview-link").removeAttribute("href");
      $("#preview-video").removeAttribute("src");
      $("#preview-video").load();
    }
    const final = state.exports.final;
    const freshFinal = exportMatches(final, options);
    $("#final-link").classList.toggle("hidden", !freshFinal);
    if (state.rendering !== "final") $("#final-status").textContent = freshFinal
      ? `Vídeo final de ${fmtTime(final.probe?.duration)} disponível em ${final.probe?.width || (final.layout_config?.aspect === "16:9" ? 1920 : 1080)} × ${final.probe?.height || (final.layout_config?.aspect === "16:9" ? 1080 : 1920)}.`
      : matching ? "Prévia pronta; revise o vídeo acima e confirme a aprovação." : "Gere uma prévia nesta configuração para liberar o final.";
    if (freshFinal) $("#final-link").href = renderedUrl("final", final);
    else $("#final-link").removeAttribute("href");
  }
  function currentOptions() {
    return {layout:$("#export-layout").value, subtitles:$("#export-subtitles").checked, subtitle_style:readStyle(),
      ...(layoutEditor ? {layout_config:layoutEditor.getConfig()} : {})};
  }
  async function exportTimeline(quality) {
    if (state.rendering || state.transcribing) return;
    const button = quality === "preview" ? $("#render-preview") : $("#render-final");
    const status = quality === "preview" ? $("#preview-status") : $("#final-status");
    state.rendering = quality;
    setBusy(button, true, "Preparando vídeo...");
    updateApproval();
    updateTranscriptionButton();
    try {
      await flushStyle();
      if (layoutEditor) await layoutEditor.flush();
      const options = currentOptions();
      if (quality === "final" && (!exportMatches(state.exports.preview, options) || !$("#approve-preview").checked))
        throw new Error("Gere e aprove uma prévia com estas opções antes de exportar o vídeo final.");
      if (quality === "preview") $("#approve-preview").checked = false;
      const result = await api(`/api/videos/${videoId}/timeline/render/${quality}`, {method:"POST", body:JSON.stringify({
        ...options, approved_selection:$("#approve-selection").checked,
        approved_preview:$("#approve-preview").checked,
        approved_preview_id:quality === "final" ? state.exports.preview?.render_id || null : null,
      })});
      await pollTask(result.task_id, (message) => { status.textContent = message; });
      await loadExports();
      notice(`${quality === "preview" ? "Prévia" : "Exportação final"} gerada no servidor.`, "ok");
    } catch (error) { button.dataset.lastError = error.message; notice(error.message, "error"); }
    finally {
      state.rendering = null;
      setBusy(button, false);
      updateApproval();
      updateTranscriptionButton();
      if (button.dataset.lastError) { status.textContent = button.dataset.lastError; delete button.dataset.lastError; }
    }
  }
  function seekSource(time) {
    state.playing = false;
    video.currentTime = Number(time) || 0;
    video.play().catch(() => {});
  }
  function runMontage() {
    if (!segments().length) return;
    state.playIndex = 0;
    state.playing = true;
    state.selectedId = segments()[0].id;
    video.currentTime = segments()[0].source_in;
    updateSelection(false);
    video.play().catch(() => notice("Use o controle do vídeo para iniciar a reprodução.", "info"));
  }
  video.addEventListener("timeupdate", () => {
    $("#time-indicator").textContent = `${fmtTime(video.currentTime)} / ${fmtTime(state.duration)}`;
    if (!state.playing) return;
    const current = segments()[state.playIndex];
    if (current && video.currentTime >= current.source_out - 0.08) {
      state.playIndex += 1;
      const next = segments()[state.playIndex];
      if (next) {
        state.selectedId = next.id;
        video.currentTime = next.source_in;
        updateSelection(false);
      } else { state.playing = false; video.pause(); }
    }
  });
  video.addEventListener("ended", () => state.playing = false);

  function showWords() {
    const query = $("#word-search").value.toLocaleLowerCase("pt-BR").trim();
    const matches = state.words.map((w, i) => ({...w, i})).filter((w) => !query ||
      String(w.text).toLocaleLowerCase("pt-BR").includes(query));
    const shown = matches.slice(0, 180);
    $("#transcript-words").innerHTML = shown.length
      ? shown.map((w) => `<span class="word"><button type="button" data-seek="${w.i}" title="Ver fala em ${fmtTime(w.start)}">${escapeHtml(w.text)} <small>${fmtTime(w.start)}</small></button><button type="button" data-addword="${w.i}" title="Adicionar trecho em torno desta palavra">+</button></span>`).join("")
      : `<p class="hint">${state.words.length ? "Nenhuma fala encontrada para a busca." : "As falas aparecerão aqui depois da transcrição."}</p>`;
    if (!state.transcribing) {
      const status = state.transcriptState.status;
      $("#transcript-info").textContent = status === "running"
        ? state.transcriptState.message || "Gerando transcrição. Você pode continuar organizando os trechos."
        : status === "error" ? `A transcrição não foi concluída. ${state.transcriptState.message || "Tente novamente quando o serviço estiver disponível."}`
        : state.words.length ? `${state.words.length} palavras transcritas${shown.length < matches.length ? ` · mostrando ${shown.length} de ${matches.length}` : ""}.`
        : state.transcriptNotice || (status === "done" ? "A transcrição terminou sem detectar falas." : state.transcribeAvailable ? "Gere a transcrição para criar legendas e editar pelas falas." : "A transcrição está indisponível neste servidor. Os cortes continuam disponíveis.");
    }
    for (const extension of ["srt", "vtt"]) {
      const link = $(`#download-${extension}`);
      link.classList.toggle("hidden", !state.words.length);
      if (state.words.length) link.href = `/api/videos/${videoId}/timeline.${extension}`;
      else link.removeAttribute("href");
    }
    $("#remove-pauses").disabled = !state.words.length || state.transcribing || state.transcriptState.status === "running";
  }
  function updateTranscriptionButton() {
    const button = $("#transcribe-local");
    const running = state.transcribing || state.transcriptState.status === "running";
    button.disabled = !state.transcribeAvailable || running || Boolean(state.rendering);
    button.textContent = running ? "Gerando transcrição..." : state.transcribeAvailable
      ? state.words.length ? "Gerar transcrição novamente" : "Gerar transcrição e legendas"
      : "Transcrição indisponível";
    button.title = state.transcribeAvailable ? "" : "O serviço de transcrição precisa estar disponível no servidor. Você pode continuar cortando e exportando sem legendas.";
  }
  async function loadTranscript() {
    const request = ++transcriptRequest;
    const revision = styleRevision;
    const result = await api(`/api/videos/${videoId}/transcript`);
    if (request !== transcriptRequest) return;
    state.words = result.words || [];
    state.transcriptEngine = result.engine || "none";
    state.transcriptState = result.state || {};
    state.transcriptNotice = result.notice || "";
    if (!state.styleLoaded) {
      state.savedStyle = normalizedStyle(result.subtitle_style);
      if (revision === styleRevision) applyStyle(state.savedStyle);
      state.styleLoaded = true;
      styleInputs.forEach((input) => input.disabled = false);
    }
    showWords();
    updateTranscriptionButton();
    $("#caption-track").textContent = state.words.length
      ? `${state.words.length} palavras disponíveis · legendas SRT e VTT`
      : state.transcriptState.status === "running" ? "Preparando legendas..." : "Gere a transcrição para criar legendas";
    updateApproval();
    clearTimeout(transcriptRefreshTimer);
    if (!state.transcribing && state.transcriptState.status === "running") {
      transcriptRefreshTimer = setTimeout(async () => {
        try { await loadTranscript(); await loadExports(); }
        catch (error) { notice(`Não foi possível atualizar a transcrição: ${error.message}`, "error"); }
      }, document.hidden ? 10000 : 5000);
    }
  }
  $("#transcript-words").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.seek !== undefined) {
      const word = state.words[Number(button.dataset.seek)];
      if (word) seekSource(word.start);
    }
    if (button.dataset.addword !== undefined) {
      const word = state.words[Number(button.dataset.addword)];
      if (word) command("add", {
        source_in: Math.max(0, Math.round((word.start - 2) * 1000) / 1000),
        source_out: Math.min(state.duration, Math.round((word.end + 5) * 1000) / 1000),
        after_id: state.selectedId,
      });
    }
  });
  $("#word-search").addEventListener("input", showWords);
  $("#play-edit").addEventListener("click", runMontage);
  $("#pause-edit").addEventListener("click", () => { state.playing = false; video.pause(); });
  $("#save-trim").addEventListener("click", () => {
    if (!selected()) return;
    command("trim", {id:state.selectedId, source_in:Number($("#trim-start").value), source_out:Number($("#trim-end").value)});
  });
  $("#split-at-playhead").addEventListener("click", () => {
    if (!selected()) return;
    command("split", {id:state.selectedId, at:Math.round(video.currentTime * 1000) / 1000});
  });
  $("#remove-segment").addEventListener("click", () => { if (selected() && confirm("Remover este trecho da montagem? O original não será apagado.")) command("remove", {id:state.selectedId}); });
  $("#move-left").addEventListener("click", () => { if (selected()) command("move", {id:state.selectedId, direction:-1}); });
  $("#move-right").addEventListener("click", () => { if (selected()) command("move", {id:state.selectedId, direction:1}); });
  $("#add-segment").addEventListener("click", () => command("add", {source_in:Number($("#add-start").value), source_out:Number($("#add-end").value), after_id:state.selectedId}));
  $("#restore-project").addEventListener("click", () => { if (confirm("Restaurar o trecho inicial? Será possível desfazer.")) command("restore"); });
  $("#undo").addEventListener("click", () => command("undo"));
  $("#redo").addEventListener("click", () => command("redo"));
  $("#remove-pauses").addEventListener("click", () => {
    if (!state.words.length) return notice("Transcreva o vídeo antes de remover pausas.", "error");
    if (confirm("Remover pausas longas da montagem com base nas falas? Você poderá desfazer."))
      command("remove_pauses", {threshold:Number($("#pause-threshold").value)});
  });
  $("#transcribe-local").addEventListener("click", async () => {
    if (state.transcribing || state.rendering || !state.transcribeAvailable) return;
    state.transcribing = true;
    clearTimeout(transcriptRefreshTimer);
    invalidateApproval();
    updateTranscriptionButton();
    $("#transcript-info").textContent = "Preparando a transcrição...";
    $("#remove-pauses").disabled = true;
    try {
      const result = await api(`/api/videos/${videoId}/transcript`, {method:"POST", body:"{}"});
      await pollTask(result.task_id, (message) => { $("#transcript-info").textContent = message; });
      await loadTranscript();
      await loadExports();
      $("#approve-preview").checked = false;
      notice(state.words.length ? "Transcrição concluída. Legendas SRT e VTT disponíveis." : state.transcriptNotice || "Transcrição concluída, mas nenhuma fala foi detectada.", state.words.length ? "ok" : "info");
    } catch (error) {
      state.transcriptState = {status:"error", message:error.message};
      notice(error.message, "error");
    }
    finally { state.transcribing = false; showWords(); updateTranscriptionButton(); updateApproval(); }
  });
  $("#render-preview").addEventListener("click", () => exportTimeline("preview"));
  $("#render-final").addEventListener("click", () => exportTimeline("final"));
  for (const selector of ["#export-layout", "#export-subtitles"])
    $(selector).addEventListener("change", () => { $("#approve-preview").checked = false; updateApproval(); });
  for (const selector of ["#approve-selection", "#approve-preview"])
    $(selector).addEventListener("change", updateApproval);
  for (const input of styleInputs) input.addEventListener("input", styleChanged);
  for (const link of document.querySelectorAll('a[href="/"]')) link.addEventListener("click", async (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    try {
      if (state.styleLoaded) await flushStyle();
      if (layoutEditor?.isReady()) await layoutEditor.flush();
      window.location.assign("/");
    } catch (error) { notice(`Não foi possível salvar antes de sair: ${error.message}`, "error"); }
  });

  async function init() {
    if (!videoId) return notice("Identificador de vídeo inválido.", "error");
    video.src = `/api/videos/${videoId}/stream`;
    try {
      const list = await api("/api/videos");
      const item = (list.videos || []).find((v) => v.id === videoId);
      if (item) $("#project-title").textContent = `${item.name} · ${fmtTime(item.duration)} · ${item.width}×${item.height}`;
      await loadTimeline();
      const cfg = await api("/api/config");
      state.transcribeAvailable = Boolean(cfg.transcribe_available);
      updateTranscriptionButton();
      $("#add-end").value = Math.min(15, state.duration).toFixed(1);
      await loadTranscript();
      if (window.LayoutEditor) {
        layoutEditor = window.LayoutEditor.create({container:"#layout-editor", videoId, layoutSelect:"#export-layout",
          onChange:() => { $("#approve-preview").checked = false; updateApproval(); },
          getSubtitles:() => $("#export-subtitles").checked,
          getTime:() => video.currentTime || 0,
          onPresetSubtitles:(checked) => { $("#export-subtitles").checked = checked && state.words.length > 0; updateApproval(); }});
        await layoutEditor.load();
      }
      await loadExports();
    } catch (error) {
      notice(`Não foi possível carregar o projeto: ${error.message}`, "error");
    }
  }
  init();
})();
