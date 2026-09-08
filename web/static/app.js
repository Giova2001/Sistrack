const state = {
  fecha: null,
  records: [],
  fields: [],
  fieldsDraft: null,
  editingFieldIndex: null,
  view: "lista",
  region: "dept",
  editing: false,
  uploadPoll: null,
  sistrackEmail: "",
  sistrackPassword: "",
  sistrackPasswordSet: false,
  uploadPlatform: "sistrack",
  forzaCodigo: "",
  forzaUsuario: "",
  forzaPasswordSet: false,
  uploadHeadless: false,
  uploadDryRun: false,
  defaultEntrega: "",
  locations: null,
  previewRecords: null,
  saveTimer: null,
  loadSeq: 0,
  switchingZona: false,
};

const $ = (id) => document.getElementById(id);

function setModalOpen(id, open) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle("hidden", !open);
  el.hidden = !open;
}

function iconSvg(id, className = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${id}`);
  svg.appendChild(use);
  return svg;
}

function setThemeIcon(theme) {
  const use = $("themeIcon")?.querySelector("use");
  if (use) use.setAttribute("href", theme === "dark" ? "#i-moon" : "#i-sun");
}

function enabledFields() {
  return (state.fields || []).filter((f) => f.enabled !== false);
}

function addChat(role, text) {
  const log = $("chatLog");
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function clearChatHistory() {
  const log = $("chatLog");
  if (!log || !log.children.length) return;
  if (!confirm("¿Limpiar todo el historial del chat?")) return;
  log.innerHTML = "";
  addChat("bot", "Historial limpio. Pega un pedido cuando quieras.");
}

function setChatCollapsed(collapsed) {
  const app = $("appShell");
  const rail = $("chatRailBtn");
  if (!app) return;
  app.classList.toggle("chat-collapsed", collapsed);
  if (rail) rail.hidden = !collapsed;
  try {
    localStorage.setItem("orderTrack.chatCollapsed", collapsed ? "1" : "0");
  } catch (_) {}
}

function loadChatCollapsed() {
  try {
    return localStorage.getItem("orderTrack.chatCollapsed") === "1";
  } catch (_) {
    return false;
  }
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t || res.statusText);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

function setEditing(on) {
  state.editing = on;
  document.body.classList.toggle("editing", on);
  $("editBtnLabel").textContent = on ? "Listo" : "Editar";
  const use = $("editIcon")?.querySelector("use");
  if (use) use.setAttribute("href", on ? "#i-check" : "#i-pencil");
}

function collectFromDom() {
  // values already bound live into state.records via oninput
  return state.records;
}

function bindValue(el, rec, key) {
  el.value = rec[key] ?? "";
  el.addEventListener("input", () => {
    rec[key] = el.value;
    schedulePersist();
  });
}

const DEFAULT_OBS = "Contactar al cliente para coordinar la entrega";

function isYes(val) {
  const v = String(val ?? "")
    .trim()
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");
  return ["si", "true", "1", "yes"].includes(v);
}

function toDateInputValue(v) {
  if (!v) return "";
  const s = String(v).trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  let m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (m) return `${m[3]}-${m[2].padStart(2, "0")}-${m[1].padStart(2, "0")}`;
  m = s.match(/^(\d{1,2})-(\d{1,2})-(\d{4})$/);
  if (m) return `${m[3]}-${m[2].padStart(2, "0")}-${m[1].padStart(2, "0")}`;
  return s;
}

function normalizeObs(val) {
  let obs = String(val ?? "")
    .replace(/^\\n+/, "")
    .replace(/^\n+/, "")
    .trim();
  if (!obs) return DEFAULT_OBS;
  // Fragmentos incompletos del chat ("Contactar", "\nContactar", etc.)
  if (/^contactar\b/i.test(obs) && obs.length < 55) return DEFAULT_OBS;
  return obs;
}

function normalizeRecord(rec) {
  if (!rec || typeof rec !== "object") return rec;
  rec.observaciones = normalizeObs(rec.observaciones);
  rec.grabado = isYes(rec.grabado) ? "Si" : "No";
  rec.pagado = isYes(rec.pagado) ? "Si" : "No";
  if (isYes(rec.pagado)) rec.precio = "0";
  rec.fecha_entrega = toDateInputValue(rec.fecha_entrega) || state.defaultEntrega || state.fecha || "";
  return rec;
}

function normalizeRecords(list) {
  return (list || []).map((r) => normalizeRecord(r));
}

function normLoc(s) {
  return String(s || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim();
}

function phoneKey(raw) {
  let d = String(raw || "").replace(/\D/g, "");
  if (d.startsWith("503") && d.length >= 11) d = d.slice(3);
  return d.length >= 8 ? d.slice(-8) : d;
}

function nameKey(raw) {
  return normLoc(String(raw || "").replace(/^\s*\d{1,2}[.)]\s*/, ""))
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Marca avisos de pedidos repetidos (telefono o nombre) en el dia. */
function applyDuplicateWarnings(records) {
  const list = records || [];
  const byPhone = new Map();
  const byName = new Map();
  list.forEach((r, i) => {
    const ph = phoneKey(r.telefono);
    const nm = nameKey(r.nombre);
    if (ph.length >= 8) {
      if (!byPhone.has(ph)) byPhone.set(ph, []);
      byPhone.get(ph).push(i);
    }
    if (nm.length >= 3) {
      if (!byName.has(nm)) byName.set(nm, []);
      byName.get(nm).push(i);
    }
  });

  list.forEach((r, i) => {
    const warnings = (r.warnings || []).filter((w) => !/pedido repetido/i.test(String(w)));
    const reasons = [];
    const ph = phoneKey(r.telefono);
    const nm = nameKey(r.nombre);
    if (ph.length >= 8 && (byPhone.get(ph) || []).length > 1) reasons.push("telefono");
    if (nm.length >= 3 && (byName.get(nm) || []).length > 1) reasons.push("nombre");
    if (reasons.length) {
      warnings.push(`Pedido repetido (${reasons.join(" y ")})`);
      r.duplicate = true;
    } else {
      r.duplicate = false;
    }
    r.warnings = warnings;
    r.incomplete = warnings.length > 0;
    r.location_uncertain = warnings.some((w) => /ubicacion/i.test(String(w)));
  });
  return list;
}

function findDepartmentKey(name) {
  const locs = state.locations;
  if (!locs || !name) return "";
  const n = normLoc(name);
  const hit = (locs.departments || []).find((d) => normLoc(d) === n);
  if (hit) return hit;
  // Alias Sistrack (ej. Chaletenango)
  const aliases = locs.state_aliases || {};
  for (const [dept, alias] of Object.entries(aliases)) {
    if (normLoc(alias) === n) return dept;
  }
  return name;
}

function currentZona() {
  return state.region === "ss" ? "ss" : "dept";
}

function zonaLabel() {
  return currentZona() === "ss" ? "San Salvador" : "Departamentales";
}

function isUploadRunning() {
  return !!$("stopUploadBtn")?.classList.contains("active");
}

function syncUploadButton(running = isUploadRunning()) {
  const btn = $("uploadBtn");
  if (!btn) return;
  const ss = currentZona() === "ss";
  btn.disabled = !!running || ss;
  btn.title = ss
    ? "Deshabilitado: San Salvador no se sube al sistema"
    : running
      ? "Subida en curso"
      : "Subir datos al sistema";
}

function emptyMessage() {
  const kind = currentZona() === "ss" ? "de San Salvador" : "departamentales";
  return `Sin registros ${kind} para esta fecha.<br/>Pega pedidos en el chat.`;
}

function loadRegion() {
  try {
    const v = localStorage.getItem("orderTrack.zona");
    if (v === "ss" || v === "dept") return v;
  } catch (_) {}
  return "dept";
}

function syncRegionSelect() {
  const sel = $("regionSelect");
  if (sel) sel.value = currentZona();
}

async function setRegion(region) {
  const next = region === "ss" ? "ss" : "dept";
  if (next === state.region) {
    syncRegionSelect();
    return;
  }
  if (state.switchingZona) return;
  state.switchingZona = true;

  if (state.saveTimer) {
    clearTimeout(state.saveTimer);
    state.saveTimer = null;
  }

  const prevZona = currentZona();
  const prevFecha = state.fecha;
  const prevRecords = state.records;

  state.region = next;
  try {
    localStorage.setItem("orderTrack.zona", state.region);
  } catch (_) {}
  syncRegionSelect();

  const panel = $("recordsPanel");
  if (panel) panel.innerHTML = `<div class="empty">Cargando ${zonaLabel()}…</div>`;

  try {
    if (prevFecha) {
      try {
        await persist({ fecha: prevFecha, zona: prevZona, records: prevRecords });
      } catch (_) {}
      await loadDay(prevFecha);
    }
    const file = next === "ss" ? `${state.fecha}_SS` : state.fecha;
    addChat(
      "bot",
      `Tabla ${zonaLabel()}: ${state.records.length} pedido(s). Se guarda como ${file}.`
    );
  } catch (err) {
    addChat("bot", "No se pudo cambiar de tabla: " + err.message);
  } finally {
    state.switchingZona = false;
    syncRegionSelect();
  }
}

function municipiosForDept(deptName) {
  const locs = state.locations;
  if (!locs) return [];
  const key = findDepartmentKey(deptName);
  return locs.by_department[key] || locs.by_department[deptName] || [];
}

/** Recalcula avisos de un registro con la misma lógica del parser. */
function computeRecordWarnings(rec) {
  const warnings = [];
  const phone = phoneKey(rec.telefono);
  if (!phone || phone.length < 8) warnings.push("Telefono incompleto");
  const dir = String(rec.direccion || "").trim();
  if (!dir || dir.length < 5) warnings.push("Direccion incompleta");
  const dept = String(rec.departamento || "").trim();
  const muni = String(rec.municipio || "").trim();
  if (!muni || (dept && normLoc(muni) === normLoc(dept))) {
    warnings.push("Ubicacion dudosa");
  }
  const producto = String(rec.producto || "").trim();
  if (!producto || producto === "Producto") warnings.push("Producto generico o vacio");
  // Conservar aviso de duplicado si ya estaba
  for (const w of rec.warnings || []) {
    if (/pedido repetido/i.test(String(w)) && !warnings.includes(w)) warnings.push(w);
  }
  rec.warnings = warnings;
  rec.incomplete = warnings.length > 0;
  rec.location_uncertain = warnings.includes("Ubicacion dudosa");
  return warnings;
}

function revalidateAllRecords() {
  state.records.forEach(computeRecordWarnings);
  applyDuplicateWarnings(state.records);
}

async function refreshAndRevalidate() {
  const btn = $("refreshBtn");
  if (btn) {
    btn.disabled = true;
    btn.classList.add("spinning");
  }
  try {
    try {
      state.locations = await api("/api/ubicaciones");
    } catch (_) {
      /* mantener ubicaciones actuales */
    }
    revalidateAllRecords();
    render();
    await persist();
    const warnN = state.records.filter((r) => r.incomplete).length;
    addChat(
      "bot",
      warnN
        ? `Datos recalculados: ${warnN} registro(s) con avisos.`
        : "Datos recalculados: todo en orden."
    );
  } catch (err) {
    addChat("bot", "No se pudo actualizar: " + (err.message || err));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("spinning");
    }
  }
}

function fillSelect(select, options, current, placeholder) {
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = placeholder || "Seleccionar…";
  select.appendChild(empty);
  const cur = String(current || "").trim();
  const curN = normLoc(cur);
  let matched = false;
  options.forEach((opt) => {
    const o = document.createElement("option");
    o.value = opt;
    o.textContent = opt;
    if (cur && (opt === cur || normLoc(opt) === curN)) {
      o.selected = true;
      matched = true;
    }
    select.appendChild(o);
  });
  if (cur && !matched) {
    const o = document.createElement("option");
    o.value = cur;
    o.textContent = cur + " (actual)";
    o.selected = true;
    select.appendChild(o);
  }
  return select;
}

function deptSelectFor(rec) {
  const select = document.createElement("select");
  select.className = "loc-select";
  const depts = state.locations?.departments || [];
  const current = findDepartmentKey(rec.departamento) || rec.departamento || "";
  fillSelect(select, depts, current, "Departamento…");
  select.addEventListener("change", () => {
    rec.departamento = select.value;
    const munis = municipiosForDept(select.value);
    if (!munis.includes(String(rec.municipio || "").trim())) {
      const curN = normLoc(rec.municipio);
      const stillOk = munis.find((m) => normLoc(m) === curN);
      rec.municipio = stillOk || munis[0] || "";
    }
    rec.location_uncertain = false;
    if (rec.warnings) {
      rec.warnings = rec.warnings.filter((w) => !/ubicacion/i.test(w));
      rec.incomplete = rec.warnings.length > 0;
    }
    schedulePersist();
    render();
  });
  return select;
}

function muniSelectFor(rec) {
  const select = document.createElement("select");
  select.className = "loc-select";
  const munis = municipiosForDept(rec.departamento);
  fillSelect(select, munis, rec.municipio || "", "Municipio…");
  select.addEventListener("change", () => {
    rec.municipio = select.value;
    rec.location_uncertain = false;
    if (rec.warnings) {
      rec.warnings = rec.warnings.filter((w) => !/ubicacion/i.test(w));
      rec.incomplete = rec.warnings.length > 0;
    }
    schedulePersist();
    render();
  });
  return select;
}

const UPLOAD_STATUS_OPTIONS = [
  { value: "pending", label: "Pendiente" },
  { value: "success", label: "Subido" },
  { value: "error", label: "Error" },
];

function statusSelectFor(rec) {
  const select = document.createElement("select");
  select.className = "status-select";
  select.title = "Estado de subida";
  const cur = rec.upload_status || "pending";
  UPLOAD_STATUS_OPTIONS.forEach((o) => {
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    if (o.value === cur) opt.selected = true;
    select.appendChild(opt);
  });
  select.addEventListener("change", () => {
    rec.upload_status = select.value;
    if (select.value === "pending" || select.value === "success") {
      rec.upload_error = "";
    }
    schedulePersist();
    render();
  });
  return select;
}

function cleanNombre(nombre) {
  return String(nombre || "")
    .replace(/^\s*\d{1,2}[\.\)]\s*/, "")
    .trim();
}

function renumberRecords() {
  state.records.forEach((rec, i) => {
    const raw = String(rec.nombre || "");
    if (/^\s*\d{1,2}[\.\)]\s/.test(raw)) {
      const base = cleanNombre(raw) || `Registro ${i + 1}`;
      rec.nombre = `${i + 1}. ${base}`;
    }
  });
}

async function deleteRecord(idx) {
  const rec = state.records[idx];
  if (!rec || !state.editing) return;
  const label = cleanNombre(rec.nombre) || `Registro ${idx + 1}`;
  const ok = window.confirm(
    `¿Seguro que quieres borrar el registro "${label}"?\n\nEsta acción no se puede deshacer.`
  );
  if (!ok) return;
  state.records.splice(idx, 1);
  renumberRecords();
  render();
  await persist();
  addChat("bot", `Registro eliminado. Quedan ${state.records.length}.`);
}

function makeDeleteBtn(idx) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "row-delete";
  btn.title = "Borrar registro";
  btn.setAttribute("aria-label", "Borrar registro");
  btn.appendChild(iconSvg("i-trash"));
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    deleteRecord(idx);
  });
  return btn;
}

function renderLista() {
  const panel = $("recordsPanel");
  const fields = enabledFields();
  if (!state.records.length) {
    panel.innerHTML = `<div class="empty">${emptyMessage()}</div>`;
    return;
  }
  panel.innerHTML = "";
  state.records.forEach((rec, idx) => {
    const card = document.createElement("article");
    card.className =
      "record-card " +
      (rec.upload_status || "") +
      (rec.incomplete ? " incomplete" : "") +
      (rec.duplicate ? " duplicate" : "");
    const head = document.createElement("div");
    head.className = "record-head";
    const h = document.createElement("h3");
    h.textContent = rec.nombre || `Registro ${idx + 1}`;
    head.appendChild(h);
    if (state.editing) head.appendChild(makeDeleteBtn(idx));
    card.appendChild(head);
    if (rec.incomplete || rec.location_uncertain || rec.duplicate) {
      const warn = document.createElement("div");
      warn.className = "record-warn" + (rec.duplicate ? " duplicate" : "");
      warn.textContent = (rec.warnings || ["Revisar datos"]).join(" · ");
      card.appendChild(warn);
    }
    const kv = document.createElement("div");
    kv.className = "kv";
    fields.forEach((f) => {
      const k = document.createElement("div");
      k.className = "k";
      k.textContent = f.label;
      const v = document.createElement("div");
      v.appendChild(fieldInputFor(f, rec));
      kv.appendChild(k);
      kv.appendChild(v);
    });
    const sk = document.createElement("div");
    sk.className = "k";
    sk.textContent = "Estado subida";
    const sv = document.createElement("div");
    sv.appendChild(statusSelectFor(rec));
    kv.appendChild(sk);
    kv.appendChild(sv);
    if (rec.upload_error) {
      const err = document.createElement("div");
      err.className = "k";
      err.textContent = "Error";
      const ev = document.createElement("div");
      ev.textContent = rec.upload_error;
      kv.appendChild(err);
      kv.appendChild(ev);
    }
    card.appendChild(kv);
    panel.appendChild(card);
  });
}

function renderTabla() {
  const panel = $("recordsPanel");
  const fields = enabledFields();
  if (!state.records.length) {
    panel.innerHTML = `<div class="empty">${emptyMessage()}</div>`;
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "data";
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  const th0 = document.createElement("th");
  th0.textContent = "#";
  hr.appendChild(th0);
  fields.forEach((f) => {
    const th = document.createElement("th");
    th.textContent = f.label;
    hr.appendChild(th);
  });
  const thStatus = document.createElement("th");
  thStatus.className = "col-status";
  thStatus.textContent = "Estado";
  hr.appendChild(thStatus);
  if (state.editing) {
    const thDel = document.createElement("th");
    thDel.className = "col-actions";
    thDel.textContent = "";
    thDel.title = "Acciones";
    hr.appendChild(thDel);
  }
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  state.records.forEach((rec, idx) => {
    const tr = document.createElement("tr");
    tr.className = rec.upload_status || "";
    const td0 = document.createElement("td");
    td0.textContent = String(idx + 1);
    tr.appendChild(td0);
    fields.forEach((f) => {
      const td = document.createElement("td");
      td.appendChild(fieldInputFor(f, rec));
      tr.appendChild(td);
    });
    const tdStatus = document.createElement("td");
    tdStatus.className = "col-status";
    tdStatus.appendChild(statusSelectFor(rec));
    tr.appendChild(tdStatus);
    if (state.editing) {
      const tdDel = document.createElement("td");
      tdDel.className = "col-actions";
      tdDel.appendChild(makeDeleteBtn(idx));
      tr.appendChild(tdDel);
    }
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  panel.innerHTML = "";
  panel.appendChild(wrap);
}

function formatPrecioSimple(rec) {
  if (isYes(rec.pagado)) return "$0";
  const raw = String(rec.precio ?? "").trim();
  if (!raw) return "$0";
  const num = raw.replace(/[^\d.,]/g, "").replace(",", ".");
  if (!num) return raw.startsWith("$") ? raw : `$${raw}`;
  const n = Number(num);
  if (Number.isFinite(n)) {
    return Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`;
  }
  return raw.startsWith("$") ? raw : `$${raw}`;
}

