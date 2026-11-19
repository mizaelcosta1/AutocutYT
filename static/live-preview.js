/* Live capture monitor. Each recording owns one persistent video/player.
 * All playback stays on the authenticated origin; no provider playback URL,
 * external iframe, credentials or API response is interpolated into HTML.
 */
(() => {
  "use strict";
  const cards = new Map();
  const activeStates = new Set(["starting", "recording", "stopping"]);
  const MAX_AUTO_RETRIES = 2;
  const platformNames = { youtube: "YouTube", twitch: "Twitch", kick: "Kick" };
  const delayNote = "Prévia da captura · o atraso varia conforme a conexão e a transmissão.";

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function setText(node, value) {
    const text = String(value ?? "");
    if (node.textContent !== text) node.textContent = text;
  }

  function bytes(value) {
    const size = Math.max(0, Number(value) || 0);
    if (size < 1024) return `${Math.round(size)} B`;
    const unit = size >= 1024 ** 3 ? "GB" : size >= 1024 ** 2 ? "MB" : "KB";
    const divisor = { KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3 }[unit];
    return `${(size / divisor).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} ${unit}`;
  }

  function publicMessage(value, fallback = "") {
    if (typeof value !== "string" || !value.trim()) return fallback;
    // Unexpected traces/provider URLs are not helpful or safe as UI copy.
    if (/traceback|https?:\/\/|\b(?:Exception|RuntimeError|FileNotFoundError)\b/i.test(value)) return fallback;
    return value.trim().slice(0, 400);
  }

  function sourceUrl(live) {
    if (typeof live.id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(live.id)) return null;
    if (typeof live.preview_url !== "string") return null;
    try {
      const url = new URL(live.preview_url, window.location.href);
      const expected = `/api/live/${encodeURIComponent(live.id)}/preview/index.m3u8`;
      if (url.origin !== window.location.origin || url.pathname !== expected ||
          url.username || url.password || url.search || url.hash) return null;
      return url.href;
    } catch (_) { return null; }
  }

  function clearRetry(card) {
    if (card.retryTimer !== null) window.clearTimeout(card.retryTimer);
    card.retryTimer = null;
  }

  function destroyPlayer(card) {
    clearRetry(card);
    card.mode = "idle";
    if (card.hls) {
      card.hls.destroy();
      card.hls = null;
    }
    if (card.source) {
      card.video.pause();
      card.video.removeAttribute("src");
      card.video.load();
    }
    card.source = null;
    card.video.hidden = true;
    card.placeholder.hidden = false;
    card.frame.setAttribute("aria-busy", "false");
  }

  function failPreview(card, message) {
    destroyPlayer(card);
    card.failed = true;
    setText(card.placeholder, "Prévia indisponível");
    setText(card.previewStatus, message);
    card.retry.hidden = false;
  }

  function attemptPlay(card) {
    try {
      const pending = card.video.play();
      if (pending && pending.catch) pending.catch(() => {
        if (card.mode !== "idle" && !card.failed) {
          setText(card.previewStatus, "Toque em reproduzir para acompanhar a prévia. A gravação continua.");
        }
      });
    } catch (_) {
      setText(card.previewStatus, "Toque em reproduzir para acompanhar a prévia. A gravação continua.");
    }
  }

  function recover(card, kind) {
    if (card.retryTimer !== null || card.failed || !activeStates.has(card.live.status)) return;
    if (card.retries >= MAX_AUTO_RETRIES) {
      failPreview(card, "A prévia perdeu a conexão. Você pode tentar novamente; a gravação continua no servidor.");
      return;
    }
    card.retries += 1;
    setText(card.previewStatus, `Reconectando a prévia (${card.retries}/${MAX_AUTO_RETRIES}). A gravação continua.`);
    card.retryTimer = window.setTimeout(() => {
      card.retryTimer = null;
      if (card.failed || !activeStates.has(card.live.status)) return;
      try {
        if (card.hls) {
          if (kind === "media") card.hls.recoverMediaError();
          else {
            card.hls.loadSource(card.source);
            card.hls.startLoad(-1);
          }
        } else if (card.mode === "native") {
          card.video.load();
          attemptPlay(card);
        }
      } catch (_) {
        failPreview(card, "Não foi possível retomar a prévia. Tente novamente; a gravação continua no servidor.");
      }
    }, 1000 * 2 ** (card.retries - 1));
  }

  function loadPolicy() {
    return { default: {
      maxTimeToFirstByteMs: 8000, maxLoadTimeMs: 15000,
      timeoutRetry: { maxNumRetry: 1, retryDelayMs: 1000, maxRetryDelayMs: 2000 },
      errorRetry: { maxNumRetry: 1, retryDelayMs: 1000, maxRetryDelayMs: 2000 },
    } };
  }

  function startPlayer(card, url) {
    card.failed = false;
    card.source = url;
    card.retry.hidden = true;
    card.video.hidden = false;
    card.placeholder.hidden = true;
    card.frame.setAttribute("aria-busy", "true");
    setText(card.previewStatus, "Abrindo a prévia da captura…");
    const Hls = window.Hls;
    try {
      if (Hls && typeof Hls.isSupported === "function" && Hls.isSupported()) {
        const player = new Hls({
          enableWorker: true, lowLatencyMode: false,
          backBufferLength: 12, maxBufferLength: 12, maxMaxBufferLength: 20,
          maxBufferSize: 16 * 1024 * 1024,
          liveSyncDurationCount: 2, liveMaxLatencyDurationCount: 5,
          manifestLoadPolicy: loadPolicy(), playlistLoadPolicy: loadPolicy(), fragLoadPolicy: loadPolicy(),
          // Preview and segment URLs are validated same-origin. Cookie-based
          // authentication is preserved, including native Safari playback.
          xhrSetup(xhr) { xhr.withCredentials = true; },
        });
        card.hls = player;
        card.mode = "hls";
        player.on(Hls.Events.MANIFEST_PARSED, () => {
          if (card.hls === player) attemptPlay(card);
        });
        player.on(Hls.Events.ERROR, (_event, data) => {
          if (card.hls !== player || !data?.fatal) return;
          const status = Number(data.response?.code || data.networkDetails?.status);
          if (status === 401 || status === 403) {
            failPreview(card, "Atualize a página e entre novamente para retomar a prévia.");
          } else if (/codec|sourcebuffer/i.test(String(data.details || ""))) {
            failPreview(card, "Este navegador não conseguiu reproduzir o formato da fonte. A gravação continua disponível para abrir depois.");
          } else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            recover(card, "network");
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            recover(card, "media");
          } else {
            failPreview(card, "Não foi possível exibir a prévia. Tente novamente; a gravação continua no servidor.");
          }
        });
        player.loadSource(url);
        player.attachMedia(card.video);
      } else if (card.video.canPlayType("application/vnd.apple.mpegurl")) {
        card.mode = "native";
        card.video.src = url;
        card.video.load();
        attemptPlay(card);
      } else {
        failPreview(card, "A prévia não está disponível neste navegador. Atualize a página ou use um navegador com reprodução HLS.");
      }
    } catch (_) {
      failPreview(card, "Não foi possível iniciar a prévia. Tente novamente; a gravação continua no servidor.");
    }
  }

  function updateActions(card) {
    const state = card.live.status;
    const stopping = state === "stopping";
    card.stop.hidden = !["starting", "recording", "stopping"].includes(state);
    card.stop.disabled = stopping || card.stopPending;
    setText(card.stop, stopping || card.stopPending ? "Encerrando gravação…" : "Encerrar gravação");
    card.open.hidden = !card.live.video_id;
    card.open.disabled = card.openPending;
    setText(card.open, card.openPending ? "Abrindo gravação…" : "Abrir gravação");
  }

  function createCard(id) {
    const root = element("article", "live-card");
    const head = element("div", "live-card-head");
    const title = element("h3");
    const badge = element("span", "live-badge");
    const status = element("p", "live-status");
    const meta = element("p", "live-meta");
    meta.setAttribute("aria-live", "off");
    const frame = element("div", "live-preview-frame");
    const video = element("video");
    video.controls = true;
    video.muted = true;
    video.defaultMuted = true;
    video.autoplay = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.setAttribute("playsinline", "");
    video.setAttribute("muted", "");
    video.hidden = true;
    const previewStatus = element("p", "live-preview-status");
    previewStatus.setAttribute("role", "status");
    const placeholder = element("p", "live-preview-placeholder", "A prévia aparecerá aqui");
    const delay = element("p", "live-meta", delayNote);
    const actions = element("div", "live-actions");
    const stop = element("button", "danger", "Encerrar gravação");
    const open = element("button", "outline", "Abrir gravação");
    const retry = element("button", "outline", "Tentar prévia novamente");
    for (const button of [stop, open, retry]) button.type = "button";
    retry.hidden = true;
    const task = element("p", "live-status");
    const progress = element("progress", "live-analysis-progress");
    progress.max = 100;
    progress.hidden = true;
    progress.setAttribute("aria-label", "Progresso do processamento da gravação");
    const message = element("p", "live-status");
    head.append(title, badge);
    frame.append(video, placeholder);
    actions.append(stop, open, retry);
    root.append(head, status, meta, frame, previewStatus, delay, task, progress, message, actions);
    const card = { id, root, title, badge, status, meta, frame, video, placeholder, previewStatus,
      delay, task, progress, message, stop, open, retry, live: {}, mode: "idle",
      hls: null, source: null, retries: 0, retryTimer: null, failed: false,
      stopPending: false, openPending: false };
    video.addEventListener("playing", () => {
      if (card.mode !== "idle" && !card.failed) {
        frame.setAttribute("aria-busy", "false");
        setText(previewStatus, "Acompanhando a captura. Use o controle de som do player para ouvir.");
      }
    });
    video.addEventListener("waiting", () => {
      if (card.mode !== "idle" && !card.failed && card.retryTimer === null) {
        setText(previewStatus, "Carregando os próximos segundos da captura…");
      }
    });
    video.addEventListener("error", () => {
      if (card.mode !== "native" || card.failed) return;
      if (video.error?.code === 2) recover(card, "network");
      else failPreview(card, "Este navegador não conseguiu reproduzir o formato da fonte. A gravação continua no servidor.");
    });
    retry.addEventListener("click", () => {
      if (!activeStates.has(card.live.status)) return;
      destroyPlayer(card);
      card.failed = false;
      card.retries = 0;
      card.retry.hidden = true;
      updatePreview(card);
    });
    stop.addEventListener("click", async () => {
      if (card.stopPending || stop.disabled || typeof window._stopLive !== "function") return;
      card.stopPending = true;
      updateActions(card);
      try { await window._stopLive(card.id); }
      catch (_) { setText(message, "Não foi possível encerrar agora. Tente novamente."); message.hidden = false; }
      finally { card.stopPending = false; updateActions(card); }
    });
    open.addEventListener("click", async () => {
      if (card.openPending || !card.live.video_id || typeof window._openVideoById !== "function") return;
      card.openPending = true;
      updateActions(card);
      try { await window._openVideoById(card.live.video_id); }
      catch (_) { setText(message, "Não foi possível abrir a gravação agora. Tente novamente."); message.hidden = false; }
      finally { card.openPending = false; updateActions(card); }
    });
    return card;
  }

  function updatePreview(card) {
    const live = card.live;
    if (!activeStates.has(live.status)) {
      if (card.mode !== "idle" || card.source) destroyPlayer(card);
      card.retry.hidden = true;
      card.failed = false;
      card.delay.hidden = true;
      setText(card.placeholder, "Gravação encerrada");
      setText(card.previewStatus, live.video_id
        ? "Captura encerrada. Abra a gravação para assistir e editar."
        : live.status === "error" ? "A captura não está ativa. Veja a mensagem abaixo."
          : "Captura encerrada. Preparando o arquivo da gravação…");
      return;
    }
    card.delay.hidden = false;
    if (live.preview_has_video === false || live.has_video === false) {
      if (card.mode !== "idle" || card.source) destroyPlayer(card);
      card.retry.hidden = true;
      setText(card.placeholder, "Fonte somente áudio");
      setText(card.previewStatus, publicMessage(live.preview_message,
        "A fonte contém apenas áudio. A gravação continua sem preview de vídeo."));
      return;
    }
    // Polling never detaches the media or resets playback, mute, volume or the
    // native controls. A transient playlist race also preserves the player.
    if (card.failed || card.mode !== "idle") return;
    if (!live.preview_ready) {
      setText(card.placeholder, "A prévia aparecerá aqui");
      setText(card.previewStatus, publicMessage(live.preview_message,
        "Aguardando os primeiros segundos da captura para abrir a prévia…"));
      card.frame.setAttribute("aria-busy", "true");
      return;
    }
    const url = sourceUrl(live);
    if (!url) {
      failPreview(card, "O endereço da prévia está indisponível. Atualize a página ou tente novamente.");
      return;
    }
    startPlayer(card, url);
  }

  function updateCard(card, live) {
    card.live = live;
    const platform = platformNames[live.platform] || "Live";
    setText(card.title, `Gravação · ${platform}`);
    card.video.setAttribute("aria-label", `Prévia da captura de ${platform}`);
    const labels = { starting: "Conectando", recording: "Gravando", stopping: "Encerrando",
      completed: live.video_id ? "Gravação salva" : "Finalizando gravação", processing: "Processando", saving: "Salvando gravação",
      interrupted: "Captura interrompida", error: "Falha na captura" };
    const label = labels[live.status] || "Verificando gravação";
    setText(card.badge, label);
    card.badge.classList.toggle("is-recording", live.status === "recording");
    card.badge.classList.toggle("is-error", live.status === "error");
    card.status.hidden = true;
    setText(card.meta, `${bytes(live.bytes)} gravados`);
    const taskLabels = { queued: "O processamento da gravação está na fila.",
      running: "Preparando transcrição, legendas e cortes.",
      done: "Processamento concluído. Abra a gravação para revisar.",
      error: "Não foi possível concluir o processamento. Abra a gravação para tentar novamente." };
    const processing = ["queued", "running"].includes(live.analysis_status);
    const analysisMessage = publicMessage(live.analysis_message, taskLabels[live.analysis_status] || "");
    setText(card.task, analysisMessage);
    card.task.hidden = !analysisMessage;
    card.progress.hidden = !processing;
    if (processing && live.analysis_status === "running" && Number.isFinite(Number(live.analysis_progress))) {
      card.progress.value = Math.min(100, Math.max(0, Number(live.analysis_progress)));
    } else card.progress.removeAttribute("value");
    const message = publicMessage(live.message, live.status === "error"
      ? "Não foi possível continuar a captura. Verifique a transmissão e inicie novamente." : "");
    setText(card.message, message);
    card.message.hidden = !message;
    updateActions(card);
    updatePreview(card);
  }

  function refresh(lives) {
    const container = document.getElementById("lives-list");
    if (!container || !Array.isArray(lives)) return;
    const valid = lives.filter((live) => live && typeof live.id === "string" && live.id);
    const incoming = new Set(valid.map((live) => live.id));
    for (const [id, card] of cards) {
      if (!incoming.has(id) || card.root.parentNode !== container) {
        destroyPlayer(card);
        card.root.remove();
        cards.delete(id);
      }
    }
    const empty = document.getElementById("lives-empty");
    if (empty) empty.hidden = valid.length > 0;
    const processed = new Set();
    let index = 0;
    for (const live of valid) {
      if (processed.has(live.id)) continue;
      processed.add(live.id);
      let card = cards.get(live.id);
      if (!card) { card = createCard(live.id); cards.set(live.id, card); }
      if (container.children[index] !== card.root) container.insertBefore(card.root, container.children[index] || null);
      updateCard(card, live);
      index += 1;
    }
  }

  function destroyAll() {
    for (const card of cards.values()) { destroyPlayer(card); card.root.remove(); }
    cards.clear();
    const empty = document.getElementById("lives-empty");
    if (empty) empty.hidden = false;
  }

  window.AutoClipsLive = { refresh, destroyAll };
  window.addEventListener("pagehide", destroyAll);
})();
