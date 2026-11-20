/* Shared, opt-in download and external storage controls for both editors. */
(() => {
  "use strict";
  const state = {
    settings: null, settingsError: "", videoId: null, jobs: [], historyError: "",
    historyRequest: 0, poll: null, saving: false, connecting: false, settingsDirty: false, downloads: new Map(),
    capacity: null, capacityError: "", capacityLoading: true, capacityPoll: null, optimizing: false
  };
  const senders = new WeakMap(), pendingSends = new Set(), pendingRetries = new Set(), pendingCancels = new Set(), attempted = new Set();
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const qualityLabel = quality => quality === "preview" ? "prévia 720p" : "final 1080p";
  const providerLabel = provider => provider === "github" ? "GitHub" : "Google Drive";
  const connected = (provider = state.settings?.provider) => Boolean(state.settings?.[provider]?.connected);
  const field = name => $('[data-storage-field="' + name + '"]');
  let settingsRequest = null, capacityRequest = null;

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
      '<p>Baixe seus cortes ou guarde os finais no Drive para abrir espaço para os próximos vídeos.</p></div>',
      '<button type="button" class="delivery-button" data-storage-refresh>Atualizar espaço e conexões</button></div>',
      '<p class="delivery-feedback" data-delivery-feedback role="status" aria-live="polite" hidden></p>',
      '<p class="delivery-feedback delivery-error" data-storage-error role="status" hidden></p>',
      '<section class="delivery-capacity" aria-label="Capacidade de armazenamento" aria-live="polite" data-storage-capacity>',
      '<div class="delivery-capacity-grid"><article class="delivery-capacity-card" data-capacity-local>',
      '<h3>Espaço de edição</h3><p class="delivery-capacity-value" data-local-free>Consultando…</p>',
      '<p class="delivery-hint" data-local-usage>Espaço no servidor para gravar e preparar os vídeos.</p>',
      '<progress max="100" value="0" aria-label="Ocupação do espaço de edição" data-local-meter hidden></progress>',
      '<p class="delivery-hint" data-local-detail>Gravações e renderizações precisam de espaço temporário aqui.</p></article>',
      '<article class="delivery-capacity-card" data-capacity-drive><h3>Google Drive</h3>',
      '<p class="delivery-capacity-value" data-drive-free>Consultando…</p>',
      '<p class="delivery-hint" data-drive-usage>Capacidade da conta autorizada no AutoClips.</p>',
      '<progress max="100" value="0" aria-label="Ocupação do Google Drive" data-drive-meter hidden></progress>',
      '<p class="delivery-hint" data-drive-capacity-detail>A capacidade disponível será consultada na sua conta.</p></article></div>',
      '<p class="delivery-hint" data-archive-pending hidden></p>',
      '<p class="delivery-hint delivery-archive-total" data-archive-total hidden></p>',
      '<p class="delivery-error" data-capacity-error hidden></p></section>',
      '<div class="delivery-providers">',
      '<article class="delivery-provider"><div class="delivery-heading"><h3>Google Drive</h3><span class="delivery-badge" data-drive-status>Consultando…</span></div>',
      '<p>Salva os vídeos na pasta <strong>AutoClips</strong> do seu Drive.</p>',
      '<p class="delivery-hint" data-drive-detail>Autorize o Drive dentro do AutoClips. Uma conexão no ChatGPT não é transferida para o estúdio.</p>',
      '<div class="delivery-buttons"><a class="delivery-button delivery-primary" data-drive-connect href="/auth/storage/drive/start" hidden>Conectar Google Drive</a>',
      '<button type="button" class="delivery-button delivery-primary" data-drive-optimize hidden>Usar Drive e liberar espaço</button>',
      '<a class="delivery-link" data-drive-folder target="_blank" rel="noopener noreferrer" hidden>Abrir pasta ↗</a>',
      '<button type="button" class="delivery-button delivery-danger" data-storage-disconnect="drive" hidden>Desconectar Drive</button></div>',
      '<p class="delivery-hint" data-drive-optimize-help hidden>Ativa o envio dos próximos finais e inclui os finais já gerados. A cópia local só é liberada após a confirmação no Drive. Fontes, gravações em edição e prévias são preservadas.</p></article>',
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
      '<label class="delivery-check delivery-archive-setting"><input type="checkbox" data-storage-field="archive_after_upload" disabled><span><strong>Arquivar finais no Drive e liberar espaço local</strong><small>A cópia no Drive é verificada antes de liberar espaço. Fontes, gravações em edição e prévias permanecem no estúdio.</small></span></label>',
      '<p class="delivery-hint" data-archive-help>Conecte o Drive e ative o envio automático para usar o arquivamento.</p>',
      '<p class="delivery-hint">A qualidade escolhida vale para download e envio automático. O final continua exigindo sua revisão e aprovação da prévia.</p>',
      '<button type="submit" class="delivery-button delivery-primary" data-storage-save disabled>Salvar automação</button>',
      '<span class="delivery-hint" data-storage-saved>Consultando preferências…</span></form>'
    ].join("");
    $("[data-github-form]", root).addEventListener("submit", connectGitHub);
    $("[data-storage-form]", root).addEventListener("submit", saveSettings);
    field("provider").addEventListener("change", updateUploadAvailability);
    field("auto_upload").addEventListener("change", updateUploadAvailability);
    field("qualities").addEventListener("change", updateUploadAvailability);
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
    input.disabled = state.saving || state.optimizing || !state.settings || !connected(provider);
    if (!connected(provider)) input.checked = false;
    $("[data-auto-upload-help]").textContent = connected(provider)
      ? "Destino conectado: " + providerLabel(provider) + (provider === "github" ? " · " + (state.settings.github.repository || "") : " · pasta AutoClips") + "."
      : "Conecte " + providerLabel(provider) + " para habilitar o envio automático.";
    const archive = field("archive_after_upload"), allowed = provider === "drive" && connected("drive") && input.checked;
    archive.disabled = state.saving || state.optimizing || !allowed;
    if (!allowed) archive.checked = false;
    $("[data-archive-help]").textContent = allowed
      ? field("qualities").value === "preview"
        ? "Você selecionou apenas prévias. Inclua os finais em Qual arquivo? para que a automação libere espaço."
        : "Somente os finais são arquivados. Downloads e prévias continuam acessíveis pelos mesmos botões."
      : "Escolha Google Drive e ative o envio automático para arquivar os finais.";
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
        $("[data-drive-optimize]").hidden = !drive.connected;
        $("[data-drive-optimize]").disabled = state.saving || state.connecting || state.optimizing;
        $("[data-drive-optimize]").textContent = state.optimizing ? "Preparando envio e arquivamento…" : "Usar Drive e liberar espaço";
        $("[data-drive-optimize-help]").hidden = !drive.connected;
        $('[data-storage-disconnect="drive"]').hidden = !drive.connected;
        $('[data-storage-disconnect="drive"]').disabled = state.optimizing;
        $('[data-storage-disconnect="github"]').hidden = !github.connected;
        $("[data-drive-detail]").textContent = drive.connected
          ? "Autorizado para salvar os arquivos do AutoClips na sua conta."
          : drive.configured ? "Conecte o Drive aqui para autorizar o AutoClips. A conexão do ChatGPT e o login no estúdio não concedem acesso aos seus arquivos."
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
          field("archive_after_upload").checked = Boolean(settings.archive_after_upload);
          field("provider").value = settings.provider || "drive";
          field("qualities").value = settings.qualities?.length === 2 ? "both" : settings.qualities?.[0] || "final";
          $("[data-storage-saved]").textContent = "Preferências salvas para sua conta.";
        }
      }
      for (const input of $$("[data-storage-field]:not([data-storage-field=auto_upload]),[data-storage-save]"))
        input.disabled = !settings || state.saving || state.optimizing;
      $("[data-github-submit]").disabled = !settings || state.connecting || state.optimizing;
      updateUploadAvailability();
    }
    for (const summary of $$("[data-delivery-summary]")) {
      if (!settings) { summary.textContent = state.settingsError || "Consultando preferências de download e envio…"; continue; }
      const qualities = settings.qualities?.length === 2 ? "prévia e final" : qualityLabel(settings.qualities?.[0]);
      summary.textContent = "Download automático: " + (settings.auto_download ? "ativado enquanto esta aba estiver aberta" : "desativado") +
        ". Envio automático: " + (settings.auto_upload && connected() ? providerLabel(settings.provider) : "desativado") +
        ". Arquivos: " + qualities + "." + (settings.archive_after_upload && settings.auto_upload &&
          settings.provider === "drive" && connected("drive") ? " Finais arquivados no Drive liberam espaço local após a verificação." : "");
    }
    updateSenders();
  }
  const capacityNumber = value => value == null || value === "" || !Number.isFinite(Number(value)) || Number(value) < 0 ? null : Number(value);
  function storageBytes(value) {
    const number = capacityNumber(value);
    if (number == null) return "Não informado";
    const units = ["B", "KB", "MB", "GB", "TB"], index = number > 0
      ? Math.max(0, Math.min(units.length - 1, Math.floor(Math.log(number) / Math.log(1000)))) : 0;
    return (number / 1000 ** index).toLocaleString("pt-BR", {maximumFractionDigits:index > 0 ? 1 : 0}) + " " + units[index];
  }
  function renderCapacity() {
    const root = $("[data-storage-capacity]");
    if (!root) return;
    root.setAttribute("aria-busy", String(state.capacityLoading));
    const error = $("[data-capacity-error]");
    error.hidden = !state.capacityError;
    error.textContent = state.capacityError;
    const capacity = state.capacityError ? null : state.capacity;
    for (const name of ["local", "drive"]) {
      const meter = $("[data-" + name + "-meter]");
      meter.hidden = true;
      $("[data-capacity-" + name + "]").classList.remove("is-warning");
    }
    $("[data-archive-total]").hidden = true;
    $("[data-archive-pending]").hidden = true;
    if (!capacity) {
      for (const name of ["local", "drive"]) {
        $("[data-" + name + "-free]").textContent = state.capacityLoading ? "Consultando…" : "Indisponível no momento";
        $("[data-" + name + "-usage]").textContent = state.capacityLoading ? "Consultando a capacidade disponível…" : "Use Atualizar espaço e conexões para tentar novamente.";
      }
      $("[data-local-detail]").textContent = "Gravações e renderizações precisam de espaço temporário aqui.";
      $("[data-drive-capacity-detail]").textContent = "A capacidade disponível será consultada na sua conta.";
      return;
    }
    const displayMeter = (name, used, total) => {
      if (used == null || total == null || total <= 0) return;
      const meter = $("[data-" + name + "-meter]");
      meter.value = Math.max(0, Math.min(100, used / total * 100));
      meter.hidden = false;
    };
    const local = capacity.local || {}, localFree = capacityNumber(local.free_bytes), localUsed = capacityNumber(local.used_bytes), localTotal = capacityNumber(local.total_bytes);
    $("[data-local-free]").textContent = localFree == null ? "Espaço livre não informado" : storageBytes(localFree) + " livres";
    $("[data-local-usage]").textContent = localUsed != null && localTotal != null
      ? storageBytes(localUsed) + " usados de " + storageBytes(localTotal)
      : "Espaço no servidor para gravar e preparar os vídeos.";
    $("[data-capacity-local]").classList.toggle("is-warning", Boolean(local.low_space));
    $("[data-local-detail]").textContent = local.low_space
      ? "Pouco espaço para gerar vídeos. Arquive finais no Drive antes de iniciar uma nova gravação."
      : "Gravações e renderizações precisam de espaço temporário aqui, mesmo com o Drive conectado.";
    displayMeter("local", localUsed, localTotal);
    const drive = capacity.drive || {}, driveFree = capacityNumber(drive.free_bytes), driveUsed = capacityNumber(drive.used_bytes), driveTotal = capacityNumber(drive.total_bytes);
    if (drive.status === "error") {
      $("[data-drive-free]").textContent = "Não foi possível consultar";
      $("[data-drive-usage]").textContent = drive.error || "Atualize a conexão para consultar a capacidade da conta.";
      $("[data-drive-capacity-detail]").textContent = "O espaço disponível não foi confirmado. Atualize as conexões e confira a autorização do Drive.";
    } else if (!drive.connected || drive.status === "disconnected") {
      $("[data-drive-free]").textContent = "Conecte sua conta";
      $("[data-drive-usage]").textContent = "A capacidade será consultada após autorizar o AutoClips.";
      $("[data-drive-capacity-detail]").textContent = "Use Conectar Google Drive abaixo. A conexão do ChatGPT não é transferida para o estúdio.";
    } else {
      $("[data-drive-free]").textContent = driveFree == null ? "Espaço livre não informado" : storageBytes(driveFree) + " livres";
      $("[data-drive-usage]").textContent = driveUsed != null
        ? storageBytes(driveUsed) + " usados" + (driveTotal == null ? " · limite não informado pelo Drive" : " de " + storageBytes(driveTotal))
        : "Uso da conta não informado pelo Drive.";
      $("[data-drive-capacity-detail]").textContent = "O espaço é compartilhado com os demais serviços da sua conta Google.";
      $("[data-capacity-drive]").classList.toggle("is-warning", driveFree === 0);
      displayMeter("drive", driveUsed, driveTotal);
    }
    const archivedCount = capacityNumber(capacity.archive?.archived_count), archivedBytes = capacityNumber(capacity.archive?.archived_bytes);
    if (archivedCount > 0) {
      const summary = $("[data-archive-total]");
      summary.textContent = archivedCount.toLocaleString("pt-BR") + (archivedCount === 1 ? " final arquivado no Drive" : " finais arquivados no Drive") +
        (archivedBytes == null ? "." : " · " + storageBytes(archivedBytes) + " ao todo.");
      summary.hidden = false;
    }
    const pendingCount = capacityNumber(capacity.archive?.pending_count);
    if (pendingCount > 0) {
      const pending = $("[data-archive-pending]");
      pending.textContent = pendingCount.toLocaleString("pt-BR") + (pendingCount === 1 ? " final aguardando arquivamento." : " finais aguardando arquivamento.") + " O espaço será atualizado automaticamente.";
      pending.hidden = false;
    }
  }
  async function refreshCapacity() {
    if (!$("[data-storage-capacity]")) return null;
    clearTimeout(state.capacityPoll);
    if (capacityRequest) return capacityRequest;
    capacityRequest = (async () => {
      state.capacityLoading = true; renderCapacity();
      try {
        state.capacity = await request("/api/storage/capacity", {cache:"no-store"});
        state.capacityError = "";
      } catch (error) {
        state.capacityError = "Não foi possível consultar o armazenamento. " + error.message;
      } finally {
        state.capacityLoading = false; capacityRequest = null; renderCapacity();
        if (!state.capacityError && capacityNumber(state.capacity?.archive?.pending_count) > 0)
          state.capacityPoll = setTimeout(refreshCapacity, 10000);
      }
      return state.capacityError ? null : state.capacity;
    })();
    return capacityRequest;
  }
  async function optimizeDrive() {
    if (!state.settings || !connected("drive") || state.saving || state.connecting || state.optimizing) return;
    state.optimizing = true; renderSettings(false);
    feedback("Preparando o envio dos finais ao Drive. O espaço local só será liberado após verificar cada cópia.");
    try {
      const result = await request("/api/storage/optimize-drive", {method:"POST"});
      state.settingsDirty = false;
      if (result.settings) { state.settings = result.settings; state.settingsError = ""; }
      else await refreshSettings();
      renderSettings();
      const summary = result.summary || {}, count = value => Array.isArray(value) ? value.length : Math.max(0, Math.floor(Number(value) || 0));
      const queued = count(summary.queued), archived = count(summary.already_archived), errors = count(summary.errors);
      let message = "Drive ativado para arquivar os próximos finais e liberar espaço após a verificação. ";
      message += queued > 0 ? queued + (queued === 1 ? " arquivo final na fila." : " arquivos finais na fila.") : "Nenhum arquivo final novo entrou na fila.";
      if (archived > 0) message += " " + archived + (archived === 1 ? " final já estava arquivado." : " finais já estavam arquivados.");
      if (errors > 0) message += " " + errors + (errors === 1 ? " arquivo precisa" : " arquivos precisam") + " de atenção. As cópias locais desses arquivos foram preservadas; confira os envios do projeto.";
      feedback(message, errors > 0);
      await Promise.all([refreshCapacity(), refreshJobs()]);
    } catch (error) {
      feedback("Não foi possível preparar o arquivamento. " + error.message, true);
    } finally {
      state.optimizing = false; renderSettings(false);
    }
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
    if (!state.settings || state.saving || state.optimizing) return;
    const qualities = field("qualities").value, provider = field("provider").value;
    const body = {auto_download: field("auto_download").checked,
      auto_upload: field("auto_upload").checked, archive_after_upload: field("archive_after_upload").checked, provider,
      qualities: qualities === "both" ? ["preview","final"] : [qualities]};
    if (body.auto_upload && !connected(provider)) return feedback("Conecte o destino antes de ativar o envio automático.", true);
    if (body.archive_after_upload && (provider !== "drive" || !connected("drive") || !body.auto_upload))
      return feedback("Conecte o Drive e ative o envio automático antes de arquivar os finais.", true);
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
    if (state.connecting || state.optimizing) return;
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
      await Promise.all([refreshSettings(), refreshCapacity()]);
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
          (job.status === "succeeded" && job.provider === "drive" && job.archived
            ? '<p class="delivery-archive-status">Arquivado no Drive · espaço local liberado</p>' : "") +
          (job.archive_pending && !job.archived
            ? '<p class="delivery-hint">Final enviado ao Drive. Verificando a cópia para liberar espaço local…</p>' : "") +
          (job.status === "succeeded" && job.provider === "drive" && !job.archived && job.archive_error
            ? '<p class="delivery-archive-warning">Enviado ao Drive. A cópia local foi preservada porque não foi possível liberar espaço. ' + esc(job.archive_error) + '</p>' : "") +
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
      const archivedBefore = state.jobs.filter(job => job.archived).map(job => job.id).join(",");
      state.jobs = Array.isArray(result.jobs) ? result.jobs : []; state.historyError = "";
      if (state.jobs.filter(job => job.archived).map(job => job.id).join(",") !== archivedBefore) void refreshCapacity();
    } catch (error) {
      if (videoId !== state.videoId || sequence !== state.historyRequest) return;
      state.historyError = "Não foi possível atualizar os envios. " + error.message;
    }
    renderJobs();
    if (!state.historyError && state.jobs.some(job => ["queued","running","retry_wait"].includes(job.status)))
      state.poll = setTimeout(refreshJobs, document.hidden ? 8000 : 2500);
    else if (!state.historyError && state.jobs.some(job => job.archive_pending))
      state.poll = setTimeout(refreshJobs, 10000);
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
    void refreshCapacity();
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
  async function refresh() { await Promise.all([refreshSettings(), refreshCapacity(), refreshJobs()]); }
  const controller = {
    ready:null, refresh, refreshJobs, refreshCapacity, setVideo, onRendered, mountSend, send,
    downloadUrl: input => artifact(input)?.downloadUrl || ""
  };
  window.StorageDeliveryUI = controller;
  buildConnections(); buildProject();
  controller.ready = Promise.all([refreshSettings(), refreshCapacity()]);
  document.addEventListener("click", event => {
    const target = event.target.closest?.("button");
    if (!target || target.disabled) return;
    if (target.hasAttribute("data-storage-refresh")) void refresh();
    if (target.hasAttribute("data-drive-optimize")) void optimizeDrive();
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
