(() => {
  const state = {
    config: null,
    videos: [],
    currentVideo: null,
    manifest: null,
    selectedFile: null,
    youtubeCanUpload: false,
    transcript: null,
    completedAnalysisSeen: null,
  };
  let styleSaveTimer = null;
  let styleSavePromise = Promise.resolve();
  let layoutEditor = null;
  let editorGeneration = 0;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function showNotice(msg, kind = "info") {
    const el = $("#notice");
    if (!el) return;
    el.textContent = msg;
    el.className = "notice" + (kind === "error" ? " error" : kind === "ok" ? " ok" : "");
    el.classList.remove("hidden");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add("hidden"), 7000);
  }

  function fmtTime(sec) {
    sec = Math.max(0, Number(sec) || 0);
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  function fmtBytes(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let val = bytes;
    while (val >= 1024 && i < units.length - 1) {
      val /= 1024;
      i++;
    }
    return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (!res.ok) {
      let detail = `Erro HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data && data.detail) detail = data.detail;
      } catch (_) {}
      throw new Error(detail);
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  }

  function switchTab(tab) {
    $$(".page").forEach((p) => p.classList.toggle("active", p.id === tab));
    $$("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    if (tab === "library") loadLibrary();
    if (tab === "editor") loadConfig();
    if (tab === "connections" && window.reloadConnections) window.reloadConnections();
  }

  $$("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  async function pollTask(taskId, onProgressText) {
    while (true) {
      await new Promise((r) => setTimeout(r, 1500));
      const t = await api(`/api/tasks/${taskId}`);
      if (t.status === "done") return t.result;
      if (t.status === "error" || t.status === "interrupted") {
        throw new Error(t.error || "Tarefa interrompida");
      }
      if (onProgressText) onProgressText(t.message
        ? `${t.message}${Number(t.progress) > 0 && t.status === "running" ? ` ${Math.round(t.progress)}%` : ""}`
        : t.status === "queued" ? "Aguardando na fila..." : "Processando no servidor...");
    }
  }

  async function loadConfig() {
    try {
      const cfg = await api("/api/config");
      state.config = cfg;
      ["#live-transcribe", "#vod-transcribe", "#transcribe"].forEach((selector) => {
        const input = $(selector);
        if (!input) return;
        input.disabled = !cfg.transcribe_available;
        if (!cfg.transcribe_available) input.checked = false;
      });
      if ($("#btn-transcribe")) $("#btn-transcribe").disabled = !cfg.transcribe_available || state.transcript?.state?.status === "running";
      $$(".c-gen-meta").forEach((btn) => { btn.textContent = cfg.ai_titles_ready ? "Gerar título com IA" : "Sugerir título (sem IA)"; });
      const transcribeCheckbox = $("#transcribe");
      const cloudCheckbox = $("#cloud-model");
      if (transcribeCheckbox && !cfg.transcribe_available) {
        transcribeCheckbox.checked = false;
        transcribeCheckbox.disabled = true;
        transcribeCheckbox.title = "O recurso de transcrição não está disponível neste servidor.";
        if (cloudCheckbox) { cloudCheckbox.checked = false; cloudCheckbox.disabled = true; }
        $("#analysis-note").textContent = "Transcrição indisponível neste servidor. As sugestões usarão variações do áudio.";
      }
      const freeStr = fmtBytes(cfg.storage_free_bytes);
      const storageEl = $("#storage-info");
      if (storageEl) {
        storageEl.textContent = `${freeStr} livres para vídeos e exportações`;
      }
      const liveCap = $("#live-capability");
      if (liveCap) {
        liveCap.textContent = cfg.streamlink
          ? "Capture YouTube, Twitch ou Kick e acompanhe a prévia. A transcrição começa ao encerrar."
          : "A gravação de lives está indisponível neste servidor.";
      }
      const vodCap = $("#vod-capability");
      if (vodCap) {
        vodCap.textContent = cfg.ytdlp
          ? "Vídeos gravados são importados. Se a live estiver no ar, a gravação começa e você acompanha a prévia abaixo."
          : "A importação por link está indisponível neste servidor.";
      }
    } catch (err) {
      console.error(err);
    }
  }

  const fileInput = $("#video-file");
  const uploadAuth = $("#upload-authorized");
  const btnUpload = $("#btn-upload");

  function syncUploadBtn() {
    btnUpload.disabled = !(state.selectedFile && uploadAuth.checked);
  }

  fileInput?.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    state.selectedFile = file || null;
    const lbl = $("#file-label");
    if (file) {
      lbl.textContent = `Arquivo: ${file.name} (${fmtBytes(file.size)})`;
      lbl.classList.remove("hidden");
    } else {
      lbl.classList.add("hidden");
    }
    syncUploadBtn();
  });

  uploadAuth?.addEventListener("change", syncUploadBtn);

  btnUpload?.addEventListener("click", async () => {
    const file = state.selectedFile;
    if (!file || !uploadAuth.checked) return;
    btnUpload.disabled = true;
    const progWrap = $("#upload-progress");
    const progBar = $("#upload-bar");
    const progText = $("#upload-text");
    progWrap.classList.remove("hidden");
    progBar.style.width = "0%";
    progText.textContent = "0%";

    try {
      const start = await api("/api/upload/start", {
        method: "POST",
        body: JSON.stringify({ filename: file.name, size: file.size, authorized: true }),
      });
      const chunkSize = start.chunksize;
      const totalChunks = start.chunks;

      for (let i = 0; i < totalChunks; i++) {
        const blob = file.slice(i * chunkSize, Math.min(file.size, (i + 1) * chunkSize));
        const res = await fetch(`/api/upload/${start.upload_id}/chunk/${i}`, {
          method: "PUT",
          credentials: "same-origin",
          body: blob,
        });
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.detail || `Falha ao enviar bloco ${i + 1}/${totalChunks}`);
        }
        const pct = Math.round(((i + 1) / totalChunks) * 100);
        progBar.style.width = `${pct}%`;
        progText.textContent = `${pct}%`;
      }

      const done = await api(`/api/upload/${start.upload_id}/complete`, { method: "POST" });
      showNotice("Video enviado com sucesso! Abrindo no editor...", "ok");
      state.selectedFile = null;
      fileInput.value = "";
      $("#file-label").classList.add("hidden");
      progWrap.classList.add("hidden");
      await loadLibrary();
      openEditor(done.video);
    } catch (err) {
      showNotice(err.message, "error");
    } finally {
      syncUploadBtn();
    }
  });

  $("#btn-vod")?.addEventListener("click", async () => {
    const url = $("#vod-url").value.trim();
    const authorized = $("#vod-authorized").checked;
    const analyzeAfter = $("#vod-auto").checked;
    if (!url) return showNotice("Cole o link do video VOD primeiro.", "error");
    if (!authorized) return showNotice("Confirme que voce tem direito de editar este video.", "error");

    const btn = $("#btn-vod");
    btn.disabled = true;
    btn.textContent = "Verificando live ou gravação...";
    try {
      const started = await api("/api/source/auto", {
        method: "POST",
        body: JSON.stringify({ url, authorized: true, analyze_after: analyzeAfter, auto_preview: analyzeAfter,
          transcribe_after: Boolean($("#vod-transcribe")?.checked) }),
      });
      $("#vod-url").value = "";
      if (started.kind === "live") {
        showNotice("Live detectada: captura solicitada. Confira o status em Lives e encerre para gerar os cortes.", "info");
        await loadLives();
      } else {
        showNotice("VOD identificado. Baixando e preparando cortes de revisão...", "info");
        const res = await pollTask(started.task_id, (txt) => (btn.textContent = txt));
        showNotice(res.transcription === "error" ? `Vídeo salvo. Transcrição pendente: ${res.transcription_message}`
          : `Vídeo importado. ${res.previews || 0} prévia(s) pronta(s) para revisar.`, res.transcription === "error" ? "error" : "ok");
        await loadLibrary();
        const video = state.videos.find((v) => v.id === res.video_id);
        if (video) openEditor(video);
      }
    } catch (err) {
      showNotice(err.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Importar link";
    }
  });

  async function loadLives() {
    try {
      const data = await api("/api/lives");
      const box = $("#lives-list");
      if (!box) return;
      if (!data.lives?.length) {
        if (window.AutoClipsLive) window.AutoClipsLive.refresh([]);
        return;
      }
      if (window.AutoClipsLive) window.AutoClipsLive.refresh(data.lives);
      const currentLive = data.lives.find((live) => live.video_id === state.currentVideo?.id && live.analysis_task_id);
      if (currentLive) {
        await loadTranscript();
        const completedKey = `${currentLive.analysis_task_id}:${currentLive.analysis_status}`;
        if (["done", "error", "interrupted"].includes(currentLive.analysis_status) && state.completedAnalysisSeen !== completedKey) {
          state.completedAnalysisSeen = completedKey;
          await loadManifest();
        }
      }
    } catch (_) {}
  }

  window._stopLive = async (liveId) => {
    try {
      await api(`/api/live/${liveId}/stop`, { method: "POST" });
      showNotice("Encerrando gravacao da live...", "info");
      loadLives();
    } catch (err) {
      showNotice(err.message, "error");
    }
  };

  $("#btn-live")?.addEventListener("click", async () => {
    const url = $("#live-url").value.trim();
    const authorized = $("#live-authorized").checked;
    const analyzeAfter = $("#live-auto").checked;
    if (!url) return showNotice("Cole o link da live.", "error");
    if (!authorized) return showNotice("Confirme que tem autorizacao para gravar esta live.", "error");
    const btn = $("#btn-live");
    btn.disabled = true;
    btn.textContent = "Iniciando gravação...";
    try {
      await api("/api/live/start", {
        method: "POST",
        body: JSON.stringify({ url, authorized: true, analyze_after: analyzeAfter,
          transcribe_after: Boolean($("#live-transcribe")?.checked), auto_preview: analyzeAfter }),
      });
      showNotice("Captura solicitada. Confira o status em Lives.", "ok");
      $("#live-url").value = "";
      loadLives();
    } catch (err) {
      showNotice(err.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "● Gravar live";
    }
  });

  async function loadLibrary() {
    try {
      const data = await api("/api/videos");
      state.videos = data.videos || [];
      const grid = $("#video-library");
      if (!grid) return;
      if (!state.videos.length) {
        grid.innerHTML = `<div class="empty-state"><h3>Nenhum projeto ainda</h3><p>Envie um video, importe um VOD ou grave uma live para comecar.</p></div>`;
        return;
      }
      grid.innerHTML = state.videos
        .map(
          (v) => `
          <article class="video-card">
            <div class="video-thumb">
              ${v.thumbnail_url ? `<img src="${v.thumbnail_url}" alt="">` : `<span>&#9654;</span>`}
              <span class="video-badge">${escapeHtml(v.platform)}</span>
            </div>
            <div class="video-body">
              <h3>${escapeHtml(v.name)}</h3>
              <div class="video-meta">${fmtTime(v.duration)} • ${v.width}x${v.height} • ${fmtBytes(v.size)}</div>
              <div class="btn-row">
                <button class="primary" onclick="window._openVideoById('${v.id}')">Abrir no editor</button>
                <button class="danger" onclick="window._deleteVideo('${v.id}')">Excluir</button>
              </div>
            </div>
          </article>`
        )
        .join("");
    } catch (err) {
      showNotice(err.message, "error");
    }
  }

  window._openVideoById = async (id) => {
    if (!state.videos.some((x) => x.id === id)) await loadLibrary();
    const v = state.videos.find((x) => x.id === id);
    if (v) return openEditor(v);
  };

  window._deleteVideo = async (id) => {
    if (!confirm("Excluir este projeto e todos os clipes renderizados do disco?")) return;
    try {
      await api(`/api/videos/${id}`, { method: "DELETE" });
      showNotice("Projeto excluido.", "ok");
      if (state.currentVideo?.id === id) {
        editorGeneration++;
        clearTimeout(styleSaveTimer);
        layoutEditor?.destroy();
        layoutEditor = null;
        state.currentVideo = null;
        state.manifest = null;
        renderClipsList([]);
        $("#editor-body").classList.add("hidden");
        $("#editor-empty").classList.remove("hidden");
      }
      loadLibrary();
      loadConfig();
    } catch (err) {
      showNotice(err.message, "error");
    }
  };

  $("#refresh-library")?.addEventListener("click", loadLibrary);
  $("#cleanup-storage")?.addEventListener("click", async () => {
    try {
      const res = await api("/api/maintenance/cleanup", { method: "POST" });
      showNotice(`Limpeza concluida (${res.removed_stale_uploads} sessoes orfas removidas).`, "ok");
      loadConfig();
    } catch (err) {
      showNotice(err.message, "error");
    }
  });

  const player = $("#editor-player");
  const startInput = $("#clip-start");
  const endInput = $("#clip-end");
  const startSlider = $("#start-slider");
  const endSlider = $("#end-slider");

  function subtitleStyle() {
    return { font_family: $("#subtitle-font").value, font_size: Number($("#subtitle-size").value),
      color: $("#subtitle-color").value.toUpperCase() };
  }

  function validSubtitleStyle() {
    const size = subtitleStyle().font_size;
    const valid = Number.isInteger(size) && size >= 18 && size <= 96;
    $("#subtitle-size").setCustomValidity(valid ? "" : "Escolha um tamanho inteiro entre 18 e 96.");
    return valid;
  }

  function updateSubtitleSample() {
    const style = subtitleStyle();
    const sample = $("#subtitle-sample");
    if (!sample) return;
    sample.style.fontFamily = `"${style.font_family}", sans-serif`;
    sample.style.color = style.color;
    sample.style.fontSize = `${Math.max(9, Math.min(48, style.font_size / 2 || 28))}px`;
  }

  function optionsMatch(options) {
    if (layoutEditor && !layoutEditor.isReady()) return false;
    if (!options || options.layout !== $("#layout").value || options.burn !== $("#burn-subs").checked) return false;
    if (window.LayoutEditor && !window.LayoutEditor.same(options.layout_config, layoutEditor?.getConfig())) return false;
    if (!options.burn) return true;
    const style = subtitleStyle();
    return options.subtitle_style && ["font_family", "font_size", "color"].every((k) => options.subtitle_style[k] === style[k]);
  }

  function updateClipApprovals(reset = false) {
    $$(".clip-card").forEach((card) => {
      const clip = state.manifest?.clips.find((item) => item.id === card.dataset.clipId);
      const matching = Boolean(clip?.preview_url) && optionsMatch(clip?.preview_options);
      const approval = card.querySelector(".c-preview-approve");
      if (approval) { if (reset || !matching) approval.checked = false; approval.disabled = !matching; }
      const final = card.querySelector(".c-render-final");
      if (final) final.disabled = !matching || !approval?.checked;
      const note = card.querySelector(".c-preview-settings-note");
      if (note) note.classList.toggle("hidden", matching || !clip?.preview_url);
      const publish = card.querySelector(".yt-publish-box");
      if (publish) publish.classList.toggle("hidden", !optionsMatch(clip?.final_options));
    });
  }

  function persistSubtitleStyle() {
    clearTimeout(styleSaveTimer);
    styleSaveTimer = null;
    if (!state.currentVideo || !validSubtitleStyle()) return Promise.resolve();
    const id = state.currentVideo.id;
    const style = subtitleStyle();
    styleSavePromise = styleSavePromise.catch(() => {}).then(() => api(`/api/videos/${id}/subtitle-style`, {
      method: "PUT", body: JSON.stringify(style),
    }));
    return styleSavePromise;
  }

  async function loadTranscript(restoreStyle = false) {
    if (!state.currentVideo) return;
    const id = state.currentVideo.id;
    try {
      const result = await api(`/api/videos/${id}/transcript`);
      if (state.currentVideo?.id !== id) return;
      if (!state.transcript?.available && result.available) $("#burn-subs").checked = true;
      state.transcript = result;
      if (restoreStyle && result.subtitle_style) {
        $("#subtitle-font").value = result.subtitle_style.font_family;
        $("#subtitle-size").value = result.subtitle_style.font_size;
        $("#subtitle-color").value = result.subtitle_style.color;
        updateSubtitleSample();
      }
      $("#subtitle-status").textContent = result.state?.status === "running" ? "Transcrição em andamento no servidor..."
        : result.available ? `${result.words.length} palavras transcritas. Revise as legendas na prévia antes de exportar.`
        : result.state?.status === "error" ? `A gravação está salva. ${result.state.message}`
        : result.notice || "Gere legendas a partir do áudio deste vídeo.";
      $("#btn-transcribe").disabled = result.state?.status === "running" || state.config?.transcribe_available === false;
      $("#btn-transcribe").textContent = result.available ? "Refazer transcrição" : "Gerar legendas";
      for (const ext of ["srt", "vtt"]) {
        const link = $(`#subtitle-download-${ext}`);
        link.classList.toggle("hidden", !result.available);
        link.href = `/api/videos/${id}/subtitles.${ext}`;
      }
      $("#burn-subs").disabled = !result.available;
      if (!result.available) $("#burn-subs").checked = false;
    } catch (error) {
      if (state.currentVideo?.id === id) $("#subtitle-status").textContent = error.message;
    }
  }

  for (const selector of ["#subtitle-font", "#subtitle-size", "#subtitle-color"]) {
    $(selector)?.addEventListener("input", () => {
      updateSubtitleSample();
      updateClipApprovals(true);
      clearTimeout(styleSaveTimer);
      if (validSubtitleStyle()) styleSaveTimer = setTimeout(() => {
        persistSubtitleStyle().catch((error) => showNotice(`Não foi possível salvar o estilo: ${error.message}`, "error"));
      }, 350);
    });
  }
  for (const selector of ["#layout", "#burn-subs"]) $(selector)?.addEventListener("change", () => updateClipApprovals(true));
  $("#cloud-model")?.addEventListener("change", () => $("#cloud-approval-line")?.classList.toggle("hidden", !$("#cloud-model").checked));
  $("#cloud-approval-line")?.classList.toggle("hidden", !$("#cloud-model")?.checked);

  $("#btn-transcribe")?.addEventListener("click", async () => {
    if (!state.currentVideo) return;
    const id = state.currentVideo.id;
    const button = $("#btn-transcribe");
    button.disabled = true;
    button.textContent = "Iniciando transcrição...";
    try {
      const started = await api(`/api/videos/${id}/transcript`, { method: "POST", body: "{}" });
      const result = await pollTask(started.task_id, (message) => {
        if (state.currentVideo?.id === id) $("#subtitle-status").textContent = message;
      });
      if (state.currentVideo?.id === id) {
        await loadTranscript();
        $("#burn-subs").checked = Boolean(state.transcript?.available);
        await loadManifest();
      }
      showNotice(result.notice || "Transcrição concluída. Você já pode baixar ou incluir as legendas no vídeo.", "ok");
    } catch (error) { showNotice(error.message, "error"); }
    finally {
      if (state.currentVideo?.id === id) {
        button.disabled = state.config?.transcribe_available === false;
        button.textContent = state.transcript?.available ? "Refazer transcrição" : "Gerar legendas";
      }
    }
  });

  function syncTimelineUI() {
    const dur = Math.max(1, Number(state.currentVideo?.duration) || 30);
    let s = Math.max(0, Math.min(dur - 0.5, Number(startInput.value) || 0));
    let e = Math.max(s + 0.5, Math.min(dur, Number(endInput.value) || Math.min(dur, 30)));
    startInput.value = s.toFixed(1);
    endInput.value = e.toFixed(1);
    startSlider.value = s;
    endSlider.value = e;
    $("#range-start-time").textContent = fmtTime(s);
    $("#range-end-time").textContent = fmtTime(e);
    const sel = $("#timeline-selection");
    if (sel) {
      sel.style.left = `${(s / dur) * 100}%`;
      sel.style.width = `${((e - s) / dur) * 100}%`;
    }
  }

  startInput?.addEventListener("input", syncTimelineUI);
  endInput?.addEventListener("input", syncTimelineUI);
  startSlider?.addEventListener("input", () => {
    startInput.value = startSlider.value;
    if (Number(endInput.value) <= Number(startInput.value)) {
      endInput.value = Math.min(Number(startSlider.max), Number(startInput.value) + 5);
    }
    syncTimelineUI();
    if (player) player.currentTime = Number(startInput.value);
  });
  endSlider?.addEventListener("input", () => {
    endInput.value = endSlider.value;
    if (Number(startInput.value) >= Number(endInput.value)) {
      startInput.value = Math.max(0, Number(endInput.value) - 5);
    }
    syncTimelineUI();
  });

  async function openEditor(video) {
    const generation = ++editorGeneration;
    if (styleSaveTimer) await persistSubtitleStyle().catch((error) => showNotice(error.message, "error"));
    await styleSavePromise.catch(() => {});
    if (generation !== editorGeneration) return;
    if (layoutEditor) {
      try { if (layoutEditor.isReady()) await layoutEditor.flush(); }
      catch (error) { return showNotice(`Salve o layout antes de trocar de vídeo: ${error.message}`, "error"); }
      if (generation !== editorGeneration) return;
      layoutEditor.destroy();
      layoutEditor = null;
    }
    state.currentVideo = video;
    state.manifest = null;
    renderClipsList([]);
    state.transcript = null;
    state.completedAnalysisSeen = null;
    const advanced = $("#btn-advanced-timeline");
    if (advanced) advanced.disabled = false;
    switchTab("editor");
    $("#editor-empty").classList.add("hidden");
    $("#editor-body").classList.remove("hidden");
    $("#editor-subtitle").textContent = `${video.name} (${fmtTime(video.duration)})`;
    player.src = video.stream_url;
    const dur = Math.max(1, Number(video.duration) || 30);
    startSlider.max = dur;
    endSlider.max = dur;
    startInput.value = 0;
    endInput.value = Math.min(dur, 30).toFixed(1);
    syncTimelineUI();
    await loadTranscript(true);
    if (state.currentVideo?.id !== video.id || generation !== editorGeneration) return;
    $("#burn-subs").checked = Boolean(state.transcript?.available);
    if (window.LayoutEditor) {
      layoutEditor = window.LayoutEditor.create({container:"#layout-editor", videoId:video.id, layoutSelect:"#layout",
        onChange:() => updateClipApprovals(true),
        getSubtitles:() => $("#burn-subs").checked,
        getTime:() => player.currentTime || 0,
        onPresetSubtitles:(checked) => { $("#burn-subs").checked = checked && Boolean(state.transcript?.available); updateClipApprovals(true); }});
      try { await layoutEditor.load(); }
      catch (error) { showNotice(`Não foi possível carregar o layout: ${error.message}`, "error"); }
    }
    if (state.currentVideo?.id !== video.id || generation !== editorGeneration) return;
    await loadManifest();
  }

  $("#btn-advanced-timeline")?.addEventListener("click", async () => {
    if (!state.currentVideo) return showNotice("Selecione um video para abrir a timeline.", "error");
    const id = state.currentVideo.id;
    try {
      await persistSubtitleStyle();
      if (layoutEditor) await layoutEditor.flush();
      if (state.currentVideo?.id === id) window.location.assign(`/studio/${encodeURIComponent(id)}`);
    } catch (error) { showNotice(`Não foi possível salvar antes de abrir a montagem: ${error.message}`, "error"); }
  });
  $("#back-library")?.addEventListener("click", () => switchTab("library"));
  $("#empty-go")?.addEventListener("click", () => switchTab("sources"));

  $("#btn-manual")?.addEventListener("click", async () => {
    if (!state.currentVideo) return;
    const start = Number(startInput.value);
    const end = Number(endInput.value);
    const title = $("#manual-title").value.trim() || "Momento especial";
    try {
      await api(`/api/videos/${state.currentVideo.id}/manual`, {
        method: "POST",
        body: JSON.stringify({ title, start, end }),
      });
      showNotice("Clipe manual adicionado!", "ok");
      $("#manual-title").value = "";
      await loadManifest();
    } catch (err) {
      showNotice(err.message, "error");
    }
  });

  $("#btn-analyze")?.addEventListener("click", async () => {
    if (!state.currentVideo) return;
    const btn = $("#btn-analyze");
    btn.disabled = true;
    btn.textContent = "Analisando gravacao...";
    try {
      const payload = {
        clips: Number($("#clip-count").value),
        seconds: Number($("#clip-length").value),
        transcribe: $("#transcribe").checked,
        ollama_model: $("#ollama-model").value.trim() || null,
        use_cloud_model: $("#cloud-model").checked,
        cloud_approved: $("#cloud-approved").checked,
      };
      const started = await api(`/api/videos/${state.currentVideo.id}/analyze`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      await pollTask(started.task_id, (txt) => (btn.textContent = txt));
      showNotice("Analise concluida! Confira os momentos sugeridos abaixo.", "ok");
      await loadTranscript();
      await loadManifest();
    } catch (err) {
      showNotice(err.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Encontrar momentos";
    }
  });

  $("#refresh-clips")?.addEventListener("click", loadManifest);

  async function loadManifest() {
    if (!state.currentVideo) return;
    const id = state.currentVideo.id;
    try {
      const m = await api(`/api/videos/${id}/manifest`);
      if (state.currentVideo?.id !== id) return;
      state.manifest = m;
      renderClipsList(m.clips || []);
    } catch (_) {
      if (state.currentVideo?.id !== id) return;
      state.manifest = { clips: [] };
      renderClipsList([]);
    }
  }

  function renderClipsList(clips) {
    $("#clips-number").textContent = clips.length;
    const list = $("#clips-list");
    if (!clips.length) {
      list.innerHTML = `<div class="empty-state mini"><p>Crie um clipe manual ou use a analise para ver sugestoes.</p></div>`;
      return;
    }
    list.innerHTML = clips
      .map(
        (c) => `
        <article class="clip-card" data-clip-id="${c.id}">
          <div>
            <div class="clip-header">
              <span class="clip-badge">${escapeHtml(c.id)}</span>
              ${c.score != null ? `<span class="clip-badge clip-score">Score: ${c.score}</span>` : ""}
              <span class="clip-badge">${escapeHtml(c.status)}</span>
              <span class="muted small">${escapeHtml(c.reason || "")}</span>
            </div>
            <div class="clip-fields">
              <label class="field-label">TITULO DO CLIPE<input class="c-title" value="${escapeHtml(c.title || c.id)}"></label>
              <label class="field-label">INICIO (s)<input class="c-start" type="number" step="0.1" value="${c.start}"></label>
              <label class="field-label">FIM (s)<input class="c-end" type="number" step="0.1" value="${c.end}"></label>
            </div>
            ${c.excerpt ? `<div class="clip-excerpt">"${escapeHtml(c.excerpt)}"</div>` : ""}
            <div class="btn-row" style="margin-top:10px">
              <button class="outline" onclick="window._previewJump(${c.start})">&#9654; Ver no player</button>
              <button class="outline" onclick="window._saveAllClips()">Salvar ajustes</button>
              <button class="outline c-gen-meta" onclick="window._genMeta('${c.id}')">${state.config?.ai_titles_ready ? "Gerar título com IA" : "Sugerir título (sem IA)"}</button>
              <button class="primary" onclick="window._renderClip('${c.id}', 'preview')">1. Gerar Previa 720p</button>
              ${
                c.preview_url
                  ? `<label class="checkline"><input type="checkbox" class="c-preview-approve" onchange="window._approveClipPreview('${c.id}')"> Assisti a previa e aprovo esta versao para exportacao final.</label><button class="primary c-render-final" disabled onclick="window._renderClip('${c.id}', 'final')">2. Exportar final 1080p</button>`
                  : ""
              }
              <button class="danger" onclick="window._deleteClip('${c.id}')">Remover</button>
            </div>
            <p class="hint c-preview-settings-note hidden">O estilo ou enquadramento mudou. Gere uma nova prévia para revisar essas opções.</p>
            ${
              c.final_url
                ? `<div class="yt-publish-box">
                    <div class="pane-top"><b>Publicar no YouTube</b><span class="fine">ENVIO COM SUA APROVAÇÃO</span></div>
                    <label class="field-label">DESCRICAO / HASHTAGS<textarea class="c-desc" rows="2">${escapeHtml(c.description || c.excerpt || "")}</textarea></label>
                    <div class="trim-grid">
                      <label>PRIVACIDADE
                        <select class="c-privacy">
                          <option value="private" selected>Privado (Rascunho seguro)</option>
                          <option value="unlisted">Nao listado</option>
                          <option value="public">Publico</option>
                        </select>
                      </label>
                      <div style="display:flex;align-items:flex-end">
                        ${c.youtube?.youtube_video_id
                          ? `<span class="hint">Este corte ja foi enviado; edite e exporte outra versao para novo envio.</span>`
                          : `<button class="primary full" onclick="window._publishYouTube('${c.id}')">Enviar ao YouTube</button>`}
                      </div>
                    </div>
                    <label class="checkline"><input type="checkbox" class="c-yt-approve"> Confirmo que revisei este clipe e autorizo o envio oficial para o meu canal do YouTube.</label>
                    ${
                      c.youtube?.url
                        ? `<p class="hint" style="color:#4ade80">Enviado ao YouTube: <a href="${escapeHtml(c.youtube.url)}" target="_blank" rel="noopener" style="color:#38bdf8">${escapeHtml(c.youtube.url)}</a></p>`
                        : ""
                    }
                  </div>`
                : ""
            }
          </div>
          <div class="clip-media">
            ${
              c.final_url
                ? `<video controls playsinline src="${c.final_url}"></video><a class="outline" style="text-align:center;text-decoration:none;padding:8px" href="${c.final_url}" download="${c.id}_final_1080p.mp4">Baixar Final 1080p</a>`
                : c.preview_url
                ? `<video controls playsinline src="${c.preview_url}"></video><span class="hint" style="text-align:center">Previa 720p pronta</span>`
                : `<div class="empty-state mini"><p class="small">Gere a prévia 720p para revisar o corte no layout escolhido.</p></div>`
            }
          </div>
        </article>`
      )
      .join("");
    updateClipApprovals();
  }

  window._previewJump = (startSec) => {
    if (player) {
      player.currentTime = Number(startSec) || 0;
      player.play().catch(() => {});
    }
  };

  window._saveAllClips = async () => {
    if (!state.currentVideo || !state.manifest) return false;
    const cards = $$(".clip-card");
    const updated = cards.map((card) => {
      const id = card.dataset.clipId;
      return {
        id,
        title: card.querySelector(".c-title").value.trim(),
        start: Number(card.querySelector(".c-start").value),
        end: Number(card.querySelector(".c-end").value),
        selected: true,
      };
    });
    try {
      await api(`/api/videos/${state.currentVideo.id}/clips`, {
        method: "PUT",
        body: JSON.stringify({ clips: updated }),
      });
      showNotice("Ajustes dos clipes salvos!", "ok");
      await loadManifest();
      return true;
    } catch (err) {
      showNotice(err.message, "error");
      return false;
    }
  };

  window._genMeta = async (clipId) => {
    if (!state.currentVideo) return;
    try {
      const meta = await api(`/api/videos/${state.currentVideo.id}/clips/${clipId}/generate-meta`, {
        method: "POST",
      });
      showNotice(`Titulo e descricao gerados (${meta.source})!`, "ok");
      await loadManifest();
    } catch (err) {
      showNotice(err.message, "error");
    }
  };

  window._approveClipPreview = (clipId) => {
    updateClipApprovals();
  };

  window._renderClip = async (clipId, quality) => {
    if (!state.currentVideo) return;
    const videoId = state.currentVideo.id;
    if (!validSubtitleStyle()) { $("#subtitle-size").reportValidity(); return; }
    const card = document.querySelector(`.clip-card[data-clip-id="${clipId}"]`);
    const approvedPreview = Boolean(card?.querySelector(".c-preview-approve")?.checked);
    const approvedPreviewId = state.manifest?.clips.find((clip) => clip.id === clipId)?.preview_options?.render_id || null;
    if (quality === "final" && !approvedPreview) {
      return showNotice("Assista a previa e marque a aprovacao antes de exportar o final.", "error");
    }
    // Never render the OLD interval after a failed attempt to save edits.
    if (!await window._saveAllClips()) return;
    if (state.currentVideo?.id !== videoId) return;
    try {
      await persistSubtitleStyle();
      if (layoutEditor) await layoutEditor.flush();
      if (state.currentVideo?.id !== videoId) return;
      const currentClip = state.manifest?.clips.find((clip) => clip.id === clipId);
      if (quality === "final" && (!optionsMatch(currentClip?.preview_options)
          || (approvedPreviewId && currentClip?.preview_options?.render_id !== approvedPreviewId)))
        throw new Error("O layout ou a prévia mudou. Gere e aprove uma nova prévia antes de exportar.");
      showNotice(`Renderizando ${quality === "preview" ? "previa 720p" : "final 1080p"}...`, "info");
      const started = await api(`/api/videos/${videoId}/render/${quality}`, {
        method: "POST",
        body: JSON.stringify({
          clip_id: clipId,
          layout: $("#layout").value,
          ...(layoutEditor ? {layout_config:layoutEditor.getConfig()} : {}),
          subtitles: $("#burn-subs").checked,
          subtitle_style: subtitleStyle(),
          approved_selection: true,
          approved_preview: quality === "final" && approvedPreview,
          approved_preview_id: quality === "final" ? approvedPreviewId : null,
        }),
      });
      await pollTask(started.task_id);
      showNotice(`${quality === "preview" ? "Previa 720p" : "Video final 1080p"} pronto!`, "ok");
      await loadManifest();
    } catch (err) {
      showNotice(err.message, "error");
    }
  };

  window._deleteClip = async (clipId) => {
    if (!state.currentVideo) return;
    try {
      await api(`/api/videos/${state.currentVideo.id}/clips/${clipId}`, { method: "DELETE" });
      showNotice("Clipe removido.", "ok");
      await loadManifest();
    } catch (err) {
      showNotice(err.message, "error");
    }
  };

  window._publishYouTube = async (clipId) => {
    if (!state.currentVideo) return;
    const card = document.querySelector(`.clip-card[data-clip-id="${clipId}"]`);
    if (!card) return;
    const approved = card.querySelector(".c-yt-approve")?.checked;
    if (!approved) {
      return showNotice("Marque a caixa de confirmacao antes de enviar ao YouTube.", "error");
    }
    const title = card.querySelector(".c-title")?.value.trim() || "";
    const description = card.querySelector(".c-desc")?.value.trim() || "";
    const privacy = card.querySelector(".c-privacy")?.value || "private";

    try {
      showNotice("Enviando video aprovado ao YouTube...", "info");
      const started = await api(`/api/videos/${state.currentVideo.id}/clips/${clipId}/publish/youtube`, {
        method: "POST",
        body: JSON.stringify({
          title,
          description,
          privacy_status: privacy,
          approved_publish: true,
        }),
      });
      const res = await pollTask(started.task_id);
      showNotice(`Publicado no YouTube (${res.privacy_status}): ${res.url}`, "ok");
      await loadManifest();
    } catch (err) {
      showNotice(err.message, "error");
    }
  };

  loadConfig();
  updateSubtitleSample();
  loadLibrary();
  loadLives();
  setInterval(loadLives, 8000);

  if (location.hash === "#connections" || new URLSearchParams(location.search).get("connected")) {
    switchTab("connections");
    const connected = new URLSearchParams(location.search).get("connected");
    if (connected) {
      showNotice(`Conta ${connected.toUpperCase()} conectada com sucesso!`, "ok");
      history.replaceState(null, "", "/#connections");
    }
  }
})();
