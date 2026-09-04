const $ = (id) => document.getElementById(id);

const state = {
  productos: [],
  categorias: [],
  movimientos: [],
  tipos: {},
  resumen: {},
  tab: "productos",
};

function flash(text, ok = true) {
  const el = $("invFlash");
  el.textContent = text;
  el.classList.toggle("error", !ok);
  el.classList.remove("hidden");
  clearTimeout(flash._t);
  flash._t = setTimeout(() => el.classList.add("hidden"), 4200);
}

function setModalOpen(id, open) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle("hidden", !open);
  el.hidden = !open;
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

function applyTheme() {
  const theme = localStorage.getItem("orderTrack.theme") || "light";
  document.body.classList.toggle("dark", theme === "dark");
  const use = $("themeIcon")?.querySelector("use");
  if (use) use.setAttribute("href", theme === "dark" ? "#i-moon" : "#i-sun");
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

function activos() {
  return state.productos.filter((p) => p.activo !== false);
}

function renderSummary() {
  const r = state.resumen || {};
  $("invSummary").innerHTML = `
    <article class="inv-stat"><span>Productos activos</span><strong>${r.productos_activos ?? 0}</strong></article>
    <article class="inv-stat"><span>Stock total</span><strong>${r.stock_total ?? 0}</strong></article>
    <article class="inv-stat warn"><span>Bajo mínimo</span><strong>${r.bajo_minimo ?? 0}</strong></article>
    <article class="inv-stat"><span>Categorías</span><strong>${r.categorias ?? 0}</strong></article>
  `;
}

function renderFilters() {
  fillSelect($("filtroCategoria"), state.categorias, "id", (c) => c.nombre, "Todas las categorías");
  fillSelect(
    $("filtroTipo"),
    Object.entries(state.tipos || {}).map(([k, v]) => ({ id: k, nombre: v })),
    "id",
    (t) => t.nombre,
    "Todos los tipos"
  );
  const prodOpts = activos().map((p) => ({
    id: p.id,
    nombre: `${p.nombre} (disp. ${p.stock_disponible})`,
  }));
  fillSelect($("entradaProducto"), prodOpts, "id", (p) => p.nombre, "Selecciona producto");
  fillSelect($("salidaProducto"), prodOpts, "id", (p) => p.nombre, "Selecciona producto");
  fillSelect($("ajusteProducto"), prodOpts, "id", (p) => p.nombre, "Selecciona producto");
  fillSelect($("kardexProducto"), state.productos, "id", (p) => p.nombre, "Selecciona un producto");
  fillSelect($("prodCategoria"), state.categorias, "id", (c) => c.nombre);
  fillSelect(
    $("prodTipo"),
    Object.entries(state.tipos || {}).map(([k, v]) => ({ id: k, nombre: v })),
    "id",
    (t) => t.nombre
  );
}

function renderProductos() {
  const body = $("productosBody");
  body.innerHTML = "";
  state.productos.forEach((p) => {
    const tr = document.createElement("tr");
    if (p.bajo_minimo) tr.classList.add("low");
    if (!p.activo) tr.classList.add("inactive");
    tr.innerHTML = `
      <td>${p.id}</td>
      <td>${esc(p.nombre)}</td>
      <td>${esc(p.categoria_nombre || "—")}</td>
      <td>${esc(p.tipo_inventario_label || p.tipo_inventario)}</td>
      <td>${money(p.precio_unitario)}</td>
      <td>${p.stock_actual}</td>
      <td>${p.stock_reservado}</td>
      <td>${p.stock_disponible}</td>
      <td>${p.stock_minimo}</td>
      <td>${p.activo ? "Activo" : "Inactivo"}</td>
      <td class="inv-row-actions"></td>
    `;
    const actions = tr.querySelector(".inv-row-actions");
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "action gray mini";
    edit.textContent = "Editar";
    edit.addEventListener("click", () => openProducto(p));
    const kardex = document.createElement("button");
    kardex.type = "button";
    kardex.className = "action blue mini";
    kardex.textContent = "Kardex";
    kardex.addEventListener("click", () => {
      $("kardexProducto").value = String(p.id);
      showTab("kardex");
      loadKardex(p.id);
    });
    const del = document.createElement("button");
    del.type = "button";
    del.className = "action red mini";
    del.textContent = "Eliminar";
    del.addEventListener("click", () => destroyProducto(p.id));
    actions.append(edit, kardex, del);
    body.appendChild(tr);
  });
}

function renderMovimientos(tipo, tbodyId) {
  const tipos = Array.isArray(tipo) ? tipo : [tipo];
  const rows = (state.movimientos || []).filter((m) => tipos.includes(m.tipo_movimiento));
  const body = $(tbodyId);
  body.innerHTML = rows
    .slice(0, 20)
    .map(
      (m) => `<tr>
        <td>${esc(m.fecha_movimiento || "")}</td>
        <td>${esc(m.producto_nombre || "")}</td>
        <td>${m.cantidad}</td>
        <td>${m.stock_nuevo}</td>
        <td>${esc(m.motivo || "—")}</td>
      </tr>`
    )
    .join("");
}

function renderCategorias() {
  const body = $("categoriasBody");
  body.innerHTML = "";
  state.categorias.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(c.nombre)}</td>
      <td>${esc(c.descripcion || "—")}</td>
      <td>${c.productos_count ?? 0}</td>
      <td class="inv-row-actions"></td>
    `;
    const actions = tr.querySelector(".inv-row-actions");
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "action gray mini";
    edit.textContent = "Editar";
    edit.addEventListener("click", () => {
      $("categoriaId").value = c.id;
      $("categoriaNombre").value = c.nombre;
      $("categoriaDesc").value = c.descripcion || "";
    });
    const del = document.createElement("button");
    del.type = "button";
    del.className = "action red mini";
    del.textContent = "Eliminar";
    del.addEventListener("click", () => destroyCategoria(c.id));
    actions.append(edit, del);
    body.appendChild(tr);
  });
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
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
  const data = await api(
    `/api/inventario/productos?buscar=${encodeURIComponent($("filtroBuscar").value || "")}` +
      `&categoria=${encodeURIComponent($("filtroCategoria").value || "")}` +
      `&tipo=${encodeURIComponent($("filtroTipo").value || "")}` +
      `&estado=${encodeURIComponent($("filtroEstado").value || "")}` +
      `&stock=${encodeURIComponent($("filtroStock").value || "")}`
  );
  state.productos = data.productos || [];
  state.categorias = data.categorias || [];
  state.movimientos = data.movimientos || [];
  state.tipos = data.tipos_inventario || {};
  state.resumen = data.resumen || {};
  renderSummary();
  renderFilters();
  renderProductos();
  renderMovimientos("Entrada", "entradasBody");
  renderMovimientos(["Salida", "Consumo_Produccion"], "salidasBody");
  renderCategorias();
}

function openProducto(p) {
  $("productoModalTitle").textContent = p ? "Editar producto" : "Nuevo producto";
  $("prodId").value = p ? p.id : "";
  $("prodNombre").value = p ? p.nombre : "";
  $("prodCategoria").value = p ? p.id_categoria : (state.categorias[0]?.id || "");
  $("prodTipo").value = p ? p.tipo_inventario : "producto_base";
  $("prodDesc").value = p ? p.descripcion || "" : "";
  $("prodPrecio").value = p ? p.precio_unitario : 0;
  $("prodPrecioPaq").value = p ? p.precio_paquete || "" : "";
  $("prodUnidades").value = p ? p.unidades_por_paquete || "" : "";
  $("prodStock").value = p ? p.stock_actual : 0;
  $("prodMinimo").value = p ? p.stock_minimo : 0;
  $("prodActivo").checked = p ? !!p.activo : true;
  $("prodActivoWrap").classList.toggle("hidden", !p);
  setModalOpen("productoModal", true);
}

async function destroyProducto(id) {
  if (!confirm("¿Eliminar o inactivar este producto?")) return;
  try {
    const res = await api(`/api/inventario/productos/${id}`, { method: "DELETE" });
    flash(res.message || "Listo");
    await loadAll();
  } catch (err) {
    flash(err.message, false);
  }
}

async function destroyCategoria(id) {
  if (!confirm("¿Eliminar esta categoría?")) return;
  try {
    const res = await api(`/api/inventario/categorias/${id}`, { method: "DELETE" });
    flash(res.message || "Categoría eliminada");
    await loadAll();
  } catch (err) {
    flash(err.message, false);
  }
}

async function loadKardex(id) {
  const box = $("kardexDetalle");
  if (!id) {
    box.innerHTML = "<p class='muted'>Selecciona un producto para ver su kardex.</p>";
    return;
  }
  try {
    const data = await api(`/api/inventario/productos/${id}/kardex`);
    const p = data.producto;
    const rows = (data.movimientos || [])
      .map(
        (m) => `<tr>
          <td>${esc(m.fecha_movimiento)}</td>
          <td>${esc(m.tipo_movimiento)}</td>
          <td>${m.cantidad}</td>
          <td>${m.stock_anterior}</td>
          <td>${m.stock_nuevo}</td>
          <td>${esc(m.referencia || "—")}</td>
          <td>${esc(m.motivo || "—")}</td>
        </tr>`
      )
      .join("");
    box.innerHTML = `
      <h2>${esc(p.nombre)}</h2>
      <p class="muted">Disponible ${p.stock_disponible} · Actual ${p.stock_actual} · Reservado ${p.stock_reservado} · Mínimo ${p.stock_minimo}</p>
      <div class="inv-table-wrap">
        <table class="inv-table">
          <thead><tr><th>Fecha</th><th>Tipo</th><th>Cant.</th><th>Anterior</th><th>Nuevo</th><th>Ref.</th><th>Motivo</th></tr></thead>
          <tbody>${rows || "<tr><td colspan='7'>Sin movimientos</td></tr>"}</tbody>
        </table>
      </div>
    `;
  } catch (err) {
    box.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

$("themeBtn").addEventListener("click", () => {
  const next = document.body.classList.contains("dark") ? "light" : "dark";
  localStorage.setItem("orderTrack.theme", next);
  applyTheme();
});
$("refreshBtn").addEventListener("click", () => loadAll().catch((e) => flash(e.message, false)));
document.querySelectorAll(".inv-tab").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});
["filtroBuscar", "filtroCategoria", "filtroTipo", "filtroEstado", "filtroStock"].forEach((id) => {
  $(id).addEventListener("change", () => loadAll().catch((e) => flash(e.message, false)));
  $(id).addEventListener("input", () => {
    clearTimeout($(id)._t);
    $(id)._t = setTimeout(() => loadAll().catch((e) => flash(e.message, false)), 250);
  });
});
$("nuevoProductoBtn").addEventListener("click", () => openProducto(null));
$("productoCancel").addEventListener("click", () => setModalOpen("productoModal", false));
$("productoModal").addEventListener("click", (e) => {
  if (e.target === $("productoModal")) setModalOpen("productoModal", false);
});

$("formProducto").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("prodId").value;
  const payload = {
    nombre: $("prodNombre").value,
    id_categoria: Number($("prodCategoria").value),
    tipo_inventario: $("prodTipo").value,
    descripcion: $("prodDesc").value,
    precio_unitario: Number($("prodPrecio").value || 0),
    precio_paquete: Number($("prodPrecioPaq").value || 0),
    unidades_por_paquete: Number($("prodUnidades").value || 0),
    stock_actual: Number($("prodStock").value || 0),
    stock_minimo: Number($("prodMinimo").value || 0),
    activo: $("prodActivo").checked,
  };
  try {
    await api(id ? `/api/inventario/productos/${id}` : "/api/inventario/productos", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    setModalOpen("productoModal", false);
    flash(id ? "Producto actualizado." : "Producto creado.");
    await loadAll();
  } catch (err) {
    flash(err.message, false);
  }
});

$("formEntrada").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/inventario/entradas", {
      method: "POST",
      body: JSON.stringify({
        id_producto: Number($("entradaProducto").value),
        cantidad: Number($("entradaCantidad").value),
        costo_unitario: $("entradaCosto").value === "" ? null : Number($("entradaCosto").value),
        referencia: $("entradaRef").value,
        motivo: $("entradaMotivo").value,
      }),
    });
    e.target.reset();
    flash("Entrada registrada. Stock actualizado.");
    await loadAll();
  } catch (err) {
    flash(err.message, false);
  }
});

$("formSalida").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/inventario/salidas", {
      method: "POST",
      body: JSON.stringify({
        id_producto: Number($("salidaProducto").value),
        cantidad: Number($("salidaCantidad").value),
        motivo: $("salidaMotivo").value,
      }),
    });
    e.target.reset();
    flash("Salida registrada. Stock descontado.");
    await loadAll();
  } catch (err) {
    flash(err.message, false);
  }
});

$("formAjuste").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/inventario/ajustes", {
      method: "POST",
      body: JSON.stringify({
        id_producto: Number($("ajusteProducto").value),
        stock_nuevo: Number($("ajusteStock").value),
        motivo: $("ajusteMotivo").value,
      }),
    });
    e.target.reset();
    flash("Ajuste aplicado. El stock del sistema fue actualizado.");
    await loadAll();
  } catch (err) {
    flash(err.message, false);
  }
});

$("formCategoria").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("categoriaId").value;
  const payload = { nombre: $("categoriaNombre").value, descripcion: $("categoriaDesc").value };
  try {
    await api(id ? `/api/inventario/categorias/${id}` : "/api/inventario/categorias", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    $("categoriaId").value = "";
    e.target.reset();
    flash("Categoría guardada.");
    await loadAll();
  } catch (err) {
    flash(err.message, false);
  }
});
$("categoriaCancel").addEventListener("click", () => {
  $("categoriaId").value = "";
  $("formCategoria").reset();
});
$("kardexProducto").addEventListener("change", (e) => loadKardex(e.target.value));

applyTheme();
loadAll().catch((e) => flash(e.message, false));