function formatContenidoSimple(rec) {
  const producto = String(rec.producto || "").trim();
  if (isYes(rec.grabado)) {
    const msg = String(rec.mensaje_grabado || "").trim();
    if (msg) return producto ? `${producto} — Grabado: ${msg}` : `Grabado: ${msg}`;
    return producto ? `${producto} — Grabado` : "Grabado";
  }
  return producto;
}

function formatDireccionSimple(rec) {
  return [rec.departamento, rec.municipio, rec.direccion, rec.punto_referencia]
    .map((x) => String(x || "").trim())
    .filter(Boolean)
    .join(", ");
}

function formatNotaSimple(rec) {
  const nota = String(rec.observaciones || "").trim();
  const emerg = String(rec.numero_de_emergencia || "").trim();
  if (nota && emerg) return `${nota} · Emergencia: ${emerg}`;
  if (emerg) return `Emergencia: ${emerg}`;
  return nota;
}

function formatSimpleText() {
  if (!state.records.length) return "";
  return state.records
    .map((rec, idx) => {
      const lines = [
        `${idx + 1}. ${cleanNombre(rec.nombre) || `Registro ${idx + 1}`}`,
        `\t${String(rec.telefono || "").trim()}`,
        `\t${formatDireccionSimple(rec)}`,
        `\t${formatContenidoSimple(rec)}`,
        `\t${formatPrecioSimple(rec)}`,
        `\t${formatNotaSimple(rec)}`,
      ];
      return lines.join("\n");
    })
    .join("\n\n");
}

