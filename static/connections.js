(() => {
  const $ = (sel) => document.querySelector(sel);

  function esc(str) {
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
        const d = await res.json();
        if (d?.detail) detail = typeof d.detail === "string" ? d.detail : "Confira os campos informados e tente novamente.";
      } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function feedback(msg, isError = false) {
    for (const selector of ["#model-feedback", "#connection-feedback"]) {
      const el = $(selector);
      if (!el) continue;
      el.textContent = msg;
      el.classList.remove("hidden");
      el.style.color = isError ? "#fca5a5" : "#7dd3fc";
    }
  }

  function socialFeedback(message, isError = false) {
    const el = $("#social-feedback");
    if (!el) return;
    el.textContent = message;
    el.classList.remove("hidden");
    el.classList.toggle("is-error", isError);
  }

  let socialRequest = 0;
  let verifyingYouTube = false;

  async function loadSocialAccounts() {
    const grid = $("#social-grid");
    if (!grid) return;
    const request = ++socialRequest;
    try {
      const data = await api("/api/social/accounts");
      if (request !== socialRequest) return;
      grid.innerHTML = (data.networks || [])
        .map((n) => {
          const isYt = n.id === "youtube";
          let actions = "";
          if (!n.configured) {
            actions = `<span class="hint">Integração ainda não configurada pelo administrador.</span>`;
          } else if (!n.connected) {
            actions = `<a class="primary" style="display:inline-block;text-decoration:none" href="/auth/${n.id}/start?permission=view">Conectar conta ${esc(n.name)}</a>`;
          } else {
            actions = `<div class="btn-row">
              ${
                isYt && !n.can_upload
                  ? `<a class="primary" style="text-decoration:none" href="/auth/youtube/start?permission=publish">Autorizar envio ao YouTube</a>`
                  : ""
              }
              ${
                isYt
                  ? `<button class="outline" data-verify-youtube type="button" ${verifyingYouTube ? 'disabled aria-busy="true"' : ''} onclick="window._verifyYouTube()">${verifyingYouTube ? 'Verificando canal…' : 'Verificar canal'}</button>`
                  : ""
              }
              <button class="danger" type="button" onclick="window._disconnectSocial('${n.id}')">Desconectar</button>
            </div>`;
          }

          return `
            <article class="panel" data-social-provider="${esc(n.id)}">
              <div class="pane-top">
                <h3>${esc(n.name)}</h3>
                <span class="fine">${n.connected ? "CONECTADO" : n.needs_reconnect ? "RECONECTAR" : n.configured ? "PRONTO PARA CONECTAR" : "NÃO CONFIGURADO"}</span>
              </div>
              ${
                n.account?.name
                  ? `<p class="small">Canal/Conta: <b>${esc(n.account.name)}</b> ${n.account.id ? `(${esc(n.account.id)})` : ""}</p>`
                  : `<p class="muted small">Nenhuma conta vinculada.</p>`
              }
              ${
                isYt && n.connected
                  ? `<p class="hint">${n.can_upload ? "Envio autorizado. Você escolhe e aprova cada corte antes de enviar." : "Canal conectado para consulta. Autorize o envio para publicar seus cortes."}</p>`
                  : ""
              }
              ${n.needs_reconnect ? '<p class="hint">A autorização expirou. Conecte novamente pela tela oficial.</p>' : ''}
              <div style="margin-top:12px">${actions}</div>
            </article>`;
        })
        .join("");
    } catch (err) {
      if (request !== socialRequest) return;
      grid.innerHTML = `<article class="panel"><p>${esc(err.message)}</p></article>`;
    }
  }

  window._verifyYouTube = async () => {
    if (verifyingYouTube) return;
    verifyingYouTube = true;
    const button = $("[data-verify-youtube]");
    if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); button.textContent = "Verificando canal…"; }
    socialFeedback("Consultando o canal diretamente no YouTube…");
    try {
      const res = await api("/api/social/youtube/channel");
      socialFeedback(`Canal verificado: ${res.channel.name} (${res.channel.id}). ${res.can_upload ? "Pronto para receber os cortes que você aprovar." : "Conectado para consulta. Autorize o envio para publicar os cortes."}`);
      await loadSocialAccounts();
    } catch (err) {
      socialFeedback(err.message, true);
    } finally {
      verifyingYouTube = false;
      const current = $("[data-verify-youtube]");
      if (current) { current.disabled = false; current.removeAttribute("aria-busy"); current.textContent = "Verificar canal"; }
    }
  };

  window._disconnectSocial = async (provider) => {
    try {
      await api(`/api/social/${provider}/disconnect`, { method: "POST" });
      loadSocialAccounts();
    } catch (err) {
      alert(err.message);
    }
  };

  async function loadComfyWorkflows() {
    const root = $("#comfy-workflows");
    if (!root) return;
    try {
      const data = await api("/api/creative/workflows");
      const items = data.workflows || [];
      root.innerHTML = items.length
        ? items.map((item) => `<article class="panel"><div class="pane-top"><h3>${esc(item.title)}</h3><span class="fine">COMFY CLOUD</span></div><p class="hint">Abre o workflow no Comfy Cloud, na sua conta. Executar la pode consumir creditos.</p><a class="primary comfy-launch" target="_blank" rel="noopener noreferrer" href="${esc(item.url)}">Abrir workflow no Comfy Cloud</a></article>`).join("")
        : `<article class="panel"><p class="hint">Nenhum workflow configurado para este Studio. A conexao MCP do ChatGPT nao equivale a uma credencial do servidor Railway.</p></article>`;
    } catch (error) {
      root.textContent = `Nao foi possivel consultar workflows: ${error.message}`;
    }
  }

  $("#reload-comfy")?.addEventListener("click", loadComfyWorkflows);

  let cachedProviders = [];
  let providerPresets = [];
  let selectedModels = {};
  let providersRequest = 0;
  let modelRequest = 0;
  let modelAbort = null;
  let modelSearchTimer = null;
  let modelItems = [];
  let modelsLoading = false;
  const providerActions = new Set();
  const stageLabels = { moments: "melhores momentos", titles: "títulos e descrições" };

  function safeHttps(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === "https:" && !parsed.username && !parsed.password ? parsed.href : "";
    } catch (_) { return ""; }
  }

  async function busy(button, text, action) {
    if (!button || button.disabled) return;
    const label = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = text;
    try { return await action(); }
    finally {
      button.textContent = label;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  async function loadCatalog() {
    try {
      const data = await api("/api/ai/catalog");
      providerPresets = Array.isArray(data.providers) ? data.providers : [];
      const select = $("#provider-preset");
      const current = select.value;
      select.innerHTML = `<option value="custom">Provedor personalizado</option>` + providerPresets
        .filter((p) => p.id !== "openrouter")
        .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
      if (providerPresets.some((p) => p.id === current)) select.value = current;
      if (data.notice) $("#catalog-notice").textContent = data.notice;
      const sourceUrl = safeHttps(data.source?.url);
      if (sourceUrl) $("#catalog-source").href = sourceUrl;
      applyPreset(false);
    } catch (_) {
      $("#catalog-notice").textContent = "Não foi possível carregar os atalhos do catálogo. Você ainda pode conectar o OpenRouter ou informar um provedor personalizado.";
    }
  }

  function applyPreset(clearKey = true) {
    const preset = providerPresets.find((p) => p.id === $("#provider-preset").value);
    const name = $("#custom-name");
    const endpoint = $("#custom-endpoint");
    if (clearKey) $("#custom-key").value = "";
    name.readOnly = !!preset;
    endpoint.readOnly = !!preset;
    $("#preset-details").classList.toggle("hidden", !preset);
    if (preset) {
      name.value = preset.name;
      endpoint.value = preset.endpoint;
      $("#preset-price").textContent = preset.price_note || "Consulte as condições do plano gratuito na sua conta.";
      $("#preset-quota").textContent = preset.quota_note || "Os limites variam por conta e modelo.";
      const link = $("#preset-key-link");
      const href = safeHttps(preset.key_url);
      link.classList.toggle("hidden", !href);
      if (href) link.href = href;
      else link.removeAttribute("href");
    } else if (clearKey) {
      name.value = "";
      endpoint.value = "";
    }
  }

  function quotaText(provider) {
    const quota = provider.quota || provider.free_model_daily_requests;
    if (!quota || !Number.isFinite(Number(quota.limit)) || quota.limit == null) {
      return provider.kind === "openrouter"
        ? "Os modelos gratuitos compartilham a cota da sua conta. Atualizar o catálogo não renova essa cota."
        : "Confirme o plano e os limites na conta do provedor; modelos listados não garantem gratuidade.";
    }
    const used = quota.used == null ? null : Number(quota.used);
    const remaining = quota.remaining == null ? null : Number(quota.remaining);
    return `Cota diária informada: ${Number.isFinite(used) ? used : "—"} de ${Number(quota.limit)} pedidos usados${Number.isFinite(remaining) ? ` · ${remaining} restantes` : ""}. Consultada na última atualização.`;
  }

  function renderProviders() {
    const list = $("#provider-list");
    list.innerHTML = cachedProviders.length ? cachedProviders.map((p) => {
      const freeActive = p.kind === "openrouter" && p.free_router_available === true && ["moments", "titles"].every((stage) => selectedModels[stage]?.provider_id === p.id && selectedModels[stage]?.model_id === "openrouter/free" && selectedModels[stage]?.free_only === true);
      return `<article class="panel connected-provider" data-provider-id="${esc(p.id)}">
        <div class="pane-top"><div><h3>${esc(p.name)}</h3><p class="hint">${p.key_saved ? "Chave salva e protegida" : "Confira a conexão"}</p></div><span class="free-badge">${Number(p.free_model_count) || 0} gratuitos</span></div>
        <div class="provider-stats"><span>${Number(p.model_count) || 0} modelos no catálogo</span>${freeActive ? `<span class="free-active">Automático gratuito ativo</span>` : ""}</div>
        <p class="hint">${esc(quotaText(p))}</p>
        ${p.kind === "openrouter" ? (p.free_router_available === true ? `<p class="hint">O modo automático usa <b>openrouter/free</b> e não troca para modelos pagos. O teste faz um pedido curto e consome uma solicitação da sua cota gratuita.</p>` : `<p class="hint">O roteador gratuito não está confirmado neste catálogo. Atualize o catálogo para verificar sua disponibilidade e seu preço.</p>`) : ""}
        <div class="btn-row provider-actions">
          ${p.kind === "openrouter" ? `<button class="primary" type="button" data-action="free-defaults" ${p.free_router_available ? "" : 'disabled title="Atualize o catálogo para verificar o roteador gratuito"'}>Usar automático gratuito</button><button class="outline" type="button" data-action="test" ${p.free_router_available ? "" : 'disabled title="Roteador gratuito indisponível no catálogo"'}>Testar IA gratuita</button>` : ""}
          <button class="outline" type="button" data-action="refresh">Atualizar catálogo</button>
          <button class="danger" type="button" data-action="delete">Remover conexão</button>
        </div><p class="provider-feedback hint hidden" role="status" aria-live="polite"></p>
      </article>`;
    }).join("") : `<div class="connection-empty"><b>Nenhum provedor conectado ainda.</b><p class="hint">Adicione sua chave acima. Depois, escolha um modelo para cada tarefa.</p></div>`;
  }

  async function loadProviders(preferredId = "") {
    const request = ++providersRequest;
    try {
      const data = await api("/api/ai/providers");
      if (request !== providersRequest) return;
      cachedProviders = data.providers || [];
      selectedModels = data.selected || {};
      renderProviders();
      const select = $("#model-provider");
      const previous = select.value;
      select.innerHTML = cachedProviders.length
        ? cachedProviders.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} (${Number(p.model_count) || 0})</option>`).join("")
        : `<option value="">Primeiro conecte um provedor</option>`;
      const desired = preferredId || previous;
      if (cachedProviders.some((p) => p.id === desired)) select.value = desired;
      const current = cachedProviders.find((p) => p.id === select.value);
      if (select.value !== previous || preferredId) $("#model-cost").value = current?.kind === "openrouter" ? "free" : "all";
      const entries = Object.entries(selectedModels).filter(([stage]) => stage in stageLabels);
      $("#selected-models").innerHTML = entries.length ? entries.map(([stage, info]) => {
        const provider = cachedProviders.find((p) => p.id === info.provider_id);
        const verifiedFreeSelection = provider?.kind === "openrouter" && provider.free_router_available === true && info.model_id === "openrouter/free" && info.free_only === true;
        return `<span class="sel-chip"><b>${esc(data.stages?.[stage] || stageLabels[stage])}:</b> ${esc(provider?.name || "Provedor")} · ${esc(verifiedFreeSelection ? "Automático gratuito" : info.model_id)}</span>`;
      }).join("") : `<span class="hint">Escolha um modelo para ativar a IA nas tarefas.</span>`;
      await loadModelsForSelectedProvider();
    } catch (err) { feedback(err.message, true); }
  }

  function isVerifiedFreeRouter(model, provider = cachedProviders.find((p) => p.id === $("#model-provider").value)) {
    return model?.id === "openrouter/free" && model.price_class === "free" && provider?.kind === "openrouter" && provider.free_router_available === true;
  }

  function modelLabel(model) {
    if (isVerifiedFreeRouter(model)) return "Automático · somente gratuitos (openrouter/free)";
    const label = model.price_label || ({ free: "Gratuito", paid: "Pago", unknown: "Preço não informado" }[model.price_class] || "Preço não informado");
    if (model.id === "openrouter/free") return `${label} · ${model.id}`;
    return `${label} · ${model.name} (${model.id})`;
  }

  function updateModelNote() {
    const model = modelItems.find((m) => m.id === $("#model-choice").value);
    $("#save-model").disabled = !model || modelsLoading || model.chat_compatible === false;
    $("#model-price-note").textContent = !model
      ? "Escolha um modelo para ver suas condições."
      : isVerifiedFreeRouter(model)
        ? "O OpenRouter escolhe um modelo gratuito compatível por pedido. As cotas da conta continuam valendo; não há troca para modelos pagos."
        : model.id === "openrouter/free"
          ? "Este ID não está confirmado como roteador gratuito nesta conexão. Confira o provedor e o preço informado antes de usar."
        : model.price_class === "free"
          ? "Preço informado como zero no catálogo. O uso continua sujeito às cotas, à disponibilidade e às condições do provedor."
          : model.price_class === "paid"
            ? "Este modelo pode consumir créditos. Escolha “Somente gratuitos” para consultar os modelos de preço zero."
            : "O provedor não informou preço suficiente para confirmar gratuidade. Verifique as condições do modelo na sua conta antes de usar.";
  }

  async function loadModelsForSelectedProvider(append = false) {
    if (append && modelsLoading) return;
    modelAbort?.abort();
    modelAbort = new AbortController();
    const request = ++modelRequest;
    const pid = $("#model-provider").value;
    const choice = $("#model-choice");
    const more = $("#load-more-models");
    const previous = append ? choice.value : selectedModels[$("#model-stage").value]?.provider_id === pid ? selectedModels[$("#model-stage").value]?.model_id : "";
    const offset = append ? modelItems.length : 0;
    if (!append) modelItems = [];
    modelsLoading = true;
    choice.disabled = true;
    $("#save-model").disabled = true;
    more.disabled = true;
    $("#model-picker").setAttribute("aria-busy", "true");
    if (!append) {
      more.classList.add("hidden");
      choice.innerHTML = `<option value="">${pid ? "Carregando modelos…" : "Primeiro conecte um provedor"}</option>`;
    }
    $("#model-count").textContent = pid ? "Consultando modelos…" : "Conecte um provedor para consultar os modelos.";
    try {
      if (!pid) return;
      const params = new URLSearchParams({ q: $("#model-search").value.trim(), cost: $("#model-cost").value, offset: String(offset), limit: "100" });
      const data = await api(`/api/ai/providers/${encodeURIComponent(pid)}/models?${params}`, { signal: modelAbort.signal });
      if (request !== modelRequest) return;
      const found = Array.isArray(data.models) ? data.models : [];
      modelItems = append ? modelItems.concat(found) : found;
      choice.innerHTML = modelItems.length ? modelItems.map((model) => `<option value="${esc(model.id)}" ${model.chat_compatible === false ? "disabled" : ""}>${esc(modelLabel(model))}</option>`).join("")
        : `<option value="">Nenhum modelo encontrado com estes filtros</option>`;
      if (modelItems.some((model) => model.id === previous && model.chat_compatible !== false)) choice.value = previous;
      const counts = data.counts || {};
      const totals = Number.isFinite(Number(counts.free)) ? ` · ${Number(counts.free)} gratuitos encontrados` : "";
      $("#model-count").textContent = `${modelItems.length} de ${Number(data.total) || 0} modelos encontrados${totals}.`;
      more.classList.toggle("hidden", !data.has_more);
      if (!modelItems.length && $("#model-cost").value === "free") {
        $("#model-count").textContent += " Atualize o catálogo ou consulte “Todos os modelos”. Preço não informado não é classificado como gratuito.";
      }
    } catch (err) {
      if (request !== modelRequest || err.name === "AbortError") return;
      if (!append) choice.innerHTML = `<option value="">Não foi possível carregar os modelos</option>`;
      $("#model-count").textContent = "Falha ao carregar. Tente atualizar o catálogo ou mudar o filtro.";
      feedback(err.message, true);
    } finally {
      if (request === modelRequest) {
        modelsLoading = false;
        choice.disabled = !modelItems.length;
        more.disabled = false;
        $("#model-picker").removeAttribute("aria-busy");
        updateModelNote();
      }
    }
  }

  $("#provider-list")?.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const card = button.closest("[data-provider-id]");
    const id = card.dataset.providerId;
    if (providerActions.has(id)) return;
    providerActions.add(id);
    const status = card.querySelector(".provider-feedback");
    const action = button.dataset.action;
    const labels = { refresh: "Atualizando catálogo…", "free-defaults": "Configurando modo gratuito…", test: "Testando IA gratuita…", delete: "Removendo conexão…" };
    const controls = Array.from(card.querySelectorAll("button"));
    const disabled = controls.map((control) => control.disabled);
    controls.forEach((control) => { control.disabled = true; });
    card.setAttribute("aria-busy", "true");
    status.textContent = labels[action];
    status.classList.remove("hidden");
    try {
      const path = `/api/ai/providers/${encodeURIComponent(id)}${action === "delete" ? "" : `/${action}`}`;
      const data = await api(path, { method: action === "delete" ? "DELETE" : "POST" });
      const message = action === "test"
        ? `${data.message || "A IA gratuita respondeu ao teste."}${data.model_id ? ` Modelo: ${data.model_id}.` : ""}`
        : action === "free-defaults"
          ? data.message || "Seleção automática gratuita ativada para momentos e títulos."
          : action === "delete" ? "Conexão removida. As tarefas vinculadas ficaram sem modelo." : "Catálogo atualizado, incluindo os modelos gratuitos disponíveis.";
      feedback(message);
      if (action === "test") {
        status.textContent = message;
        status.style.color = "#86efac";
      } else {
        await loadProviders(action === "delete" ? "" : id);
        window.refreshStudioCapabilities?.();
      }
    } catch (err) {
      status.textContent = err.message;
      status.style.color = "#fca5a5";
      feedback(err.message, true);
    } finally {
      providerActions.delete(id);
      controls.forEach((control, index) => { control.disabled = disabled[index]; });
      card.removeAttribute("aria-busy");
    }
  });

  $("#openrouter-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    await busy(form.querySelector('button[type="submit"]'), "Validando chave…", async () => {
      try {
        feedback("Validando sua chave e consultando o catálogo completo do OpenRouter…");
        const data = await api("/api/ai/providers", { method: "POST", body: JSON.stringify({ kind: "openrouter", name: "OpenRouter", api_key: $("#openrouter-key").value.trim() }) });
        $("#openrouter-key").value = "";
        let message = "OpenRouter conectado. Os modelos gratuitos já podem ser consultados.";
        if ($("#openrouter-auto-free").checked) {
          if (data.provider?.free_router_available) {
            try {
              await api(`/api/ai/providers/${encodeURIComponent(data.provider.id)}/free-defaults`, { method: "POST" });
              message = "OpenRouter conectado: seleção automática gratuita pronta para momentos e títulos.";
            } catch (err) {
              message = `Chave salva. Não foi possível ativar o automático gratuito: ${err.message}`;
            }
          } else {
            message = "Chave salva. O roteador gratuito não apareceu no catálogo; escolha um modelo listado ou atualize o catálogo.";
          }
        }
        feedback(message);
        await loadProviders(data.provider?.id || "");
        window.refreshStudioCapabilities?.();
      } catch (err) { feedback(err.message, true); }
    });
  });

  $("#custom-provider-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    await busy(form.querySelector('button[type="submit"]'), "Consultando provedor…", async () => {
      try {
        feedback("Validando a conexão e consultando os modelos do provedor…");
        const preset = providerPresets.find((p) => p.id === $("#provider-preset").value);
        const payload = { kind: preset?.kind || "custom", name: $("#custom-name").value.trim(), endpoint: $("#custom-endpoint").value.trim(), api_key: $("#custom-key").value.trim() };
        if (preset) payload.preset_id = preset.id;
        const data = await api("/api/ai/providers", { method: "POST", body: JSON.stringify(payload) });
        $("#custom-key").value = "";
        feedback("Provedor conectado. Confira as condições da sua conta e escolha um modelo para a tarefa.");
        await loadProviders(data.provider?.id || "");
      } catch (err) { feedback(err.message, true); }
    });
  });

  $("#provider-preset")?.addEventListener("change", () => applyPreset());
  $("#model-provider")?.addEventListener("change", () => {
    clearTimeout(modelSearchTimer);
    const provider = cachedProviders.find((p) => p.id === $("#model-provider").value);
    $("#model-cost").value = provider?.kind === "openrouter" ? "free" : "all";
    $("#model-search").value = "";
    loadModelsForSelectedProvider();
  });
  $("#model-cost")?.addEventListener("change", () => { clearTimeout(modelSearchTimer); loadModelsForSelectedProvider(); });
  $("#model-search")?.addEventListener("input", () => {
    clearTimeout(modelSearchTimer);
    modelAbort?.abort();
    ++modelRequest;
    modelsLoading = true;
    $("#model-choice").disabled = true;
    $("#save-model").disabled = true;
    $("#load-more-models").disabled = true;
    modelSearchTimer = setTimeout(() => loadModelsForSelectedProvider(), 250);
  });
  $("#model-stage")?.addEventListener("change", () => {
    const selected = selectedModels[$("#model-stage").value];
    if (selected?.provider_id === $("#model-provider").value && modelItems.some((m) => m.id === selected.model_id)) $("#model-choice").value = selected.model_id;
    updateModelNote();
  });
  $("#model-choice")?.addEventListener("change", updateModelNote);
  $("#load-more-models")?.addEventListener("click", () => loadModelsForSelectedProvider(true));

  $("#save-model")?.addEventListener("click", async (event) => {
    const provider_id = $("#model-provider").value;
    const model_id = $("#model-choice").value;
    const stage = $("#model-stage").value;
    if (!provider_id || !model_id || modelsLoading) return feedback("Selecione um provedor e um modelo.", true);
    await busy(event.currentTarget, "Salvando escolha…", async () => {
      try {
        await api("/api/ai/selection", { method: "POST", body: JSON.stringify({ stage, provider_id, model_id }) });
        await loadProviders();
        const provider = cachedProviders.find((p) => p.id === provider_id);
        const selected = selectedModels[stage];
        const verifiedFreeSelection = provider?.kind === "openrouter" && provider.free_router_available === true && selected?.model_id === model_id && selected?.provider_id === provider_id && selected?.free_only === true && model_id === "openrouter/free";
        feedback(`Modelo salvo para ${stageLabels[stage] || stage}: ${verifiedFreeSelection ? "automático gratuito" : model_id}.`);
        window.refreshStudioCapabilities?.();
      } catch (err) { feedback(err.message, true); }
    });
    updateModelNote();
  });

  $("#reload-social")?.addEventListener("click", loadSocialAccounts);

  let catalogLoaded = false;
  window.reloadConnections = () => {
    loadSocialAccounts();
    loadProviders();
    loadComfyWorkflows();
    if (!catalogLoaded) { catalogLoaded = true; loadCatalog(); }
  };

  window.reloadConnections();
})();
