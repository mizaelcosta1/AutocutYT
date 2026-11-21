/* AutoClips Suggestions: public-source trends, thumbnail mockups and opportunities.
   This UI never treats public popularity as permission to reuse a stream. */
(() => {
  "use strict";
  const root = document.getElementById("suggestions");
  if (!root) return;
  const $ = selector => root.querySelector(selector);
  const formatNumber = number => (Number(number) || 0).toLocaleString("pt-BR");
  const state = {period: "daily", platform: "all", data: null, sequence: 0, currentPreview: null};
  const dialog = $("#suggestion-thumbnail-dialog");
  const status = $("#suggestions-status");

  function child(tag, className, content) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (content !== undefined) el.textContent = String(content ?? "");
    return el;
  }
  function safeLink(url) {
    try {
      const u = new URL(url);
      if (u.protocol !== "https:") return "";
      if (u.hostname === "www.youtube.com" && u.pathname === "/watch" &&
          /^[\w-]{11}$/.test(u.searchParams.get("v") || "")) return u.href;
      if (u.hostname === "www.twitch.tv" && /^\/[\w]{2,25}\/?$/.test(u.pathname)) return u.href;
      return "";
    } catch (_) { return ""; }
  }
  function safeImage(url) {
    try {
      const u = new URL(url);
      if (u.protocol !== "https:") return "";
      if (["i.ytimg.com", "static-cdn.jtvnw.net"].includes(u.hostname)) return u.href;
      return "";
    } catch (_) { return ""; }
  }
  function metric(row) {
    const amount = formatNumber(row.metric);
    const weekly = state.period === "weekly";
    if (row.metric_unit === "viewers_live")
      return weekly ? amount + " espectadores (média das amostras)" : amount + " espectadores ao vivo (amostra)";
    if (row.metric_unit === "sampled_live_viewers")
      return weekly ? amount + " espectadores somados (média)" : amount + " espectadores somados nas lives amostradas";
    if (row.metric_unit === "views_total")
      return amount + " visualizações acumuladas" + (row.chart_position ? " · ranking #" + row.chart_position : "");
    return amount + " registros";
  }
  function sourceName(id) { return id === "twitch" ? "Twitch" : "YouTube"; }
  function platformMatches(row) { return state.platform === "all" || row.platform === state.platform; }

  function showSources(sources) {
    const holder = $("#suggestions-sources");
    holder.replaceChildren();
    const title = child("h3", "", "Fontes e qualidade dos dados");
    holder.append(title);
    for (const s of (sources || [])) {
      const line = child("div", "tr-source");
      const indicator = child("span", "tr-source-indicator " + (s.status === "ready" ? "ready" : "pending"),
                              s.status === "ready" ? "Disponível" : s.status === "not_configured" ? "Não conectada" : "Indisponível");
      line.append(child("b", "", s.label || sourceName(s.id)), indicator);
      line.append(child("p", "hint", s.message || ""));
      holder.append(line);
    }
  }

  function copyIdea(row, kind) {
    const title = kind === "topic" ? row.name : row.headline || row.name;
    const source = sourceName(row.platform);
    const idea = [
      "PAUTA PARA VÍDEO AUTORAL · AUTOCLIPS",
      "Referência (" + source + "): " + title,
      row.topic ? "Tema: " + row.topic : "",
      "Sinal observado: " + metric(row),
      row.url ? "Fonte: " + row.url : "",
      "Abordagem: crie uma análise, reação autorizada ou comentário original; não reutilize vídeo sem licença."
    ].filter(Boolean).join("\n");
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(idea).then(() => {
        status.textContent = "Pauta copiada. Crie conteúdo original ou use material autorizado.";
      }).catch(() => {
        status.textContent = "Não foi possível copiar neste navegador. Selecione o texto na prévia.";
      });
    } else {
      status.textContent = "Cópia automática não disponível neste navegador.";
    }
  }

  function openPreview(row) {
    const src = safeImage(row.thumbnail_url || "");
    if (!src) {
      status.textContent = "Esta referência não disponibilizou uma imagem de capa para prévia.";
      return;
    }
    state.currentPreview = row;
    const image = $("#suggestions-preview-image");
    image.src = src;
    image.alt = "Imagem pública de referência de " + sourceName(row.platform);
    $("#suggestions-preview-platform").textContent = sourceName(row.platform);
    $("#suggestions-preview-caption").textContent = (row.headline || row.name || "") + " — " + metric(row);
    const suggested = (row.topic && row.topic !== "Games" ? row.topic : row.headline || row.name || "").slice(0, 62);
    const title = $("#suggestions-cover-text");
    title.value = suggested;
    $("#suggestions-cover-overlay").textContent = suggested;
    const link = $("#suggestions-preview-source");
    const href = safeLink(row.url || "");
    link.href = href || "#suggestions";
    link.hidden = !href;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function makeCard(row, kind) {
    const card = child("article", "panel tr-card");
    const thumbnail = safeImage(row.thumbnail_url || "");
    if (thumbnail) {
      const preview = child("button", "tr-card-thumb");
      preview.type = "button";
      preview.setAttribute("aria-label", "Ampliar a thumbnail de " + (row.headline || row.name));
      const img = child("img");
      img.src = thumbnail;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      preview.append(img, child("span", "tr-preview-label", "Ver prévia da thumbnail"));
      preview.addEventListener("click", () => openPreview(row));
      card.append(preview);
    }
    const inner = child("div", "tr-card-content");
    const meta = child("div", "tr-meta");
    meta.append(child("span", "tr-platform", sourceName(row.platform)));
    if (state.period === "weekly") {
      meta.append(child("span", "tr-coverage", formatNumber(row.sample_days || 1) + " dia(s) de observação"));
    } else if (row.chart_position) {
      meta.append(child("span", "tr-coverage", "Popular #" + row.chart_position));
    }
    inner.append(meta);
    inner.append(child("h3", "tr-card-title", kind === "topic" ? row.name : row.name || "Streamer"));
    if (kind !== "topic" && row.headline) inner.append(child("p", "tr-card-subtitle", row.headline));
    if (kind !== "topic" && row.topic && row.topic !== "Games") inner.append(child("p", "tr-topic-line", "Tema: " + row.topic));
    inner.append(child("div", "tr-metric", metric(row)));
    const buttons = child("div", "tr-actions");
    const copy = child("button", "outline", "Copiar pauta");
    copy.type = "button";
    copy.addEventListener("click", () => copyIdea(row, kind));
    buttons.append(copy);
    const href = safeLink(row.url || "");
    if (href) {
      const a = child("a", "tr-external", "Abrir origem ↗");
      a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
      buttons.append(a);
    }
    inner.append(buttons);
    card.append(inner);
    return card;
  }

  function renderGroup(holder, rows, kind) {
    holder.replaceChildren();
    const visible = (rows || []).filter(platformMatches).slice(0, 12);
    if (!visible.length) {
      const text = state.platform !== "all" ? "Não há dados desta plataforma neste período." :
        kind === "topic" ? "Os temas dependem das categorias observadas nas lives da Twitch." :
        "Ainda não há amostras para mostrar. Confira as conexões abaixo.";
      holder.append(child("div", "tr-empty", text));
      return;
    }
    for (const row of visible) holder.append(makeCard(row, kind));
  }

  function render() {
    const data = state.data;
    if (!data) return;
    const week = state.period === "weekly";
    const coverage = Number(data.coverage_days || 0);
    $("#suggestions-coverage").textContent = week
      ? "Histórico: " + coverage + " de 7 dias observados" : data.status === "ready" ? "Retrato de agora" : "Aguardando dados";
    $("#suggestions-updated").textContent = data.updated_at
      ? "Atualizado em " + new Date(data.updated_at).toLocaleString("pt-BR", {dateStyle: "short", timeStyle: "short"}) : "";
    $("#suggestions-method").textContent = data.methodology || "";
    $("#suggestions-week-note").hidden = !week || !data.partial;
    $("#suggestions-week-note").textContent = week && data.partial
      ? "Coleta inicial: a semana tem " + coverage + " dia(s) medidos. Não é um ranking histórico de 7 dias completos."
      : "";
    renderGroup($("#suggestions-topics"), data.topics, "topic");
    renderGroup($("#suggestions-streamers"), data.creators, "creator");
    renderGroup($("#suggestions-videos"), data.videos, "video");
    showSources(data.sources);
    status.textContent = data.status === "no_data"
      ? "Sem dados de tendências: conecte o YouTube ou configure a API da Twitch no Railway."
      : "Resultados públicos carregados. Popularidade não é permissão de usar o vídeo de outra pessoa.";
  }

  async function load(refresh = false) {
    const seq = ++state.sequence;
    const btn = $("#suggestions-refresh");
    btn.disabled = true;
    status.textContent = "Consultando dados públicos das plataformas...";
    try {
      const url = "/api/suggestions/trending?period=" + encodeURIComponent(state.period) +
        "&region=BR" + (refresh ? "&refresh=true" : "");
      const resp = await fetch(url, {credentials: "same-origin", headers: {"Accept": "application/json"}, cache: "no-store"});
      if (!resp.ok) throw new Error(resp.status === 401 ? "Entre novamente no estúdio." : "A consulta falhou (HTTP " + resp.status + ").");
      const data = await resp.json();
      if (seq !== state.sequence) return;
      state.data = data;
      render();
    } catch (error) {
      if (seq === state.sequence) {
        state.data = null;
        status.textContent = error.message || "Ocorreu um erro ao buscar tendências.";
      }
    } finally {
      if (seq === state.sequence) btn.disabled = false;
    }
  }

  root.querySelectorAll("[data-trend-period]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.period = btn.dataset.trendPeriod;
      root.querySelectorAll("[data-trend-period]").forEach(other => {
        const pressed = other === btn;
        other.classList.toggle("active", pressed);
        other.setAttribute("aria-pressed", String(pressed));
      });
      load();
    });
  });
  $("#suggestions-platform").addEventListener("change", event => {
    state.platform = event.target.value;
    render();
  });
  $("#suggestions-refresh").addEventListener("click", () => load(true));
  $("#suggestions-cover-text").addEventListener("input", event => {
    $("#suggestions-cover-overlay").textContent = event.target.value.trim().slice(0, 62);
  });
  $("#suggestions-preview-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => {
    if (event.target === dialog) dialog.close();
  });
  $("#suggestions-copy-title").addEventListener("click", () => {
    const value = $("#suggestions-cover-text").value.trim();
    if (!value) return;
    if (!navigator.clipboard?.writeText) {
      $("#suggestions-preview-caption").textContent = "Cópia automática indisponível neste navegador.";
      return;
    }
    navigator.clipboard.writeText(value).then(() => {
      $("#suggestions-preview-caption").textContent = "Texto de capa copiado. Esta é apenas uma simulação visual.";
    }).catch(() => {});
  });
  root.querySelectorAll("[data-trends-open-connections]").forEach(btn => {
    btn.addEventListener("click", () => document.querySelector('.sidebar [data-tab="connections"]')?.click());
  });
  document.querySelectorAll('[data-tab="suggestions"]').forEach(btn => {
    btn.addEventListener("click", () => {
      if (!state.data) void load();
    });
  });
  if (location.hash === "#suggestions") {
    document.querySelector('[data-tab="suggestions"]')?.click();
  }
})();