function renderSimple() {
  const panel = $("recordsPanel");
  if (!state.records.length) {
    panel.innerHTML = `<div class="empty">${emptyMessage()}</div>`;
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "simple-view";

  const bar = document.createElement("div");
  bar.className = "simple-bar";
  const hint = document.createElement("span");
  hint.className = "simple-hint";
  hint.textContent = "Texto plano · solo lectura · seleccionable";
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "action gray mini";
  copyBtn.appendChild(iconSvg("i-copy"));
  const copyLbl = document.createElement("span");
  copyLbl.textContent = "Copiar todo";
  copyBtn.appendChild(copyLbl);
  copyBtn.addEventListener("click", async () => {
    const text = formatSimpleText();
    try {
      await navigator.clipboard.writeText(text);
      copyLbl.textContent = "Copiado";
      setTimeout(() => {
        copyLbl.textContent = "Copiar todo";
      }, 1500);
    } catch {
      pre.focus();
      pre.select();
      copyLbl.textContent = "Selecciona y Ctrl+C";
      setTimeout(() => {
        copyLbl.textContent = "Copiar todo";
      }, 2000);
    }
  });
  bar.appendChild(hint);
  bar.appendChild(copyBtn);

  const pre = document.createElement("textarea");
  pre.className = "simple-text";
  pre.readOnly = true;
  pre.spellcheck = false;
  pre.value = formatSimpleText();
  pre.addEventListener("focus", () => pre.select());

  wrap.appendChild(bar);
  wrap.appendChild(pre);
  panel.innerHTML = "";
  panel.appendChild(wrap);
}

function render() {
  state.records = normalizeRecords(state.records);
  applyDuplicateWarnings(state.records);
  syncRegionSelect();
  syncUploadButton();
  const editBtn = $("editBtn");
  if (editBtn) editBtn.disabled = state.view === "simple";
  if (state.view === "tabla") renderTabla();
  else if (state.view === "simple") renderSimple();
  else renderLista();
}

async function persist(opts = {}) {
  const fecha = opts.fecha ?? state.fecha;
  const zona = opts.zona ?? currentZona();
  const records = opts.records ?? state.records;
  if (!fecha) return;
  await api("/api/day", {
    method: "POST",
    body: JSON.stringify({ fecha, zona, records }),
  });
}

function schedulePersist() {
  if (state.saveTimer) clearTimeout(state.saveTimer);
  const zona = currentZona();
  const fecha = state.fecha;
  state.saveTimer = setTimeout(() => {
    persist({ zona, fecha }).catch(() => {});
  }, 700);
}

async function loadDay(fecha) {
  const zona = currentZona();
  const seq = ++state.loadSeq;
  const data = await api(`/api/day/${fecha}?zona=${encodeURIComponent(zona)}`);
  if (seq !== state.loadSeq) return;
  state.fecha = fecha; // corta YYYY-MM-DD para guardar
  state.records = normalizeRecords(data.records || []);
  $("fechaInput").value = fecha;
  $("fechaLabel").textContent = data.fecha_label || fecha;
  render();
  updateUploadHint(data.upload);
}

function updateUploadHint(upload) {
  const el = $("uploadHint");
  const box = $("uploadProgress");
  const fill = $("uploadProgressFill");
  const txt = $("uploadProgressText");
  const stopBtn = $("stopUploadBtn");
  if (!upload) return;
  if (upload.running) {
    const cur = (upload.current_index ?? 0) + 1;
    const total = upload.total || state.records.length || 1;
    const done = upload.done_count || 0;
    el.textContent = `Subiendo… ${done}/${total} (actual #${cur})`;
    if (box) box.classList.remove("hidden");
    if (fill) fill.style.width = `${Math.min(100, Math.round((done / total) * 100))}%`;
    if (txt) txt.textContent = `${done} de ${total}`;
    if (stopBtn) {
      stopBtn.disabled = false;
      stopBtn.classList.add("active");
    }
    syncUploadButton(true);
  } else {
    if (box) box.classList.add("hidden");
    if (stopBtn) {
      stopBtn.disabled = true;
      stopBtn.classList.remove("active");
    }
    syncUploadButton(false);
    if (upload.paused_at != null)
      el.textContent = `Pausado en #${upload.paused_at + 1}. Corrige, pon Pendiente si hace falta y vuelve a subir.`;
    else el.textContent = "";
  }
}

async function init() {
  const meta = await api("/api/meta");
  try {
    state.locations = await api("/api/ubicaciones");
  } catch (_) {
    state.locations = null;
  }
  state.fields = meta.settings.fields || [];
  state.view = meta.settings.view || "lista";
  state.sistrackEmail = meta.settings.sistrack_email || "";
  state.sistrackPasswordSet = !!meta.settings.sistrack_password_set;
  state.sistrackPassword = "";
  state.uploadPlatform = meta.settings.upload_platform === "forza" ? "forza" : "sistrack";
  state.forzaCodigo = meta.settings.forza_codigo || "";
  state.forzaUsuario = meta.settings.forza_usuario || "";
  state.forzaPasswordSet = !!meta.settings.forza_password_set;
  state.uploadHeadless = !!meta.settings.upload_headless;
  state.uploadDryRun = !!meta.settings.upload_dry_run;
  state.defaultEntrega = meta.default_entrega || "";
  if (!["lista", "tabla", "simple"].includes(state.view)) state.view = "lista";
  $("viewSelect").value = state.view;
  state.region = loadRegion();
  syncRegionSelect();
  if (meta.settings.theme === "dark") document.body.classList.add("dark");
  setThemeIcon(meta.settings.theme === "dark" ? "dark" : "light");
  await loadDay(meta.default_fecha);
  setChatCollapsed(loadChatCollapsed());
  addChat("bot", "Hola. Pega un pedido desordenado y lo agrego a la tabla del día seleccionado.");
}

$("clearChatBtn")?.addEventListener("click", clearChatHistory);
$("collapseChatBtn")?.addEventListener("click", () => setChatCollapsed(true));
$("chatRailBtn")?.addEventListener("click", () => {
  setChatCollapsed(false);
  const log = $("chatLog");
  if (log) log.scrollTop = log.scrollHeight;
});

$("chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("chatMessage").value.trim();
  if (!msg) return;
  addChat("user", msg);
  $("chatMessage").value = "";
  try {
    await persist();
    const res = await api("/api/chat/preview", {
      method: "POST",
      body: JSON.stringify({ message: msg, fecha: state.fecha, zona: currentZona() }),
    });
    if (!res.ok) {
      addChat("bot", res.reply || "No se detecto pedido.");
      return;
    }
    state.previewRecords = res.preview || [];
    $("previewReply").textContent = res.reply || "";
    const list = $("previewList");
    list.innerHTML = "";
    state.previewRecords.forEach((r, i) => {
      const div = document.createElement("div");
      const isDup = !!r.duplicate || (r.warnings || []).some((w) => /pedido repetido/i.test(String(w)));
      div.className = "preview-item" + (r.incomplete || isDup ? " warn" : "") + (isDup ? " duplicate" : "");
      const warns = (r.warnings || []).join(" · ");
      div.innerHTML = `<strong>${r.nombre || "Pedido " + (i + 1)}</strong>
        <div>${r.telefono || "—"} · ${r.departamento || ""} / ${r.municipio || ""}</div>
        <div>${r.direccion || ""}</div>
        <div>${r.producto || ""} · $${r.precio || "0"} · entrega ${r.fecha_entrega || ""}</div>
        ${warns ? `<div class="warn-text">${warns}</div>` : ""}
        ${isDup ? `<div class="dup-text">Posible pedido repetido (mismo nombre o telefono)</div>` : ""}`;
      list.appendChild(div);
    });
    setModalOpen("previewModal", true);
    addChat("bot", res.reply);
    if (res.duplicates?.length) {
      addChat("bot", "Pedidos repetidos detectados: " + res.duplicates.slice(0, 5).join("; "));
    }
  } catch (err) {
    addChat("bot", "Error: " + err.message);
  }
});

