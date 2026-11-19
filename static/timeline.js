(() => {
  "use strict";
  const match = location.pathname.match(/^\/studio\/([0-9a-f]{32})$/);
  const videoId = match && match[1];
  const $ = (selector) => document.querySelector(selector);
  const state = { timeline: null, duration: 0, selectedId: null, words: [], transcriptEngine: "none",
    canUndo: false, canRedo: false, exports: {}, playing: false, playIndex: 0 };
  const video = $("#source-player");
  let toastTimer = null;

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
  async function pollTask(taskId, onProgress = null) {
    const deadline = Date.now() + 20 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 950));
      const task = await api(`/api/tasks/${taskId}`);
      if (task.status === "done") return task.result;
      if (["error", "interrupted"].includes(task.status)) throw new Error(task.error || "Tarefa interrompida");
      if (onProgress) onProgress(task.status);
    }
    throw new Error("Tempo excedido na consulta. Confira o estado do servidor antes de repetir a exportacao.");
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
      ? `Legendas em ${state.transcriptEngine}: ${state.words.length} palavras disponíveis · exportação em SRT`
      : "Transcreva localmente para criar legendas; requer faster-whisper instalado";
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
    const data = await api(`/api/videos/${videoId}/timeline/exports`);
    state.exports = data.exports || {};
    updateApproval();
  }
  function updateApproval() {
    const options = currentOptions();
    const preview = state.exports.preview;
    const matching = preview && preview.version === state.timeline?.version &&
      preview.layout === options.layout && preview.subtitles === options.subtitles;
    $("#render-preview").disabled = !$("#approve-selection").checked;
    $("#render-final").disabled = !matching || !$("#approve-preview").checked;
    const previewUrl = `/api/videos/${videoId}/timeline/rendered/preview`;
    const finalUrl = `/api/videos/${videoId}/timeline/rendered/final`;
    const freshPreview = preview && preview.version === state.timeline?.version;
    $("#preview-status").textContent = freshPreview
      ? `Prévia ${fmtTime(preview.probe.duration)} disponível (${preview.layout}${preview.subtitles ? ", com legendas" : ""}).`
      : "Prévia ainda não gerada para esta montagem.";
    $("#preview-link").classList.toggle("hidden", !freshPreview);
    $("#preview-video").classList.toggle("hidden", !freshPreview);
    if (freshPreview) {
      $("#preview-link").href = previewUrl;
      if (!$("#preview-video").src.includes("timeline/rendered/preview")) $("#preview-video").src = previewUrl;
    } else {
      $("#preview-video").removeAttribute("src");
      $("#preview-video").load();
    }
    const final = state.exports.final;
    const freshFinal = final && final.version === state.timeline?.version;
    $("#final-link").classList.toggle("hidden", !freshFinal);
    $("#final-status").textContent = freshFinal
      ? `Final ${fmtTime(final.probe.duration)} disponível em 1080 × 1920.`
      : matching ? "Prévia pronta; revise o vídeo acima e confirme a aprovação." : "Gere uma prévia nesta configuração para liberar o final.";
    if (freshFinal) $("#final-link").href = finalUrl;
  }
  function currentOptions() {
    return {layout:$("#export-layout").value, subtitles:$("#export-subtitles").checked};
  }
  async function exportTimeline(quality) {
    const button = quality === "preview" ? $("#render-preview") : $("#render-final");
    setBusy(button, true, "Processando FFmpeg...");
    try {
      const result = await api(`/api/videos/${videoId}/timeline/render/${quality}`, {method:"POST", body:JSON.stringify({
        ...currentOptions(), approved_selection:$("#approve-selection").checked,
        approved_preview:$("#approve-preview").checked,
      })});
      await pollTask(result.task_id, (status) => { button.textContent = `Renderizando (${status})...`; });
      await loadExports();
      if (quality === "preview") {
        $("#preview-video").src = `/api/videos/${videoId}/timeline/rendered/preview?v=${state.timeline.version}`;
        $("#preview-video").load();
      }
      notice(`${quality === "preview" ? "Prévia" : "Exportação final"} gerada no servidor.`, "ok");
    } catch (error) { notice(error.message, "error"); }
    finally { setBusy(button, false); updateApproval(); }
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
      : `<p class="hint">${state.words.length ? "Nenhuma fala encontrada para a busca." : "Não há transcrição disponível. Use ‘Transcrever localmente’ quando o módulo estiver instalado."}</p>`;
    $("#transcript-info").textContent = state.words.length
      ? `${state.words.length} palavras · motor: ${state.transcriptEngine} ${shown.length < matches.length ? `· mostrando ${shown.length}/${matches.length}` : ""}`
      : "Sem transcrição disponível; o editor de cortes e a exportação funcionam sem ela.";
    $("#download-srt").classList.toggle("hidden", !state.words.length);
    $("#download-srt").href = `/api/videos/${videoId}/timeline.srt`;
  }
  async function loadTranscript() {
    const result = await api(`/api/videos/${videoId}/transcript`);
    state.words = result.words || [];
    state.transcriptEngine = result.engine || "none";
    showWords();
    $("#caption-track").textContent = state.words.length
      ? `${state.words.length} palavras disponíveis para legendas e exportação SRT` : "Sem transcrição";
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
    const button = $("#transcribe-local");
    setBusy(button, true, "Transcrevendo localmente...");
    try {
      const result = await api(`/api/videos/${videoId}/transcript`, {method:"POST", body:"{}"});
      await pollTask(result.task_id);
      await loadTranscript();
      notice("Transcrição local concluída.", "ok");
    } catch (error) { notice(error.message, "error"); }
    finally { setBusy(button, false); }
  });
  $("#render-preview").addEventListener("click", () => exportTimeline("preview"));
  $("#render-final").addEventListener("click", () => exportTimeline("final"));
  for (const selector of ["#export-layout", "#export-subtitles", "#approve-selection", "#approve-preview"])
    $(selector).addEventListener("change", updateApproval);

  async function init() {
    if (!videoId) return notice("Identificador de vídeo inválido.", "error");
    video.src = `/api/videos/${videoId}/stream`;
    try {
      const list = await api("/api/videos");
      const item = (list.videos || []).find((v) => v.id === videoId);
      if (item) $("#project-title").textContent = `${item.name} · ${fmtTime(item.duration)} · ${item.width}×${item.height}`;
      await loadTimeline();
      const cfg = await api("/api/config");
      if (!cfg.transcribe_available) {
        const transcribeButton = $("#transcribe-local");
        transcribeButton.disabled = true;
        transcribeButton.textContent = "Transcrição não instalada";
        transcribeButton.title = "Instale faster-whisper no servidor e reinicie o AutoClips.";
      }
      $("#add-end").value = Math.min(15, state.duration).toFixed(1);
      await loadTranscript();
      await loadExports();
    } catch (error) {
      notice(`Não foi possível carregar o projeto: ${error.message}`, "error");
    }
  }
  init();
})();
