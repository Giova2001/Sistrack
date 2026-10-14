(() => {
  const API_URL = "http://127.0.0.1:8765/api/suggest";
  const PANEL_ID = "kommo-asistente-panel";
  const BTN_ID = "kommo-asistente-btn";

  function $(id) {
    return document.getElementById(id);
  }

  function uniqueTexts(nodes) {
    const seen = new Set();
    const out = [];
    for (const n of nodes) {
      const t = (n.innerText || n.textContent || "").trim();
      if (!t || t.length < 2) continue;
      if (seen.has(t)) continue;
      seen.add(t);
      out.push({ el: n, text: t });
    }
    return out;
  }

  function candidateMessageNodes() {
    const selectors = [
      "[class*='feed-note']",
      "[class*='feed__message']",
      "[class*='message-wrapper']",
      "[class*='js-feed'] [class*='note']",
      "[data-id]",
      ".feed-note__message",
      ".feed-note-wrapper",
    ];
    const nodes = [];
    for (const sel of selectors) {
      document.querySelectorAll(sel).forEach((el) => nodes.push(el));
    }
    return uniqueTexts(nodes);
  }

  function getSelectedText() {
    const sel = window.getSelection && window.getSelection();
    const t = sel ? String(sel.toString() || "").trim() : "";
    return t;
  }

  function getLatestIncomingText() {
    const selected = getSelectedText();
    if (selected && selected.length >= 2) return selected;

    const items = candidateMessageNodes();
    if (!items.length) {
      // Fallback: toma el texto del panel derecho si hay algo razonable
      const chatRoot =
        document.querySelector("[class*='card-widgets']") ||
        document.querySelector("[class*='feed-wrapper']") ||
        document.querySelector("main") ||
        document.body;
      const blob = (chatRoot.innerText || "").trim();
      if (blob.length > 20 && blob.length < 4000) {
        const lines = blob
          .split(/\n+/)
          .map((l) => l.trim())
          .filter((l) => l.length > 8);
        return lines.slice(-8).join("\n");
      }
      return "";
    }

    // Preferir mensajes cortos/medios del final (últimos del feed)
    const last = items[items.length - 1];
    return last ? last.text : "";
  }

  function ensurePanel() {
    if ($(PANEL_ID)) return $(PANEL_ID);

    const panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.innerHTML = `
      <div class="ka-head">
        <strong>Kommo Asistente</strong>
        <button type="button" class="ka-close" title="Cerrar">×</button>
      </div>
      <div class="ka-body">
        <label class="ka-label">Mensaje capturado</label>
        <textarea class="ka-input" id="ka-source" rows="4" placeholder="Selecciona un mensaje o pégalo aquí"></textarea>
        <div class="ka-actions">
          <button type="button" class="ka-btn" id="ka-capture">Capturar</button>
          <button type="button" class="ka-btn ka-primary" id="ka-analyze">Analizar</button>
        </div>
        <label class="ka-label">Sugerencia</label>
        <textarea class="ka-input" id="ka-suggestion" rows="7" readonly placeholder="Aquí aparecerá la respuesta sugerida"></textarea>
        <div class="ka-meta" id="ka-meta"></div>
        <div class="ka-actions">
          <button type="button" class="ka-btn ka-primary" id="ka-copy">Copiar sugerencia</button>
        </div>
        <div class="ka-status" id="ka-status"></div>
      </div>
    `;
    document.body.appendChild(panel);

    panel.querySelector(".ka-close").addEventListener("click", () => {
      panel.classList.remove("ka-open");
    });
    $("ka-capture").addEventListener("click", () => {
      const text = getLatestIncomingText();
      $("ka-source").value = text;
      setStatus(text ? "Mensaje capturado." : "No se encontró texto. Selecciónalo o pégalo.", !text);
    });
    $("ka-analyze").addEventListener("click", () => analyze());
    $("ka-copy").addEventListener("click", () => copySuggestion());
    return panel;
  }

  function ensureFab() {
    if ($(BTN_ID)) return;
    const btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.type = "button";
    btn.title = "Abrir Kommo Asistente";
    btn.textContent = "KA";
    btn.addEventListener("click", () => {
      const panel = ensurePanel();
      panel.classList.toggle("ka-open");
      if (panel.classList.contains("ka-open")) {
        const text = getLatestIncomingText();
        if (text) $("ka-source").value = text;
      }
    });
    document.body.appendChild(btn);
  }

  function setStatus(msg, isError) {
    const el = $("ka-status");
    if (!el) return;
    el.textContent = msg || "";
    el.classList.toggle("ka-error", !!isError);
  }

  async function analyze() {
    const source = ($("ka-source").value || "").trim();
    if (!source) {
      setStatus("Pega o captura un mensaje primero.", true);
      return;
    }
    setStatus("Analizando…");
    $("ka-suggestion").value = "";
    $("ka-meta").textContent = "";
    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: source }),
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} en ${API_URL}`);
      }
      const data = await res.json();
      $("ka-suggestion").value = data.suggestion || "";
      const found = data.found ? Object.keys(data.found).join(", ") : "";
      const missing = (data.missing || []).join(", ");
      $("ka-meta").textContent =
        `Intención: ${data.intent || "—"}` +
        (found ? ` · Encontrado: ${found}` : "") +
        (missing ? ` · Falta: ${missing}` : "");
      setStatus("Sugerencia lista. Puedes copiarla.");
    } catch (err) {
      const detail = err && err.message ? String(err.message) : "";
      setStatus(
        detail.includes("HTTP")
          ? `Error del servidor: ${detail}. Reinicia run.bat y recarga la extensión.`
          : "No se pudo conectar a http://127.0.0.1:8765/api/suggest. ¿Está corriendo run.bat?",
        true
      );
    }
  }

  async function copySuggestion() {
    const text = ($("ka-suggestion").value || "").trim();
    if (!text) {
      setStatus("No hay sugerencia para copiar.", true);
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setStatus("Copiado. Pégalo en el chat de Kommo.");
    } catch (_) {
      $("ka-suggestion").focus();
      $("ka-suggestion").select();
      setStatus("Selecciona el texto y copia con Ctrl+C.", true);
    }
  }

  function boot() {
    ensureFab();
    ensurePanel();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