$("previewCancel")?.addEventListener("click", () => {
  state.previewRecords = null;
  setModalOpen("previewModal", false);
  addChat("bot", "Pedido no agregado.");
});

$("previewConfirm")?.addEventListener("click", async () => {
  try {
    const res = await api("/api/chat/confirm", {
      method: "POST",
      body: JSON.stringify({ fecha: state.fecha, zona: currentZona(), records: state.previewRecords || [] }),
    });
    state.records = normalizeRecords(res.records || []);
    state.previewRecords = null;
    setModalOpen("previewModal", false);
    addChat("bot", res.reply);
    if (res.duplicates?.length) {
      addChat(
        "bot",
        "Atencion: hay pedidos repetidos por nombre o telefono. Revisalos antes de subir."
      );
    }
    render();
  } catch (err) {
    addChat("bot", "Error al confirmar: " + err.message);
  }
});

$("fechaInput").addEventListener("change", async (e) => {
  const fecha = e.target.value; // corta YYYY-MM-DD
  if (!fecha) return;
  if (state.fecha && state.fecha !== fecha) {
    try {
      await persist();
    } catch (_) {}
  }
  await loadDay(fecha);
  const file = currentZona() === "ss" ? `${fecha}_SS` : fecha;
  addChat("bot", `Fecha cambiada a ${$("fechaLabel").textContent}. ${zonaLabel()} se guarda como ${file}.`);
});
$("fechaInput").addEventListener("click", () => {
  if (typeof $("fechaInput").showPicker === "function") {
    try {
      $("fechaInput").showPicker();
    } catch (_) {}
  }
});

