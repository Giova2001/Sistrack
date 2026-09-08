const $ = (id) => document.getElementById(id);

const state = {
  cobros: [],
  saldos: [],
  pendientes: [],
  metodos: [],
  resumen: {},
  tab: "lista",
};

function flash(text, ok = true) {
  const el = $("cobFlash");
  el.textContent = text;
  el.classList.toggle("error", !ok);
  el.classList.remove("hidden");
  clearTimeout(flash._t);
  flash._t = setTimeout(() => el.classList.add("hidden"), 4200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : (typeof data === "string" ? data : res.statusText);
    throw new Error(detail);
  }
  return data;
}

function money(n) {
  return "$" + Number(n || 0).toFixed(2);
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function applyTheme() {
  const theme = localStorage.getItem("orderTrack.theme") || "light";
  document.body.classList.toggle("dark", theme === "dark");
  const use = $("themeIcon")?.querySelector("use");
  if (use) use.setAttribute("href", theme === "dark" ? "#i-moon" : "#i-sun");
}

function badge(estado, clase) {
  return `<span class="cob-badge ${clase || ""}">${esc(estado)}</span>`;
}

function fillSelect(el, items, valueKey, labelFn, emptyLabel) {
  const current = el.value;
  el.innerHTML = "";
  if (emptyLabel != null) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = emptyLabel;
    el.appendChild(opt);
  }
  items.forEach((item) => {
    const opt = document.createElement("option");
    opt.value = item[valueKey];
    opt.textContent = labelFn(item);
    el.appendChild(opt);
  });
  if ([...el.options].some((o) => o.value === current)) el.value = current;
}

function renderSummary() {
  const r = state.resumen || {};
  $("cobSummary").innerHTML = `
    <article class="inv-stat"><span>Cobros registrados</span><strong>${r.cobros_count ?? 0}</strong></article>
    <article class="inv-stat"><span>Cobrado este mes</span><strong>${money(r.cobros_mes)}</strong></article>
    <article class="inv-stat warn"><span>Saldo pendiente</span><strong>${money(r.saldo_pendiente)}</strong></article>
    <article class="inv-stat"><span>Pedidos pagados</span><strong>${r.pedidos_pagados ?? 0}</strong></article>
  `;
}

function renderLista() {
  const body = $("listaBody");
  body.innerHTML = "";
  if (!state.cobros.length) {
    body.innerHTML = `<tr><td colspan="7">No hay cobros con esos filtros.</td></tr>`;
    return;
  }
  state.cobros.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(c.ref)}</td>
      <td>${esc(c.pedido)}</td>
      <td>${esc(c.cliente)}</td>
      <td>${esc(c.fecha_cobro)}</td>
      <td>${esc(c.metodo)}</td>
      <td>${money(c.monto)}</td>
      <td>${badge(c.estado, c.clase_estado)}</td>
    `;
    body.appendChild(tr);
  });
}

function renderHistorial() {
  const body = $("historialBody");
  body.innerHTML = "";
  if (!state.cobros.length) {
    body.innerHTML = `<tr><td colspan="7">Aún no hay historial de cobros.</td></tr>`;
    return;
  }
  state.cobros.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(c.ref)}</td>
      <td>${esc(c.pedido)}</td>
      <td>${esc(c.cliente)}</td>
      <td>${esc(c.fecha_cobro)}</td>
      <td>${esc(c.metodo)}</td>
      <td>${money(c.monto)}</td>
      <td>${esc(c.observaciones || c.referencia_pago || "—")}</td>
    `;
    body.appendChild(tr);
  });
}

