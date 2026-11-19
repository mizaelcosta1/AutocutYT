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
        if (d?.detail) detail = d.detail;
      } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function feedback(msg, isError = false) {
    const el = $("#model-feedback");
    if (!el) return;
    el.textContent = msg;
    el.style.color = isError ? "#fca5a5" : "#7dd3fc";
  }

  async function loadSocialAccounts() {
    const grid = $("#social-grid");
    if (!grid) return;
    try {
      const data = await api("/api/social/accounts");
      grid.innerHTML = (data.networks || [])
        .map((n) => {
          const isYt = n.id === "youtube";
          let actions = "";
          if (!n.configured) {
            actions = `<span class="hint">Defina as variaveis de ambiente do aplicativo no servidor para habilitar.</span>`;
          } else if (!n.connected) {
            actions = `<a class="primary" style="display:inline-block;text-decoration:none" href="/auth/${n.id}/start?permission=view">Conectar conta ${esc(n.name)}</a>`;
          } else {
            actions = `<div class="btn-row">
              ${
                isYt && !n.can_upload
                  ? `<a class="primary" style="text-decoration:none" href="/auth/youtube/start?permission=publish">Ativar publicacao no YouTube</a>`
                  : ""
              }
              ${
                isYt
                  ? `<button class="outline" type="button" onclick="window._verifyYouTube()">Verificar canal</button>`
                  : ""
              }
              <button class="danger" type="button" onclick="window._disconnectSocial('${n.id}')">Desconectar</button>
            </div>`;
          }

          return `
            <article class="panel">
              <div class="pane-top">
                <h3>${esc(n.name)}</h3>
                <span class="fine">${n.connected ? "CONECTADO" : n.configured ? "PRONTO" : "NAO CONFIGURADO"}</span>
              </div>
              ${
                n.account?.name
                  ? `<p class="small">Canal/Conta: <b>${esc(n.account.name)}</b> ${n.account.id ? `(${esc(n.account.id)})` : ""}</p>`
                  : `<p class="muted small">Nenhuma conta vinculada.</p>`
              }
              ${
                isYt && n.connected
                  ? `<p class="hint">Permissao de upload: <b>${n.can_upload ? "Ativa (youtube.upload)" : "Somente leitura (youtube.readonly)"}</b></p>`
                  : ""
              }
              <div style="margin-top:12px">${actions}</div>
            </article>`;
        })
        .join("");
    } catch (err) {
      grid.innerHTML = `<article class="panel"><p>${esc(err.message)}</p></article>`;
    }
  }

  window._verifyYouTube = async () => {
    try {
      const res = await api("/api/social/youtube/channel");
      alert(`Canal verificado: ${res.channel.name} (${res.channel.id})\nUpload ativo: ${res.can_upload ? "Sim" : "Nao"}`);
      loadSocialAccounts();
    } catch (err) {
      alert(err.message);
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

  async function loadProviders() {
    try {
      const data = await api("/api/ai/providers");
      cachedProviders = data.providers || [];
      const list = $("#provider-list");
      const selectProv = $("#model-provider");
      const selModels = $("#selected-models");

      if (list) {
        list.innerHTML = cachedProviders
          .map(
            (p) => `
            <article class="panel">
              <div class="pane-top">
                <div><b>${esc(p.name)}</b> <span class="muted small">(${esc(p.kind)} • final ••••${esc(p.last_four)})</span></div>
                <div class="btn-row">
                  <span class="fine">${p.model_count} modelos</span>
                  <button class="outline" type="button" onclick="window._refreshProvider('${p.id}')">Atualizar catalogo</button>
                  <button class="danger" type="button" onclick="window._deleteProvider('${p.id}')">Remover</button>
                </div>
              </div>
            </article>`
          )
          .join("");
      }

      if (selectProv) {
        const prev = selectProv.value;
        selectProv.innerHTML = cachedProviders.length
          ? cachedProviders.map((p) => `<option value="${p.id}">${esc(p.name)} (${p.model_count})</option>`).join("")
          : `<option value="">Primeiro conecte um provedor</option>`;
        if (prev && cachedProviders.some((p) => p.id === prev)) selectProv.value = prev;
        await loadModelsForSelectedProvider();
      }

      if (selModels) {
        const entries = Object.entries(data.selected || {});
        selModels.innerHTML = entries.length
          ? entries
              .map(
                ([stage, info]) =>
                  `<span class="sel-chip"><b>${esc(data.stages?.[stage] || stage)}:</b> ${esc(info.model_id)}</span>`
              )
              .join("")
          : `<span class="hint">Nenhum modelo vinculado as etapas ainda.</span>`;
      }
    } catch (err) {
      feedback(err.message, true);
    }
  }

  async function loadModelsForSelectedProvider() {
    const pid = $("#model-provider")?.value;
    const choice = $("#model-choice");
    if (!choice) return;
    if (!pid) {
      choice.innerHTML = `<option value="">Escolha um provedor para ver os modelos</option>`;
      return;
    }
    const q = $("#model-search")?.value.trim() || "";
    try {
      const data = await api(`/api/ai/providers/${pid}/models?q=${encodeURIComponent(q)}&limit=250`);
      choice.innerHTML = (data.models || []).length
        ? data.models.map((m) => `<option value="${esc(m.id)}">${esc(m.name)} (${esc(m.id)})</option>`).join("")
        : `<option value="">Nenhum modelo encontrado para "${esc(q)}"</option>`;
    } catch (err) {
      feedback(err.message, true);
    }
  }

  window._refreshProvider = async (id) => {
    try {
      await api(`/api/ai/providers/${id}/refresh`, { method: "POST" });
      feedback("Catalogo atualizado!");
      loadProviders();
    } catch (err) {
      feedback(err.message, true);
    }
  };

  window._deleteProvider = async (id) => {
    try {
      await api(`/api/ai/providers/${id}`, { method: "DELETE" });
      loadProviders();
    } catch (err) {
      feedback(err.message, true);
    }
  };

  $("#openrouter-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = $("#openrouter-key").value.trim();
    try {
      feedback("Validando chave no OpenRouter...");
      await api("/api/ai/providers", {
        method: "POST",
        body: JSON.stringify({ kind: "openrouter", name: "OpenRouter", api_key: key }),
      });
      $("#openrouter-key").value = "";
      feedback("OpenRouter conectado!");
      loadProviders();
    } catch (err) {
      feedback(err.message, true);
    }
  });

  $("#custom-provider-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      feedback("Conectando ao provedor...");
      await api("/api/ai/providers", {
        method: "POST",
        body: JSON.stringify({
          kind: "custom",
          name: $("#custom-name").value.trim(),
          endpoint: $("#custom-endpoint").value.trim(),
          api_key: $("#custom-key").value.trim(),
        }),
      });
      $("#custom-key").value = "";
      feedback("Provedor conectado!");
      loadProviders();
    } catch (err) {
      feedback(err.message, true);
    }
  });

  $("#model-provider")?.addEventListener("change", loadModelsForSelectedProvider);
  $("#model-search")?.addEventListener("input", loadModelsForSelectedProvider);

  $("#save-model")?.addEventListener("click", async () => {
    const provider_id = $("#model-provider")?.value;
    const model_id = $("#model-choice")?.value;
    const stage = $("#model-stage")?.value;
    if (!provider_id || !model_id) return feedback("Selecione provedor e modelo.", true);
    try {
      await api("/api/ai/selection", {
        method: "POST",
        body: JSON.stringify({ stage, provider_id, model_id }),
      });
      feedback(`Modelo ${model_id} salvo para ${stage}!`);
      loadProviders();
      window.refreshStudioCapabilities?.();
    } catch (err) {
      feedback(err.message, true);
    }
  });

  $("#reload-social")?.addEventListener("click", loadSocialAccounts);

  window.reloadConnections = () => {
    loadSocialAccounts();
    loadProviders();
    loadComfyWorkflows();
  };

  window.reloadConnections();
})();