$("regionSelect")?.addEventListener("change", (e) => {
  e.preventDefault();
  setRegion(e.target.value).catch((err) => {
    addChat("bot", "No se pudo cambiar de tabla: " + err.message);
  });
});

$("viewSelect").addEventListener("change", async (e) => {
  const v = e.target.value;
  state.view = ["lista", "tabla", "simple"].includes(v) ? v : "lista";
  if (state.view === "simple") setEditing(false);
  render();
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ view: state.view }),
  });
});

$("refreshBtn")?.addEventListener("click", () => {
  refreshAndRevalidate();
});

$("themeBtn").addEventListener("click", async () => {
  document.body.classList.toggle("dark");
  const theme = document.body.classList.contains("dark") ? "dark" : "light";
  setThemeIcon(theme);
  await api("/api/settings", { method: "POST", body: JSON.stringify({ theme }) });
});

$("editBtn").addEventListener("click", async () => {
  if (state.editing) {
    setEditing(false);
    await persist();
    addChat("bot", "Cambios guardados.");
  } else {
    setEditing(true);
  }
  render();
});

$("exportBtn").addEventListener("click", async () => {
  await persist();
  const res = await api(`/api/export/${state.fecha}?zona=${encodeURIComponent(currentZona())}`, { method: "POST" });
  window.location.href = `/api/export/${state.fecha}/download?zona=${encodeURIComponent(currentZona())}`;
  addChat("bot", `Excel listo: ${res.filename}`);
});

function startPolling() {
  if (state.uploadPoll) clearInterval(state.uploadPoll);
  state.uploadPoll = setInterval(async () => {
    try {
      const st = await api(`/api/upload/status?fecha=${state.fecha}&zona=${encodeURIComponent(currentZona())}`);
      state.records = normalizeRecords(st.records || state.records);
      render();
      updateUploadHint(st);
      if (!st.running) {
        clearInterval(state.uploadPoll);
        state.uploadPoll = null;
        syncUploadButton(false);
        if (st.paused_at != null) {
          addChat("bot", `Error en registro #${st.paused_at + 1}. Subida pausada.`);
        } else {
          addChat("bot", "Subida finalizada.");
        }
      }
    } catch (_) {}
  }, 1500);
}

$("uploadBtn").addEventListener("click", async () => {
  if (currentZona() === "ss") {
    syncUploadButton(false);
    addChat("bot", "San Salvador no se sube al sistema.");
    return;
  }
  try {
    await persist();
    const val = await api("/api/upload/validate", {
      method: "POST",
      body: JSON.stringify({ fecha: state.fecha, zona: currentZona() }),
    });
    if (val.issues && val.issues.length) {
      const msg =
        "Hay avisos antes de subir:\n- " +
        val.issues.slice(0, 8).join("\n- ") +
        (val.issues.length > 8 ? `\n… (+${val.issues.length - 8})` : "") +
        "\n\n¿Subir de todos modos?";
      if (!confirm(msg)) return;
    }
    syncUploadButton(true);
    const res = await api("/api/upload/start", {
      method: "POST",
      body: JSON.stringify({ fecha: state.fecha, zona: currentZona() }),
    });
    if (res.message) {
      addChat("bot", res.message);
      syncUploadButton(false);
      return;
    }
    addChat("bot", `Iniciando subida (${state.uploadPlatform === "forza" ? "Forza" : "Sistrack"}) desde #${(res.started_at || 0) + 1}…`);
    if (res.warnings?.length) addChat("bot", "Avisos: " + res.warnings.slice(0, 5).join("; "));
    updateUploadHint({ ...res, running: true });
    startPolling();
  } catch (err) {
    syncUploadButton(false);
    addChat("bot", "No se pudo iniciar: " + err.message);
  }
});

