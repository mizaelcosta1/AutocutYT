/* Shared, opt-in download and external storage controls for both editors. */
(() => {
  "use strict";
  const state = {
    settings: null, settingsError: "", videoId: null, jobs: [], historyError: "",
    historyRequest: 0, poll: null, saving: false, connecting: false, settingsDirty: false, downloads: new Map()
  };
  const senders = new WeakMap(), pendingSends = new Set(), pendingRetries = new Set(), pendingCancels = new Set(), attempted = new Set();
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const qualityLabel = quality => quality === "preview" ? "prévia 720p" : "final 1080p";
  const providerLabel = provider => provider === "github" ? "GitHub" : "Google Drive";
  const connected = (provider = state.settings?.provider) => Boolean(state.settings?.[provider]?.connected);
  const field = name => $('[data-storage-field="' + name + '"]');
  let settingsRequest = null;

  async function request(url, options = {}) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {
        credentials: "same-origin", ...options, signal: controller.signal,
        headers: {"Content-Type": "application/json", ...(options.headers || {})}
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Não foi possível concluir o pedido (" + response.status + ").");
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("O servidor demorou para responder. Atualize o estado antes de tentar novamente.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function feedback(message, error = false) {
    for (const node of $$("[data-delivery-feedback]")) {
      node.textContent = message;
      node.hidden = !message;
      node.classList.toggle("delivery-error", error);
    }
  }
  function externalUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && ["drive.google.com", "github.com"].includes(url.hostname) && !url.username && !url.password ? url.href : "";
    } catch (_) { return ""; }
  }
  function artifact(value) {
    if (!value || !/^[a-f0-9]{32}$/i.test(value.video_id || "") ||
        !["clip","timeline"].includes(value.kind) || !["preview","final"].includes(value.quality) ||
        !/^[a-z0-9_-]{1,100}$/i.test(value.render_id || "")) return null;
    if (value.kind === "clip" && !/^[a-z0-9_-]{1,100}$/i.test(value.clip_id || "")) return null;
    const base = "/api/videos/" + value.video_id + (value.kind === "timeline" ? "/timeline/rendered/" :
      "/rendered/" + encodeURIComponent(value.clip_id) + "/") + value.quality;
    const parameter = value.kind === "timeline" ? "render_id" : "v";
    const url = new URL(base, location.origin);
    url.searchParams.set(parameter, value.render_id);
    if (value.url) {
      try {
        const supplied = new URL(value.url, location.origin);
        if (supplied.origin !== location.origin || supplied.pathname !== base ||
            supplied.searchParams.get(parameter) !== value.render_id) return null;
      } catch (_) { return null; }
    }
    url.searchParams.set("download", "1");
    return {
      video_id: value.video_id, kind: value.kind, clip_id: value.kind === "clip" ? value.clip_id : undefined,
      quality: value.quality, render_id: value.render_id,
      filename: String(value.filename || (value.kind === "clip" ? value.clip_id : "autoclips-montagem") +
        "_" + value.quality + ".mp4").replace(/[/\\\x00-\x1f]/g, "_"),
      downloadUrl: url.pathname + url.search
    };
  }
  const artifactKey = value => [value.video_id, value.kind, value.clip_id || "", value.quality, value.render_id].join(":");

  function buildConnections() {
    const root = $("[data-storage-connections]");
    if (!root) return;
    root.innerHTML = [
      '<div class="delivery-heading"><div><span class="delivery-eyebrow">GUARDE SEUS VÍDEOS</span><h2>Downloads e armazenamento</h2>',
      '<p>Baixe os cortes ou envie uma cópia para sua conta. A automação vale para os próximos arquivos gerados.</p></div>',
      '<button type="button" class="delivery-button" data-storage-refresh>Atualizar conexões</button></div>',
      '<p class="delivery-feedback" data-delivery-feedback role="status" aria-live="polite" hidden></p>',
      '<p class="delivery-feedback delivery-error" data-storage-error role="status" hidden></p>',
      '<div class="delivery-providers">',
      '<article class="delivery-provider"><div class="delivery-heading"><h3>Google Drive</h3><span class="delivery-badge" data-drive-status>Consultando…</span></div>',
      '<p>Salva os vídeos na pasta <strong>AutoClips</strong> do seu Drive.</p>',
      '<p class="delivery-hint" data-drive-detail>A autorização de armazenamento é separada do login no estúdio.</p>',
      '<div class="delivery-buttons"><a class="delivery-button delivery-primary" data-drive-connect href="/auth/storage/drive/start" hidden>Conectar Google Drive</a>',
      '<a class="delivery-link" data-drive-folder target="_blank" rel="noopener noreferrer" hidden>Abrir pasta ↗</a>',
      '<button type="button" class="delivery-button delivery-danger" data-storage-disconnect="drive" hidden>Desconectar Drive</button></div></article>',
      '<article class="delivery-provider"><div class="delivery-heading"><h3>GitHub</h3><span class="delivery-badge" data-github-status>Consultando…</span></div>',
      '<p>Guarda os vídeos como anexos de uma Release em rascunho, em um repositório privado.</p>',
      '<p class="delivery-hint" data-github-detail>Use um token com acesso ao repositório e permissão Contents: leitura e escrita.</p>',
      '<form data-github-form><label class="delivery-field">REPOSITÓRIO PRIVADO<input name="repository" autocomplete="off" spellcheck="false" placeholder="seu-usuario/seu-repositorio" required pattern="[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" maxlength="200"></label>',
      '<label class="delivery-field">TOKEN DO GITHUB<input name="token" type="password" autocomplete="off" spellcheck="false" placeholder="Cole seu token de acesso" required maxlength="500"></label>',
      '<button class="delivery-button delivery-primary" type="submit" data-github-submit disabled>Validar e conectar GitHub</button></form>',
      '<div class="delivery-buttons"><a class="delivery-link" data-github-repository target="_blank" rel="noopener noreferrer" hidden>Abrir repositório ↗</a>',
      '<button type="button" class="delivery-button delivery-danger" data-storage-disconnect="github" hidden>Desconectar GitHub</button></div>',
      '<p class="delivery-hint">O token não é exibido novamente. Apenas repositórios privados são aceitos.</p></article></div>',
      '<form data-storage-form class="delivery-automation"><div><h3>Ao terminar de gerar um vídeo</h3><p class="delivery-hint">Escolha as ações e salve. Os arquivos que já existem continuam disponíveis nos botões de download e envio.</p></div>',
      '<label class="delivery-check"><input type="checkbox" data-storage-field="auto_download" disabled><span><strong>Baixar automaticamente pelo navegador</strong><small>Mantenha esta aba aberta. O navegador pode pedir permissão para vários downloads; o botão manual continua disponível.</small></span></label>',
      '<div class="delivery-settings-grid"><label class="delivery-field">QUAL ARQUIVO?<select data-storage-field="qualities" disabled><option value="final">Final 1080p</option><option value="preview">Prévia 720p</option><option value="both">Prévia e final</option></select></label>',
      '<label class="delivery-field">DESTINO DOS ENVIOS<select data-storage-field="provider" disabled><option value="drive">Google Drive</option><option value="github">GitHub</option></select></label></div>',
      '<label class="delivery-check"><input type="checkbox" data-storage-field="auto_upload" disabled><span><strong>Enviar automaticamente ao destino conectado</strong><small>O servidor envia após a renderização, mesmo que você feche a página.</small></span></label>',
      '<p class="delivery-hint" data-auto-upload-help>Conecte um destino para habilitar o envio automático.</p>',
      '<p class="delivery-hint">A qualidade escolhida vale para download e envio automático. O final continua exigindo sua revisão e aprovação da prévia.</p>',
      '<button type="submit" class="delivery-button delivery-primary" data-storage-save disabled>Salvar automação</button>',
      '<span class="delivery-hint" data-storage-saved>Consultando preferências…</span></form>'
    ].join("");
    $("[data-github-form]", root).addEventListener("submit", connectGitHub);
    $("[data-storage-form]", root).addEventListener("submit", saveSettings);
    field("provider").addEventListener("change", updateUploadAvailability);
    $("[data-storage-form]", root).addEventListener("change", () => {
      state.settingsDirty = true;
      $("[data-storage-saved]").textContent = "Alterações ainda não salvas.";
    });
  }
  function buildProject() {
    for (const root of $$("[data-delivery-project]")) {
      root.innerHTML = [
        '<div class="delivery-heading"><div><span class="delivery-eyebrow">ARQUIVOS DO PROJETO</span><h3>Downloads e envios</h3></div>',
        '<a class="delivery-link" href="/#connections" target="_blank" rel="noopener noreferrer">Conexões e automação ↗</a></div>',
        '<p class="delivery-hint" data-delivery-summary>Consultando preferências de download e envio…</p>',
        '<p class="delivery-feedback" data-delivery-feedback role="status" aria-live="polite" hidden></p>',
        '<div data-download-fallbacks aria-live="polite"></div>',
        '<div class="delivery-heading delivery-history-heading"><h4>Envios deste projeto</h4><button type="button" class="delivery-button" data-delivery-refresh>Atualizar envios</button></div>',
        '<div data-delivery-history aria-live="polite"><p class="delivery-hint">Abra um vídeo para consultar seus envios.</p></div>'
      ].join("");
    }
  }
  function updateUploadAvailability() {
    const input = field("auto_upload"), provider = field("provider")?.value;
    if (!input) return;
    input.disabled = state.saving || !state.settings || !connected(provider);
    if (!connected(provider)) input.checked = false;
    $("[data-auto-upload-help]").textContent = connected(provider)
      ? "Destino conectado: " + providerLabel(provider) + (provider === "github" ? " · " + (state.settings.github.repository || "") : " · pasta AutoClips") + "."
      : "Conecte " + providerLabel(provider) + " para habilitar o envio automático.";
  }
  function renderSettings(replaceFields = true) {
    const settings = state.settings, root = $("[data-storage-connections]");
    if (root) {
      const error = $("[data-storage-error]");
      error.hidden = !state.settingsError;
      error.textContent = state.settingsError;
      if (settings) {
        const drive = settings.drive || {}, github = settings.github || {};
        const badge = (selector, text, ready) => { $(selector).textContent = text; $(selector).classList.toggle("is-connected", ready); };
        badge("[data-drive-status]", drive.connected ? "Conectado" : drive.configured ? "Não conectado" : "Indisponível no servidor", drive.connected);
        badge("[data-github-status]", github.connected ? "Conectado · privado" : "Não conectado", github.connected);
        $("[data-drive-connect]").hidden = !drive.configured || drive.connected;
        $('[data-storage-disconnect="drive"]').hidden = !drive.connected;
        $('[data-storage-disconnect="github"]').hidden = !github.connected;
        $("[data-drive-detail]").textContent = drive.connected
          ? "Autorizado para salvar os arquivos do AutoClips na sua conta."
          : drive.configured ? "Conecte sua conta para permitir o envio dos arquivos do AutoClips."
          : "A integração do Google precisa ser configurada no servidor para conectar esta conta.";
        const folderUrl = externalUrl(drive.folder_url);
        $("[data-drive-folder]").hidden = !drive.connected || !folderUrl;
        if (folderUrl) $("[data-drive-folder]").href = folderUrl;
        const repo = /^[a-z0-9_.-]+\/[a-z0-9_.-]+$/i.test(github.repository || "") ? github.repository : "";
        $("[data-github-repository]").hidden = !github.connected || !repo;
        if (repo) $("[data-github-repository]").href = "https://github.com/" + repo;
        $("[data-github-form]").hidden = github.connected;
        $("[data-github-detail]").textContent = github.connected
          ? "Destino validado: " + (repo || "repositório privado") + ". Os vídeos ficam em uma Release em rascunho."
          : "Use um token com acesso ao repositório e permissão Contents: leitura e escrita.";
        if (replaceFields) {
          field("auto_download").checked = Boolean(settings.auto_download);
          field("auto_upload").checked = Boolean(settings.auto_upload);
          field("provider").value = settings.provider || "drive";
          field("qualities").value = settings.qualities?.length === 2 ? "both" : settings.qualities?.[0] || "final";
          $("[data-storage-saved]").textContent = "Preferências salvas para sua conta.";
        }
      }
      for (const input of $$("[data-storage-field]:not([data-storage-field=auto_upload]),[data-storage-save]"))
        input.disabled = !settings || state.saving;
      $("[data-github-submit]").disabled = !settings || state.connecting;
      updateUploadAvailability();
    }
    for (const summary of $$("[data-delivery-summary]")) {
      if (!settings) { summary.textContent = state.settingsError || "Consultando preferências de download e envio…"; continue; }
      const qualities = settings.qualities?.length === 2 ? "prévia e final" : qualityLabel(settings.qualities?.[0]);
      summary.textContent = "Download automático: " + (settings.auto_download ? "ativado enquanto esta aba estiver aberta" : "desativado") +
        ". Envio automático: " + (settings.auto_upload && connected() ? providerLabel(settings.provider) : "desativado") +
        ". Arquivos: " + qualities + ".";
    }
    updateSenders();
  }
  async function refreshSettings() {
    if (settingsRequest) return settingsRequest;
    settingsRequest = (async () => {
      let fresh = null;
      try { fresh = await request("/api/storage/settings", {cache:"no-store"}); state.settings = fresh; state.settingsError = ""; }
      catch (error) { state.settingsError = "Não foi possível consultar as conexões. " + error.message; }
      finally { settingsRequest = null; renderSettings(!state.settingsDirty); }
      // Callers deciding whether to download must never use a cached opt-in
      // after a failed refresh: another tab may have changed that preference.
      return fresh;
    })();
    return settingsRequest;
  }
  async function saveSettings(event) {
    event.preventDefault();
    if (!state.settings || state.saving) return;
    const qualities = field("qualities").value, provider = field("provider").value;
    const body = {auto_download: field("auto_download").checked,
      auto_upload: field("auto_upload").checked, provider,
      qualities: qualities === "both" ? ["preview","final"] : [qualities]};
    if (body.auto_upload && !connected(provider)) return feedback("Conecte o destino antes de ativar o envio automático.", true);
    state.saving = true; renderSettings(false); feedback("Salvando preferências…");
    try {
      state.settings = await request("/api/storage/settings", {method:"PATCH", body:JSON.stringify(body)});
      state.settingsDirty = false;
      state.settingsError = "";
      feedback("Automação salva. As opções serão usadas nos próximos arquivos gerados.");
      renderSettings();
    } catch (error) { feedback(error.message, true); }
    finally { state.saving = false; renderSettings(false); }
  }
  async function connectGitHub(event) {
    event.preventDefault();
    if (state.connecting) return;
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    const token = form.elements.token.value.trim(), repository = form.elements.repository.value.trim();
    form.elements.token.value = "";
    state.connecting = true;
    $("[data-github-submit]").textContent = "Validando repositório…";
    renderSettings(false); feedback("Validando o token e o repositório privado…");
    try {
      await request("/api/storage/github", {method:"POST", body:JSON.stringify({token, repository})});
      await refreshSettings();
      feedback("GitHub conectado. Configure e salve a automação quando quiser ativá-la.");
    } catch (_) {
      feedback("Não foi possível validar o GitHub. Confira o token, o repositório privado e a permissão Contents de leitura e escrita.", true);
    } finally {
      state.connecting = false;
      $("[data-github-submit]").textContent = "Validar e conectar GitHub";
      renderSettings(false);
    }
  }
  async function disconnect(provider, button) {
    if (!["drive","github"].includes(provider) || button.disabled) return;
    button.disabled = true;
    try {
      await request("/api/storage/" + provider + "/disconnect", {method:"POST"});
      await refreshSettings();
      feedback(providerLabel(provider) + " desconectado. Os arquivos já enviados permanecem na sua conta.");
    } catch (error) { feedback(error.message, true); }
    finally { button.disabled = false; }
  }
  function mountSend(container, input) {
    if (!container) return;
    const value = artifact(input);
    container.replaceChildren();
    if (!value) return;
    const button = document.createElement("button");
    button.type = "button"; button.className = "delivery-button"; button.dataset.deliverySend = "";
    senders.set(button, value);
    container.append(button);
    const link = document.createElement("a");
    link.className = "delivery-link delivery-connect-link"; link.href = "/#connections";
    link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "Conectar destino ↗";
    link.dataset.deliveryConnect = "";
    container.append(link);
    updateSenders();
  }
  function updateSenders() {
    for (const button of $$("[data-delivery-send]")) {
      const value = senders.get(button);
      if (!value) continue;
      const busy = pendingSends.has(artifactKey(value) + ":" + state.settings?.provider);
      button.disabled = !state.settings || !connected() || busy;
      button.textContent = busy ? "Solicitando envio…" : connected() ? "Enviar ao " + providerLabel(state.settings.provider) : "Destino não conectado";
      button.setAttribute("aria-label", connected()
        ? "Enviar " + qualityLabel(value.quality) + " ao " + providerLabel(state.settings.provider)
        : "Conecte um destino de armazenamento para enviar " + qualityLabel(value.quality));
      const link = $("[data-delivery-connect]", button.parentElement);
      if (link) link.hidden = connected();
    }
  }
  async function send(input) {
    const value = artifact(input), provider = state.settings?.provider;
    if (!value || !connected(provider)) { feedback("Conecte um destino em Conexões e automação antes de enviar.", true); return null; }
    const key = artifactKey(value) + ":" + provider;
    if (pendingSends.has(key)) return null;
    pendingSends.add(key); updateSenders();
    try {
      const result = await request("/api/videos/" + value.video_id + "/deliveries", {method:"POST", body:JSON.stringify({
        kind:value.kind, clip_id:value.clip_id, quality:value.quality, render_id:value.render_id, provider
      })});
      feedback("Envio solicitado ao " + providerLabel(provider) + ". Acompanhe o estado em Envios deste projeto.");
      if (state.videoId === value.video_id) await refreshJobs();
      return result.job;
    } catch (error) { feedback(error.message, true); return null; }
    finally { pendingSends.delete(key); updateSenders(); }
  }
  const jobLabels = {queued:"Na fila", running:"Enviando", retry_wait:"Aguardando nova tentativa", succeeded:"Enviado", failed:"Falhou", cancelled:"Cancelado"};
  const bytes = count => (Number(count) / 1048576).toFixed(1) + " MB";
  function renderJobs() {
    for (const root of $$("[data-delivery-history]")) {
      const error = state.historyError ? '<p class="delivery-error">' + esc(state.historyError) + "</p>" : "";
      if (!state.videoId) { root.innerHTML = '<p class="delivery-hint">Abra um vídeo para consultar seus envios.</p>'; continue; }
      root.innerHTML = error + (state.jobs.length ? '<ol class="delivery-job-list">' + state.jobs.map(job => {
        const url = externalUrl(job.url), progress = Number(job.progress);
        const title = (job.kind === "timeline" ? "Montagem" : job.clip_id || "Corte") + " · " + qualityLabel(job.quality);
        const size = Number(job.total_bytes) > 0 ? bytes(job.bytes_sent || 0) + " de " + bytes(job.total_bytes) : "";
        const time = new Date(job.updated_at || job.created_at);
        return '<li class="delivery-job"><div class="delivery-heading"><strong>' + esc(title) + '</strong><span class="delivery-badge ' +
          (job.status === "succeeded" ? "is-connected" : job.status === "failed" ? "is-error" : "") + '">' +
          esc(jobLabels[job.status] || "Consultando estado") + '</span></div><p class="delivery-hint">' + esc(providerLabel(job.provider)) +
          (Number.isNaN(time.getTime()) ? "" : " · " + esc(time.toLocaleString("pt-BR"))) + "</p>" +
          (job.status === "running" && job.progress != null && Number.isFinite(progress) ? '<progress max="100" value="' +
            Math.max(0, Math.min(100, progress)) + '" aria-label="Progresso do envio"></progress>' : "") +
          (size && job.status === "running" ? '<p class="delivery-hint">' + esc(size) + "</p>" : "") +
          (job.error ? '<p class="delivery-error">' + esc(job.error) + "</p>" : "") +
          (url && job.status === "succeeded" ? '<a class="delivery-link" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">Abrir no ' + esc(providerLabel(job.provider)) + " ↗</a>" : "") +
          (job.status === "failed" ? '<button type="button" class="delivery-button" data-delivery-retry="' + esc(job.id) + '"' +
            (pendingRetries.has(job.id) || pendingCancels.has(job.id) ? " disabled" : "") + '>Tentar envio novamente</button>' : "") +
          (["queued","retry_wait","failed"].includes(job.status) ? '<button type="button" class="delivery-button delivery-danger" data-delivery-cancel="' +
            esc(job.id) + '"' + (pendingRetries.has(job.id) || pendingCancels.has(job.id) ? " disabled" : "") + '>Cancelar envio</button>' : "") + "</li>";
      }).join("") + "</ol>" : '<p class="delivery-hint">' + (state.historyError ? "Use Atualizar envios para tentar novamente." : "Nenhum envio registrado. Use Enviar ao destino em um arquivo gerado ou ative a automação em Conexões.") + "</p>");
    }
  }
  async function refreshJobs() {
    clearTimeout(state.poll);
    if (!state.videoId) return;
    const videoId = state.videoId, sequence = ++state.historyRequest;
    try {
      const result = await request("/api/videos/" + videoId + "/deliveries");
      if (videoId !== state.videoId || sequence !== state.historyRequest) return;
      state.jobs = Array.isArray(result.jobs) ? result.jobs : []; state.historyError = "";
    } catch (error) {
      if (videoId !== state.videoId || sequence !== state.historyRequest) return;
      state.historyError = "Não foi possível atualizar os envios. " + error.message;
    }
    renderJobs();
    if (!state.historyError && state.jobs.some(job => ["queued","running","retry_wait"].includes(job.status)))
      state.poll = setTimeout(refreshJobs, document.hidden ? 8000 : 2500);
  }
  async function retry(id) {
    if (pendingRetries.has(id) || pendingCancels.has(id)) return;
    pendingRetries.add(id); renderJobs();
    try {
      await request("/api/storage/jobs/" + encodeURIComponent(id) + "/retry", {method:"POST"});
      feedback("Nova tentativa solicitada.");
      await refreshJobs();
    } catch (error) { feedback(error.message, true); }
    finally { pendingRetries.delete(id); renderJobs(); }
  }
  async function cancel(id) {
    if (pendingCancels.has(id) || pendingRetries.has(id)) return;
    pendingCancels.add(id); renderJobs();
    try {
      await request("/api/storage/jobs/" + encodeURIComponent(id) + "/cancel", {method:"POST"});
      feedback("Envio cancelado. O vídeo original e os arquivos já enviados foram preservados.");
      await refreshJobs();
    } catch (error) { feedback(error.message, true); }
    finally { pendingCancels.delete(id); renderJobs(); }
  }
  function renderDownloads() {
    const values = Array.from(state.downloads.values()).filter(item => item.video_id === state.videoId);
    for (const root of $$("[data-download-fallbacks]")) root.innerHTML = values.map(item =>
      '<div class="delivery-download-notice"><p>' + (item.blocked
        ? "O navegador não iniciou o download automático. Use o botão abaixo."
        : "Download automático solicitado. Se não iniciar, use o botão abaixo.") + '</p><a class="delivery-button delivery-primary" href="' +
      esc(item.downloadUrl) + '" download="' + esc(item.filename) + '">Baixar ' + esc(qualityLabel(item.quality)) +
      (item.clip_id ? " · " + esc(item.clip_id) : " · montagem") + "</a></div>").join("");
  }
  async function onRendered(input) {
    if (Array.isArray(input?.artifacts)) {
      for (const item of input.artifacts) await onRendered(item);
      return;
    }
    const value = artifact(input);
    if (!value) return;
    const settings = await refreshSettings();
    if (input.delivery_error) feedback("O vídeo foi gerado, mas o envio automático precisa de atenção. " + String(input.delivery_error), true);
    if (state.videoId === value.video_id) void refreshJobs();
    if (!settings) {
      feedback("Não foi possível confirmar suas preferências de automação. O download automático não foi iniciado; use o botão Baixar do arquivo.", true);
      return;
    }
    if (!settings.auto_download || !settings.qualities?.includes(value.quality)) return;
    const key = "autoclips.download." + artifactKey(value);
    let done = attempted.has(key);
    try { done = done || sessionStorage.getItem(key) === "requested"; } catch (_) {}
    if (done) return;
    attempted.add(key);
    try { sessionStorage.setItem(key, "requested"); } catch (_) {}
    const link = document.createElement("a");
    link.href = value.downloadUrl; link.download = value.filename; link.hidden = true;
    document.body.append(link);
    let blocked = false;
    try { link.click(); } catch (_) { blocked = true; }
    finally { link.remove(); }
    state.downloads.set(key, {...value, blocked});
    if (state.downloads.size > 20) state.downloads.delete(state.downloads.keys().next().value);
    renderDownloads();
  }
  function setVideo(videoId) {
    if (videoId === state.videoId) return;
    clearTimeout(state.poll); ++state.historyRequest;
    state.videoId = /^[a-f0-9]{32}$/i.test(videoId || "") ? videoId : null;
    state.jobs = []; state.historyError = ""; renderJobs(); renderDownloads();
    void refreshJobs();
  }
  async function refresh() { await Promise.all([refreshSettings(), refreshJobs()]); }
  const controller = {
    ready:null, refresh, refreshJobs, setVideo, onRendered, mountSend, send,
    downloadUrl: input => artifact(input)?.downloadUrl || ""
  };
  window.StorageDeliveryUI = controller;
  buildConnections(); buildProject();
  controller.ready = refreshSettings();
  document.addEventListener("click", event => {
    const target = event.target.closest?.("button");
    if (!target || target.disabled) return;
    if (target.hasAttribute("data-storage-refresh")) void refresh();
    if (target.hasAttribute("data-delivery-refresh")) void refreshJobs();
    if (target.hasAttribute("data-storage-disconnect")) void disconnect(target.dataset.storageDisconnect, target);
    if (target.hasAttribute("data-delivery-send")) void send(senders.get(target));
    if (target.hasAttribute("data-delivery-retry")) void retry(target.dataset.deliveryRetry);
    if (target.hasAttribute("data-delivery-cancel")) void cancel(target.dataset.deliveryCancel);
  });
  window.addEventListener("video-task-restored", ({detail}) => {
    if (!detail.error && detail.result) void onRendered(detail.result);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void refresh();
  });
})();