function renderSaldos() {
  const body = $("saldosBody");
  body.innerHTML = "";
  const rows = state.pendientes || [];
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8">No hay saldos pendientes.</td></tr>`;
    return;
  }
  rows.forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(s.codigo)}</td>
      <td>${esc(s.cliente)}</td>
      <td>${esc(s.fecha)}</td>
      <td>${money(s.total_pedido)}</td>
      <td>${money(s.total_pagado)}</td>
      <td>${money(s.saldo_pendiente)}</td>
      <td>${badge(s.estado, s.clase_estado)}</td>
      <td class="inv-row-actions"></td>
    `;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "action mini blue";
    btn.textContent = "Cobrar";
    btn.addEventListener("click", () => prefillCobro(s.pedido_key));
    tr.querySelector(".inv-row-actions").appendChild(btn);
    body.appendChild(tr);
  });
}

function renderPedidoSelect() {
  fillSelect(
    $("cobroPedido"),
    state.pendientes,
    "pedido_key",
    (s) => `${s.codigo} · ${s.cliente} · saldo ${money(s.saldo_pendiente)}`,
    "Selecciona un pedido"
  );
  fillSelect($("cobroMetodo"), state.metodos, "id", (m) => m.nombre);
  updateHint();
}

function pedidoSeleccionado() {
  const key = $("cobroPedido").value;
  return (state.pendientes || []).find((s) => s.pedido_key === key);
}

function updateHint() {
  const s = pedidoSeleccionado();
  const hint = $("cobroHint");
  if (!s) {
    hint.textContent = "Selecciona un pedido para ver el saldo pendiente.";
    return;
  }
  hint.textContent = `${s.cliente} — total ${money(s.total_pedido)}, pagado ${money(s.total_pagado)}, saldo ${money(s.saldo_pendiente)}.`;
  const monto = $("cobroMonto");
  if (!monto.value) monto.value = Number(s.saldo_pendiente).toFixed(2);
}

function prefillCobro(pedidoKey) {
  showTab("registrar");
  $("cobroPedido").value = pedidoKey;
  const s = pedidoSeleccionado();
  if (s) $("cobroMonto").value = Number(s.saldo_pendiente).toFixed(2);
  updateHint();
}

function showTab(name) {
  state.tab = name;
  document.querySelectorAll(".inv-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === name);
  });
  document.querySelectorAll(".inv-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.id !== `tab-${name}`);
  });
}

async function loadAll() {
  const q = $("filtroBuscar").value || "";
  const estado = $("filtroEstado").value || "";
  const fecha = $("filtroFecha").value || "";
  const data = await api(
    `/api/cobros?buscar=${encodeURIComponent(q)}&estado=${encodeURIComponent(estado)}&fecha=${encodeURIComponent(fecha)}`
  );
  state.cobros = data.cobros || [];
  state.saldos = data.saldos || [];
  state.pendientes = data.saldos_pendientes || [];
  state.metodos = data.metodos || [];
  state.resumen = data.resumen || {};
  renderSummary();
  renderLista();
  renderHistorial();
  renderSaldos();
  renderPedidoSelect();
}

document.querySelectorAll(".inv-tab").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});

["filtroBuscar", "filtroEstado", "filtroFecha"].forEach((id) => {
  $(id).addEventListener("input", () => loadAll().catch((err) => flash(err.message, false)));
  $(id).addEventListener("change", () => loadAll().catch((err) => flash(err.message, false)));
});

$("cobroPedido").addEventListener("change", () => {
  const s = pedidoSeleccionado();
  $("cobroMonto").value = s ? Number(s.saldo_pendiente).toFixed(2) : "";
  updateHint();
});

$("formCobro").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/cobros", {
      method: "POST",
      body: JSON.stringify({
        pedido_key: $("cobroPedido").value,
        monto: Number($("cobroMonto").value),
        id_metodo_pago: Number($("cobroMetodo").value),
        fecha_cobro: $("cobroFecha").value,
        observaciones: $("cobroObs").value,
        referencia_pago: $("cobroObs").value,
      }),
    });
    e.target.reset();
    $("cobroFecha").value = new Date().toISOString().slice(0, 10);
    flash("Cobro registrado. El saldo del pedido fue actualizado.");
    await loadAll();
    showTab("lista");
  } catch (err) {
    flash(err.message, false);
  }
});

$("themeBtn").addEventListener("click", () => {
  const next = document.body.classList.contains("dark") ? "light" : "dark";
  localStorage.setItem("orderTrack.theme", next);
  applyTheme();
});

$("refreshBtn").addEventListener("click", () => {
  loadAll().catch((err) => flash(err.message, false));
});

applyTheme();
$("cobroFecha").value = new Date().toISOString().slice(0, 10);
loadAll().catch((err) => flash(err.message, false));