$("stopUploadBtn")?.addEventListener("click", async () => {
  const btn = $("stopUploadBtn");
  if (!btn || btn.disabled) return;
  try {
    await api("/api/upload/stop", { method: "POST", body: "{}" });
    addChat("bot", "Deteniendo subida…");
  } catch (err) {
    addChat("bot", "No se pudo detener: " + err.message);
  }
});

function fieldInputFor(f, rec, opts = {}) {
  const type = f.type || inferTypeFromKey(f.key);
  const listView = state.view !== "tabla";

  if (state.locations && (f.key === "departamento" || f.key === "municipio")) {
    return f.key === "departamento" ? deptSelectFor(rec) : muniSelectFor(rec);
  }

  // Si/No → checkbox en vista documento (y también en tabla)
  if (type === "bool" || f.key === "grabado" || f.key === "pagado") {
    const wrap = document.createElement("label");
    wrap.className = "check-wrap";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = isYes(rec[f.key]);
    const span = document.createElement("span");
    span.textContent = input.checked ? "Si" : "No";
    const sync = () => {
      rec[f.key] = input.checked ? "Si" : "No";
      span.textContent = rec[f.key];
      if (f.key === "pagado") {
        if (input.checked) rec.precio = "0";
        render();
        return;
      }
      if (f.key === "grabado") {
        render();
      }
    };
    input.addEventListener("change", sync);
    wrap.appendChild(input);
    wrap.appendChild(span);
    return wrap;
  }

  let input;
  if (
    type === "textarea" ||
    f.key === "observaciones" ||
    f.key === "mensaje_grabado" ||
    f.key === "direccion" ||
    f.key === "producto"
  ) {
    input = document.createElement("textarea");
    if (f.key === "observaciones") {
      rec.observaciones = normalizeObs(rec.observaciones);
      input.placeholder = DEFAULT_OBS;
    }
  } else if (type === "date" || f.key === "fecha_entrega") {
    input = document.createElement("input");
    input.type = "date";
    input.className = "date-input";
    input.value = toDateInputValue(rec[f.key] || state.fecha || "");
    const openPicker = (e) => {
      if (!state.editing) return;
      const panel = $("recordsPanel");
      if (panel) panel.classList.add("picking-date");
      // showPicker funciona aunque el contenedor tenga overflow
      if (typeof input.showPicker === "function") {
        try {
          if (e) e.preventDefault();
          input.showPicker();
        } catch (_) {
          // fallback: focus nativo
          input.focus();
        }
      }
    };
    input.addEventListener("input", () => {
      rec[f.key] = input.value;
    });
    input.addEventListener("change", () => {
      rec[f.key] = input.value;
      $("recordsPanel")?.classList.remove("picking-date");
    });
    input.addEventListener("blur", () => {
      $("recordsPanel")?.classList.remove("picking-date");
    });
    input.addEventListener("mousedown", openPicker);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") openPicker(e);
    });
    return input;
  } else {
    input = document.createElement("input");
    input.type = "text";
  }

  bindValue(input, rec, f.key);

  // Reglas de edición
  if (f.key === "precio" && isYes(rec.pagado)) {
    rec.precio = "0";
    input.value = "0";
    input.readOnly = true;
    input.classList.add("locked");
    input.title = "Pagado: precio fijo en 0";
  }
  if (f.key === "mensaje_grabado" && !isYes(rec.grabado)) {
    input.readOnly = true;
    input.classList.add("locked");
    input.placeholder = "Activa Grabado para editar";
    input.title = "Grabado = No";
  }

  return input;
}

function inferTypeFromKey(key) {
  if (["observaciones", "mensaje_grabado", "direccion", "producto"].includes(key)) return "textarea";
  if (["grabado", "pagado"].includes(key)) return "bool";
  if (key === "fecha_entrega") return "date";
  return "text";
}

function slugifyKey(label) {
  const base = (label || "campo")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "") || "campo";
  let key = base;
  let i = 2;
  const keys = new Set((state.fieldsDraft || state.fields).map((f) => f.key));
  while (keys.has(key)) {
    key = `${base}_${i++}`;
  }
  return key;
}

const DEFAULT_FIELDS_FALLBACK = [
  { key: "nombre", label: "Nombre", enabled: true, type: "text" },
  { key: "telefono", label: "Telefono", enabled: true, type: "text" },
  { key: "departamento", label: "Departamento", enabled: true, type: "text" },
  { key: "municipio", label: "Municipio", enabled: true, type: "text" },
  { key: "direccion", label: "Direccion", enabled: true, type: "textarea" },
  { key: "punto_referencia", label: "Punto de referencia", enabled: true, type: "text" },
  { key: "producto", label: "Contenido o Producto/s", enabled: true, type: "textarea" },
  { key: "grabado", label: "Grabado (Si/No)", enabled: true, type: "bool" },
  { key: "mensaje_grabado", label: "Mensaje del grabado", enabled: true, type: "textarea" },
  { key: "precio", label: "Precio total", enabled: true, type: "text" },
  { key: "pagado", label: "Pagado (Si/No)", enabled: true, type: "bool" },
  { key: "fecha_entrega", label: "Fecha de entrega", enabled: true, type: "date" },
  { key: "observaciones", label: "Observaciones", enabled: true, type: "textarea" },
  { key: "numero_de_emergencia", label: "Numero de emergencia", enabled: true, type: "text" },
];

function cloneFields(fields) {
  return JSON.parse(JSON.stringify(fields || []));
}

function renderFieldsCrud() {
  const list = $("fieldsList");
  list.innerHTML = "";
  const draft = state.fieldsDraft || [];
  if (!draft.length) {
    list.innerHTML = `<div class="empty" style="padding:20px">No hay campos. Agrega uno arriba.</div>`;
    return;
  }
  draft.forEach((f, i) => {
    const row = document.createElement("div");
    row.className = "field-row";

    if (state.editingFieldIndex === i) {
      row.classList.add("editing");
      const edit = document.createElement("div");
      edit.className = "field-edit-row";
      const labelIn = document.createElement("input");
      labelIn.value = f.label || "";
      labelIn.placeholder = "Etiqueta";
      const typeIn = document.createElement("select");
      [
        ["text", "Texto"],
        ["textarea", "Texto largo"],
        ["bool", "Si/No"],
        ["date", "Fecha"],
      ].forEach(([v, t]) => {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = t;
        if ((f.type || inferTypeFromKey(f.key)) === v) o.selected = true;
        typeIn.appendChild(o);
      });
      const saveBtn = document.createElement("button");
      saveBtn.type = "button";
      saveBtn.className = "action blue mini";
      saveBtn.appendChild(iconSvg("i-check"));
      const saveLbl = document.createElement("span");
      saveLbl.textContent = "OK";
      saveBtn.appendChild(saveLbl);
      saveBtn.addEventListener("click", () => {
        const label = labelIn.value.trim();
        if (!label) return;
        draft[i].label = label;
        draft[i].type = typeIn.value;
        state.editingFieldIndex = null;
        renderFieldsCrud();
      });
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "action gray mini";
      cancelBtn.title = "Cancelar";
      cancelBtn.appendChild(iconSvg("i-x"));
      cancelBtn.addEventListener("click", () => {
        state.editingFieldIndex = null;
        renderFieldsCrud();
      });
      edit.appendChild(labelIn);
      edit.appendChild(typeIn);
      edit.appendChild(saveBtn);
      edit.appendChild(cancelBtn);
      row.appendChild(edit);
      list.appendChild(row);
      return;
    }

    const main = document.createElement("div");
    main.className = "field-main";
    const label = document.createElement("span");
    label.className = "label-text";
    label.textContent = f.label;
    const tag = document.createElement("span");
    tag.className = "key-tag";
    tag.textContent = f.key;
    main.appendChild(label);
    main.appendChild(tag);

    const tools = document.createElement("div");
    tools.className = "field-tools";

    const up = document.createElement("button");
    up.type = "button";
    up.title = "Subir";
    up.appendChild(iconSvg("i-up"));
    up.disabled = i === 0;
    up.addEventListener("click", () => {
      [draft[i - 1], draft[i]] = [draft[i], draft[i - 1]];
      renderFieldsCrud();
    });

    const down = document.createElement("button");
    down.type = "button";
    down.title = "Bajar";
    down.appendChild(iconSvg("i-down"));
    down.disabled = i === draft.length - 1;
    down.addEventListener("click", () => {
      [draft[i + 1], draft[i]] = [draft[i], draft[i + 1]];
      renderFieldsCrud();
    });

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.title = "Editar";
    editBtn.appendChild(iconSvg("i-pencil"));
    editBtn.addEventListener("click", () => {
      state.editingFieldIndex = i;
      renderFieldsCrud();
    });

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.title = "Eliminar";
    delBtn.className = "danger";
    delBtn.appendChild(iconSvg("i-trash"));
    delBtn.addEventListener("click", () => {
      if (!confirm(`Eliminar campo "${f.label}"?`)) return;
      draft.splice(i, 1);
      state.editingFieldIndex = null;
      renderFieldsCrud();
    });

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.title = "Visible";
    cb.checked = f.enabled !== false;
    cb.addEventListener("change", () => {
      draft[i].enabled = cb.checked;
    });

    tools.appendChild(up);
    tools.appendChild(down);
    tools.appendChild(editBtn);
    tools.appendChild(delBtn);
    tools.appendChild(cb);

    row.appendChild(main);
    row.appendChild(tools);
    list.appendChild(row);
  });
}

function applyPlatformUI(platform) {
  const plat = platform === "forza" ? "forza" : "sistrack";
  state.uploadPlatform = plat; // <-- Asegurar que se actualiza
  $("platformSistrack")?.classList.toggle("active", plat === "sistrack");
  $("platformForza")?.classList.toggle("active", plat === "forza");
  
  const sis = $("credsSistrack");
  const forz = $("credsForza");
  if (sis) {
    sis.classList.toggle("hidden", plat !== "sistrack");
    sis.hidden = plat !== "sistrack";
  }
  if (forz) {
    forz.classList.toggle("hidden", plat !== "forza");
    forz.hidden = plat !== "forza";
  }
}

function openSettings() {
  state.fieldsDraft = cloneFields(state.fields);
  state.editingFieldIndex = null;
  $("newFieldLabel").value = "";
  $("newFieldType").value = "text";
  applyPlatformUI(state.uploadPlatform);
  $("sistrackEmail").value = state.sistrackEmail || "";
  $("sistrackPassword").value = "";
  $("sistrackPassword").placeholder = state.sistrackPasswordSet
    ? "•••••••• (dejar vacío para no cambiar)"
    : "Contraseña";
  $("passwordHint").textContent = state.sistrackPasswordSet
    ? "Contraseña ya configurada."
    : "Aún no hay contraseña guardada.";
  if ($("forzaCodigo")) $("forzaCodigo").value = state.forzaCodigo || "";
  if ($("forzaUsuario")) $("forzaUsuario").value = state.forzaUsuario || "";
  if ($("forzaPassword")) {
    $("forzaPassword").value = "";
    $("forzaPassword").placeholder = state.forzaPasswordSet
      ? "•••••••• (dejar vacío para no cambiar)"
      : "Contraseña";
    $("forzaPassword").type = "password";
  }
  if ($("forzaPasswordHint")) {
    $("forzaPasswordHint").textContent = state.forzaPasswordSet
      ? "Contraseña ya configurada."
      : "Aún no hay contraseña guardada.";
  }
  $("uploadHeadless").checked = !!state.uploadHeadless;
  $("uploadDryRun").checked = !!state.uploadDryRun;
  $("sistrackPassword").type = "password";
  const eye = $("togglePassBtn")?.querySelector("use");
  if (eye) eye.setAttribute("href", "#i-eye");
  const eyeF = $("toggleForzaPassBtn")?.querySelector("use");
  if (eyeF) eyeF.setAttribute("href", "#i-eye");
  renderFieldsCrud();
  setModalOpen("settingsModal", true);
}

function moneyFmt(n) {
  const v = Number(n) || 0;
  return "$" + v.toLocaleString("es-SV", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}

function monthInputValueFromFecha(fecha) {
  const m = String(fecha || "").match(/^(\d{4})-(\d{2})/);
  if (m) return `${m[1]}-${m[2]}`;
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function renderBarChart(chartEl, emptyEl, rows, opts) {
  const labelKey = opts.labelKey;
  const valueKey = opts.valueKey;
  const metaFn = opts.metaFn;
  const titleFn = opts.titleFn;
  const rowClass = opts.rowClass || "";
  const fillClass = opts.fillClass || "";
  if (!chartEl) return;
  if (!rows.length) {
    chartEl.innerHTML = "";
    emptyEl?.classList.remove("hidden");
    return;
  }
  emptyEl?.classList.add("hidden");
  const max = Math.max(...rows.map((r) => Number(r[valueKey]) || 0), 1);
  chartEl.innerHTML = rows
    .map((r) => {
      const value = Number(r[valueKey]) || 0;
      const pct = Math.max(2, Math.round((value / max) * 100));
      const label = String(r[labelKey] || "—");
      const safeLabel = label.replace(/"/g, "&quot;");
      return `<div class="stats-bar-row ${rowClass}" title="${titleFn(r)}">
        <div class="stats-bar-label" title="${safeLabel}">${label}</div>
        <div class="stats-bar-track"><div class="stats-bar-fill ${fillClass}" style="width:${pct}%"></div></div>
        <div class="stats-bar-meta">${metaFn(r)}</div>
      </div>`;
    })
    .join("");
}

function renderStats(data) {
  const sub = $("statsSubtitle");
  if (sub) {
    sub.textContent = data.label
      ? `Resumen de ${data.label}`
      : "Resumen del mes";
  }
  const summary = $("statsSummary");
  if (summary) {
    const cards = [
      { label: "Pedidos", value: String(data.total_pedidos || 0) },
      { label: "Ventas totales", value: moneyFmt(data.total_ventas) },
      { label: "Promedio / pedido", value: moneyFmt(data.promedio) },
      { label: "Pagados", value: String(data.pagados || 0) },
      { label: "Pendientes de pago", value: String(data.no_pagados || 0) },
      { label: "Días con pedidos", value: String(data.dias_con_datos || 0) },
    ];
    summary.innerHTML = cards
      .map(
        (c) =>
          `<div class="stat-card"><span class="stat-label">${c.label}</span><span class="stat-value">${c.value}</span></div>`
      )
      .join("");
  }

  renderBarChart($("statsChart"), $("statsDeptEmpty"), data.by_department || [], {
    labelKey: "departamento",
    valueKey: "ventas",
    metaFn: (r) => `${moneyFmt(r.ventas)} · ${Number(r.pedidos) || 0}`,
    titleFn: (r) =>
      `${r.departamento}: ${moneyFmt(r.ventas)} · ${Number(r.pedidos) || 0} pedido(s)`,
  });

  renderBarChart(
    $("statsProductsChart"),
    $("statsProductsEmpty"),
    data.top_products || [],
    {
      labelKey: "producto",
      valueKey: "cantidad",
      rowClass: "product",
      fillClass: "product",
      metaFn: (r) => `${Number(r.cantidad) || 0} · ${moneyFmt(r.ventas)}`,
      titleFn: (r) =>
        `${r.producto}: ${Number(r.cantidad) || 0} uds · ${moneyFmt(r.ventas)}`,
    }
  );
}

async function loadMonthStats(yearMonth) {
  const [y, m] = String(yearMonth || "").split("-").map(Number);
  if (!y || !m) return;
  const data = await api(`/api/stats/month?year=${y}&month=${m}`);
  renderStats(data);
}

async function openStats() {
  const input = $("statsMonthInput");
  if (input && !input.value) {
    input.value = monthInputValueFromFecha(state.fecha);
  }
  setModalOpen("statsModal", true);
  try {
    await loadMonthStats(input?.value || monthInputValueFromFecha(state.fecha));
  } catch (err) {
    addChat("bot", "No se pudieron cargar las estadísticas: " + (err.message || err));
  }
}

$("togglePassBtn")?.addEventListener("click", () => {
  const input = $("sistrackPassword");
  const use = $("togglePassBtn")?.querySelector("use");
  if (!input) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  if (use) use.setAttribute("href", show ? "#i-eye-off" : "#i-eye");
});
$("toggleForzaPassBtn")?.addEventListener("click", () => {
  const input = $("forzaPassword");
  const use = $("toggleForzaPassBtn")?.querySelector("use");
  if (!input) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  if (use) use.setAttribute("href", show ? "#i-eye-off" : "#i-eye");
});
$("platformSistrack")?.addEventListener("click", () => {
  applyPlatformUI("sistrack");
});

$("platformForza")?.addEventListener("click", () => {
  applyPlatformUI("forza");
});

$("fieldCreateForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const label = $("newFieldLabel").value.trim();
  if (!label) return;
  if (!state.fieldsDraft) state.fieldsDraft = cloneFields(state.fields);
  const key = slugifyKey(label);
  state.fieldsDraft.push({
    key,
    label,
    enabled: true,
    type: $("newFieldType").value || "text",
  });
  $("newFieldLabel").value = "";
  state.editingFieldIndex = null;
  renderFieldsCrud();
});

$("settingsBtn").addEventListener("click", openSettings);
$("statsBtn")?.addEventListener("click", () => {
  openStats();
});
$("statsClose")?.addEventListener("click", () => {
  setModalOpen("statsModal", false);
});
$("statsMonthInput")?.addEventListener("change", async (e) => {
  try {
    await loadMonthStats(e.target.value);
  } catch (err) {
    addChat("bot", "No se pudieron cargar las estadísticas: " + (err.message || err));
  }
});
$("statsModal")?.addEventListener("click", (e) => {
  if (e.target === $("statsModal")) setModalOpen("statsModal", false);
});
$("settingsCancel").addEventListener("click", () => {
  state.fieldsDraft = null;
  state.editingFieldIndex = null;
  setModalOpen("settingsModal", false);
});
$("settingsReset").addEventListener("click", () => {
  if (!confirm("Restablecer los campos por defecto?")) return;
  state.fieldsDraft = cloneFields(DEFAULT_FIELDS_FALLBACK);
  state.editingFieldIndex = null;
  renderFieldsCrud();
});
$("settingsSave").addEventListener("click", async () => {
  try {
    // Guardar campos
    state.fields = cloneFields(state.fieldsDraft || state.fields);
    state.fieldsDraft = null;
    state.editingFieldIndex = null;
    
    // Obtener credenciales
    const sistrackEmail = ($("sistrackEmail").value || "").trim();
    const sistrackPwd = $("sistrackPassword").value || "";
    const forzaCodigo = ($("forzaCodigo")?.value || "").trim();
    const forzaUsuario = ($("forzaUsuario")?.value || "").trim();
    const forzaPwd = $("forzaPassword")?.value || "";
    const uploadHeadless = !!$("uploadHeadless")?.checked;
    const uploadDryRun = !!$("uploadDryRun")?.checked;
    
    // Actualizar estado local
    state.sistrackEmail = sistrackEmail;
    state.forzaCodigo = forzaCodigo;
    state.forzaUsuario = forzaUsuario;
    state.uploadHeadless = uploadHeadless;
    state.uploadDryRun = uploadDryRun;
    
    // Construir payload
    const payload = {
      fields: state.fields,
      upload_platform: state.uploadPlatform, // <-- Asegurar que se guarda la plataforma seleccionada
      sistrack_email: sistrackEmail,
      forza_codigo: forzaCodigo,
      forza_usuario: forzaUsuario,
      upload_headless: uploadHeadless,
      upload_dry_run: uploadDryRun,
    };
    
    // Solo incluir contraseñas si se proporcionaron
    if (sistrackPwd) {
      payload.sistrack_password = sistrackPwd;
    }
    if (forzaPwd) {
      payload.forza_password = forzaPwd;
    }
    
    // Enviar al servidor
    const saved = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    
    // Actualizar estado con respuesta del servidor
    state.sistrackPasswordSet = !!saved.sistrack_password_set;
    state.forzaPasswordSet = !!saved.forza_password_set;
    state.uploadPlatform = saved.upload_platform || state.uploadPlatform;
    
    // Cerrar modal y actualizar UI
    setModalOpen("settingsModal", false);
    render();
    await persist();
    
    // Limpiar campos de contraseña por seguridad
    $("sistrackPassword").value = "";
    if ($("forzaPassword")) $("forzaPassword").value = "";
    
    addChat("bot", `Ajustes guardados. Plataforma: ${state.uploadPlatform === "forza" ? "Forza" : "Sistrack"}`);
    
  } catch (err) {
    addChat("bot", "Error al guardar ajustes: " + err.message);
  }
});

init().catch((e) => addChat("bot", "Error al iniciar: " + e.message));
