# -*- coding: utf-8 -*-
"""Order Track — API local FastAPI."""
from __future__ import annotations

import re
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sistrack.ubicaciones import locations_for_ui
from forza.ubicaciones_forza import forza_locations_for_ui
from web.parser import DEFAULT_FIELDS, DEFAULT_FIELDS_FORZA, parse_order_text
from web.store import (
    active_fields,
    archive_old_days,
    default_delivery_date,
    ensure_platform_fields,
    export_excel,
    format_fecha_es,
    load_day,
    load_product_keywords,
    load_settings,
    migrate_flat_pedidos_to_months,
    month_sales_stats,
    normalize_zona,
    public_settings,
    save_day,
    save_settings,
    update_record_status,
)
from web.upload_runner import UploadRunner
from web.inventario import (
    InventarioError,
    actualizar_categoria,
    actualizar_producto,
    ajustar_stock,
    crear_categoria,
    crear_producto,
    eliminar_categoria,
    eliminar_producto,
    kardex,
    listar_productos,
    registrar_entrada,
    registrar_salida,
    snapshot,
)
from web.cobros import (
    CobroError,
    detalle_cobro,
    registrar_cobro,
    snapshot as cobros_snapshot,
)

app = FastAPI(title="Order Track")
runner = UploadRunner()
STATIC = Path(__file__).parent / "static"

try:
    archive_old_days(60)
except Exception:
    pass


class ChatIn(BaseModel):
    message: str
    fecha: str
    zona: str = "dept"


class ConfirmChatIn(BaseModel):
    fecha: str
    records: list[dict[str, Any]]
    zona: str = "dept"


class SaveIn(BaseModel):
    fecha: str
    records: list[dict[str, Any]]
    zona: str = "dept"


class SettingsIn(BaseModel):
    fields: list[dict[str, Any]] | None = None
    fields_sistrack: list[dict[str, Any]] | None = None
    fields_forza: list[dict[str, Any]] | None = None
    theme: str | None = None
    view: str | None = None
    sistrack_email: str | None = None
    sistrack_password: str | None = None
    upload_platform: str | None = None
    forza_codigo: str | None = None
    forza_usuario: str | None = None
    forza_password: str | None = None
    upload_headless: bool | None = None
    upload_dry_run: bool | None = None


class UploadIn(BaseModel):
    fecha: str
    start_index: int | None = None
    zona: str = "dept"


class ProductoIn(BaseModel):
    nombre: str
    tipo_inventario: str = "producto_base"
    descripcion: str | None = None
    id_categoria: int
    precio_unitario: float = 0
    precio_paquete: float | None = 0
    unidades_por_paquete: float | None = 0
    stock_actual: float | None = 0
    stock_minimo: float | None = 0
    activo: bool | None = True
    motivo: str | None = None
    usuario: str | None = None


class MovimientoIn(BaseModel):
    id_producto: int
    cantidad: float | None = None
    stock_nuevo: float | None = None
    costo_unitario: float | None = None
    referencia: str | None = None
    motivo: str | None = None
    usuario: str | None = None


class CategoriaIn(BaseModel):
    nombre: str
    descripcion: str | None = None


class CobroIn(BaseModel):
    pedido_key: str
    monto: float
    id_metodo_pago: int
    fecha_cobro: str | None = None
    referencia_pago: str | None = None
    observaciones: str | None = None
    usuario: str | None = None


def _number_records(existing: list[dict], parsed: list[dict]) -> list[dict]:
    base = len(existing)
    out = []
    for i, rec in enumerate(parsed, start=1):
        num = base + i
        name = rec.get("nombre") or ""
        if not str(name).startswith(f"{num}."):
            name = re.sub(r"^\d{1,2}[\.\)]\s*", "", str(name)).strip()
            rec = {**rec, "nombre": f"{num}. {name}"}
        out.append(rec)
    return out


def _phone_key(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("503") and len(digits) >= 11:
        digits = digits[3:]
    return digits[-8:] if len(digits) >= 8 else digits


def _name_key(raw: Any) -> str:
    name = re.sub(r"^\s*\d{1,2}[\.\)]\s*", "", str(raw or "")).strip()
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return re.sub(r"\s+", " ", name).strip()


def _flag_duplicate_records(
    existing: list[dict],
    candidates: list[dict],
) -> tuple[list[dict], list[str]]:
    """Marca pedidos nuevos que repiten telefono o nombre del dia (o entre si)."""
    phone_seen: dict[str, str] = {}
    name_seen: dict[str, str] = {}
    for rec in existing:
        ph = _phone_key(rec.get("telefono"))
        nm = _name_key(rec.get("nombre"))
        label = str(rec.get("nombre") or "").strip() or "pedido existente"
        if ph:
            phone_seen.setdefault(ph, label)
        if len(nm) >= 3:
            name_seen.setdefault(nm, label)

    notices: list[str] = []
    out: list[dict] = []
    for rec in candidates:
        rec = dict(rec)
        warnings = [w for w in (rec.get("warnings") or []) if not re.search(r"pedido repetido", str(w), re.I)]
        reasons: list[str] = []
        matches: list[str] = []
        ph = _phone_key(rec.get("telefono"))
        nm = _name_key(rec.get("nombre"))
        own = str(rec.get("nombre") or "").strip() or "pedido nuevo"

        if ph and ph in phone_seen:
            reasons.append("telefono")
            matches.append(phone_seen[ph])
        if len(nm) >= 3 and nm in name_seen:
            reasons.append("nombre")
            matches.append(name_seen[nm])

        if reasons:
            # uniq reasons preserving order
            uniq = []
            for r in reasons:
                if r not in uniq:
                    uniq.append(r)
            label = " y ".join(uniq)
            warn = f"Pedido repetido ({label})"
            warnings.append(warn)
            ref = matches[0] if matches else "otro pedido"
            notices.append(f"{own} coincide con {ref} por {label}")

        # Registrar para detectar duplicados dentro del mismo lote
        if ph:
            phone_seen.setdefault(ph, own)
        if len(nm) >= 3:
            name_seen.setdefault(nm, own)

        rec["warnings"] = warnings
        rec["incomplete"] = bool(warnings)
        rec["duplicate"] = any("Pedido repetido" in str(w) for w in warnings)
        if "Ubicacion dudosa" in warnings:
            rec["location_uncertain"] = True
        out.append(rec)
    return out, notices


def _validate_records_for_upload(records: list[dict]) -> list[str]:
    issues: list[str] = []
    for i, r in enumerate(records, start=1):
        if r.get("upload_status") == "success":
            continue
        phone = re.sub(r"\D", "", str(r.get("telefono") or ""))
        if len(phone) < 8:
            issues.append(f"#{i}: telefono invalido")
        if not str(r.get("direccion") or "").strip():
            issues.append(f"#{i}: falta direccion")
        if not str(r.get("departamento") or "").strip():
            issues.append(f"#{i}: falta departamento")
        if not str(r.get("municipio") or "").strip():
            issues.append(f"#{i}: falta municipio")
        if r.get("location_uncertain") or r.get("incomplete"):
            issues.append(f"#{i}: revisar (incompleto/ubicacion dudosa)")
    return issues


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/ubicaciones")
def ubicaciones() -> dict:
    return locations_for_ui()


@app.get("/api/ubicaciones/forza")
def ubicaciones_forza() -> dict:
    return forza_locations_for_ui()


@app.get("/api/stats/month")
def stats_month(year: int | None = None, month: int | None = None) -> dict:
    today = date.today()
    y = int(year or today.year)
    m = int(month or today.month)
    try:
        return month_sales_stats(y, m)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/meta")
def meta() -> dict:
    migrate_flat_pedidos_to_months()
    settings = ensure_platform_fields(load_settings())
    save_settings(settings)
    today = date.today()
    return {
        "today": today.isoformat(),
        "default_fecha": today.isoformat(),
        "fecha_label": format_fecha_es(today.isoformat()),
        "default_entrega": default_delivery_date(today),
        "settings": public_settings(settings),
        "default_fields_sistrack": DEFAULT_FIELDS,
        "default_fields_forza": DEFAULT_FIELDS_FORZA,
        "product_keywords": load_product_keywords(),
        "upload": runner.status_snapshot(),
    }


@app.get("/api/day/{fecha}")
def get_day(fecha: str, zona: str = "dept") -> dict:
    data = load_day(fecha, zona)
    day = data.get("fecha") or fecha
    return {
        **data,
        "fecha_label": format_fecha_es(day),
        "zona": data.get("zona") or zona,
        "upload": runner.status_snapshot(),
    }


@app.post("/api/day")
def post_day(body: SaveIn) -> dict:
    payload = save_day(body.fecha, body.records, body.zona)
    fields = active_fields(load_settings()) or DEFAULT_FIELDS
    path = export_excel(body.fecha, body.records, fields, body.zona)
    return {**payload, "excel": str(path), "fecha_label": format_fecha_es(body.fecha)}


@app.post("/api/chat/preview")
def chat_preview(body: ChatIn) -> dict:
    entrega = default_delivery_date()
    settings = ensure_platform_fields(load_settings())
    platform = str(settings.get("upload_platform") or "sistrack").strip().lower()
    parsed = parse_order_text(
        body.message, default_delivery=entrega, platform=platform
    )
    if not parsed:
        return {
            "ok": False,
            "reply": "No pude detectar un pedido. Incluye nombre, telefono y direccion.",
            "preview": [],
        }
    data = load_day(body.fecha, body.zona)
    existing = list(data.get("records") or [])
    preview = _number_records(existing, parsed)
    for rec in preview:
        if not rec.get("fecha_entrega"):
            rec["fecha_entrega"] = entrega
    preview, dup_notices = _flag_duplicate_records(existing, preview)
    warn_n = sum(1 for r in preview if r.get("incomplete"))
    dup_n = sum(1 for r in preview if r.get("duplicate"))
    plat_label = "Forza" if platform == "forza" else "Express"
    reply = (
        f"Vista previa ({plat_label}): {len(preview)} pedido(s)"
        + (f", {warn_n} con avisos" if warn_n else "")
        + (f", {dup_n} repetido(s)" if dup_n else "")
        + f". Entrega tipica: {format_fecha_es(entrega)}."
    )
    if platform == "forza":
        with_poblado = sum(1 for r in preview if (r.get("colonia") or "").strip())
        reply += f" Poblado Forza: {with_poblado}/{len(preview)}."
    if dup_notices:
        reply += " Posible duplicado: " + "; ".join(dup_notices[:5])
        if len(dup_notices) > 5:
            reply += f" (+{len(dup_notices) - 5} mas)"
    return {
        "ok": True,
        "reply": reply,
        "preview": preview,
        "entrega": entrega,
        "duplicates": dup_notices,
        "platform": platform,
    }


@app.post("/api/chat/confirm")
def chat_confirm(body: ConfirmChatIn) -> dict:
    data = load_day(body.fecha, body.zona)
    records = list(data.get("records") or [])
    added = body.records or []
    if not added:
        return {"ok": False, "reply": "No hay pedidos para confirmar.", "records": records, "added": 0}
    added, dup_notices = _flag_duplicate_records(records, added)
    records.extend(added)
    save_day(body.fecha, records, body.zona)
    export_excel(body.fecha, records, active_fields(load_settings()) or DEFAULT_FIELDS, body.zona)
    names = ", ".join(r.get("nombre", "?") for r in added)
    reply = f"Agregados {len(added)} pedido(s): {names}"
    if dup_notices:
        reply += f". Atencion: {len(dup_notices)} posible(s) duplicado(s)."
    return {
        "ok": True,
        "reply": reply,
        "records": records,
        "added": len(added),
        "duplicates": dup_notices,
    }


@app.post("/api/chat")
def chat(body: ChatIn) -> dict:
    prev = chat_preview(body)
    if not prev.get("ok"):
        data = load_day(body.fecha, body.zona)
        return {**prev, "records": data.get("records") or [], "added": 0}
    return chat_confirm(ConfirmChatIn(fecha=body.fecha, records=prev["preview"], zona=body.zona))


@app.get("/api/settings")
def get_settings() -> dict:
    return public_settings(load_settings())


@app.post("/api/settings")
def post_settings(body: SettingsIn) -> dict:
    settings = ensure_platform_fields(load_settings())
    if body.upload_platform is not None:
        plat = body.upload_platform.strip().lower()
        settings["upload_platform"] = "forza" if plat == "forza" else "sistrack"
    if body.fields_sistrack is not None:
        settings["fields_sistrack"] = body.fields_sistrack
    if body.fields_forza is not None:
        settings["fields_forza"] = body.fields_forza
    if body.fields is not None:
        # Compat: el set activo de la plataforma seleccionada
        if settings.get("upload_platform") == "forza":
            settings["fields_forza"] = body.fields
        else:
            settings["fields_sistrack"] = body.fields
    if body.theme is not None:
        settings["theme"] = body.theme
    if body.view is not None:
        settings["view"] = body.view
    if body.sistrack_email is not None:
        settings["sistrack_email"] = body.sistrack_email.strip()
    if body.sistrack_password is not None and body.sistrack_password != "":
        settings["sistrack_password"] = body.sistrack_password
    if body.forza_codigo is not None:
        settings["forza_codigo"] = body.forza_codigo.strip()
    if body.forza_usuario is not None:
        settings["forza_usuario"] = body.forza_usuario.strip()
    if body.forza_password is not None and body.forza_password != "":
        settings["forza_password"] = body.forza_password
    if body.upload_headless is not None:
        settings["upload_headless"] = bool(body.upload_headless)
    if body.upload_dry_run is not None:
        settings["upload_dry_run"] = bool(body.upload_dry_run)
    settings = ensure_platform_fields(settings)
    save_settings(settings)
    return public_settings(settings)


@app.post("/api/export/{fecha}")
def export_day(fecha: str, zona: str = "dept") -> dict:
    data = load_day(fecha, zona)
    path = export_excel(
        fecha, data.get("records") or [], active_fields(load_settings()) or DEFAULT_FIELDS, zona
    )
    return {"ok": True, "path": str(path), "filename": path.name}


@app.get("/api/export/{fecha}/download")
def download_excel(fecha: str, zona: str = "dept"):
    data = load_day(fecha, zona)
    path = export_excel(
        fecha, data.get("records") or [], active_fields(load_settings()) or DEFAULT_FIELDS, zona
    )
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/upload/validate")
def upload_validate(body: UploadIn) -> dict:
    data = load_day(body.fecha, body.zona)
    records = list(data.get("records") or [])
    pending = [r for r in records if r.get("upload_status") != "success"]
    issues = _validate_records_for_upload(records)
    return {"ok": len(issues) == 0, "pending": len(pending), "issues": issues}


@app.post("/api/upload/start")
def upload_start(body: UploadIn) -> dict:
    if normalize_zona(body.zona) == "ss":
        raise HTTPException(400, "San Salvador no se sube al sistema")
    if runner.running:
        raise HTTPException(400, "Ya hay una subida en curso")
    data = load_day(body.fecha, body.zona)
    records = list(data.get("records") or [])
    if not records:
        raise HTTPException(400, "No hay registros para subir")

    pending = [r for r in records if r.get("upload_status") != "success"]
    if not pending:
        return {"ok": True, "message": "Todos ya estan subidos", "records": records}

    issues = _validate_records_for_upload(records)
    start = body.start_index
    if start is None:
        start = runner.paused_at if runner.paused_at is not None else 0
        if runner.paused_at is None:
            for i, r in enumerate(records):
                if r.get("upload_status") != "success":
                    start = i
                    break

    def on_progress(index: int, status: str, error: str, meta: dict | None = None) -> None:
        update_record_status(body.fecha, index, status, error, body.zona)

    settings = load_settings()
    platform = str(settings.get("upload_platform") or "sistrack").strip().lower()
    if platform == "forza":
        codigo = (settings.get("forza_codigo") or "").strip()
        usuario = (settings.get("forza_usuario") or "").strip()
        password = settings.get("forza_password") or ""
        if not codigo or not usuario or not password:
            raise HTTPException(
                400, "Configura código, usuario y contraseña de Forza en Ajustes"
            )
        runner.start(
            records,
            start,
            on_progress,
            lambda: None,
            headless=bool(settings.get("upload_headless")),
            dry_run=bool(settings.get("upload_dry_run")),
            platform="forza",
            forza_codigo=codigo,
            forza_usuario=usuario,
            forza_password=password,
        )
    else:
        email = (settings.get("sistrack_email") or "").strip()
        password = settings.get("sistrack_password") or ""
        if not email or not password:
            raise HTTPException(400, "Configura email y contraseña de Sistrack en Ajustes")
        runner.start(
            records,
            start,
            on_progress,
            lambda: None,
            email=email,
            password=password,
            headless=bool(settings.get("upload_headless")),
            dry_run=bool(settings.get("upload_dry_run")),
            platform="sistrack",
        )
    return {
        "ok": True,
        "started_at": start,
        "running": True,
        "platform": "forza" if platform == "forza" else "sistrack",
        "warnings": issues,
        **runner.status_snapshot(),
    }


@app.get("/api/upload/status")
def upload_status(fecha: str, zona: str = "dept") -> dict:
    data = load_day(fecha, zona)
    return {**runner.status_snapshot(), "records": data.get("records") or []}


@app.post("/api/upload/stop")
def upload_stop() -> dict:
    runner.stop()
    return {"ok": True, **runner.status_snapshot()}


def _inv_error(exc: InventarioError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _cob_error(exc: CobroError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/api/inventario")
def inventario_resumen() -> dict:
    return snapshot()


@app.get("/api/inventario/productos")
def inventario_productos(
    buscar: str = "",
    categoria: int | None = None,
    tipo: str = "",
    estado: str = "",
    stock: str = "",
) -> dict:
    return {
        "productos": listar_productos(buscar, categoria, tipo, estado, stock),
        **snapshot(),
    }


@app.post("/api/inventario/productos")
def inventario_crear_producto(body: ProductoIn) -> dict:
    try:
        producto = crear_producto(body.dict())
        return {"ok": True, "producto": producto}
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.put("/api/inventario/productos/{producto_id}")
def inventario_actualizar_producto(producto_id: int, body: ProductoIn) -> dict:
    try:
        producto = actualizar_producto(producto_id, body.dict())
        return {"ok": True, "producto": producto}
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.delete("/api/inventario/productos/{producto_id}")
def inventario_eliminar_producto(producto_id: int) -> dict:
    try:
        return eliminar_producto(producto_id)
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.get("/api/inventario/productos/{producto_id}/kardex")
def inventario_kardex(producto_id: int) -> dict:
    try:
        return kardex(producto_id)
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.post("/api/inventario/entradas")
def inventario_entrada(body: MovimientoIn) -> dict:
    try:
        mov = registrar_entrada(
            None,
            body.id_producto,
            float(body.cantidad or 0),
            costo_unitario=body.costo_unitario,
            referencia=body.referencia,
            motivo=body.motivo,
            usuario=body.usuario,
        )
        return {"ok": True, "movimiento": mov, **snapshot()}
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.post("/api/inventario/salidas")
def inventario_salida(body: MovimientoIn) -> dict:
    try:
        mov = registrar_salida(
            body.id_producto,
            float(body.cantidad or 0),
            referencia=body.referencia,
            motivo=body.motivo,
            usuario=body.usuario,
        )
        return {"ok": True, "movimiento": mov, **snapshot()}
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.post("/api/inventario/ajustes")
def inventario_ajuste(body: MovimientoIn) -> dict:
    if body.stock_nuevo is None:
        raise HTTPException(400, detail="Indica el stock físico real.")
    try:
        mov = ajustar_stock(
            None,
            body.id_producto,
            float(body.stock_nuevo),
            motivo=body.motivo,
            usuario=body.usuario,
        )
        return {"ok": True, "movimiento": mov, **snapshot()}
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.post("/api/inventario/categorias")
def inventario_crear_categoria(body: CategoriaIn) -> dict:
    try:
        return {"ok": True, "categoria": crear_categoria(body.nombre, body.descripcion)}
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.put("/api/inventario/categorias/{categoria_id}")
def inventario_actualizar_categoria(categoria_id: int, body: CategoriaIn) -> dict:
    try:
        return {
            "ok": True,
            "categoria": actualizar_categoria(categoria_id, body.nombre, body.descripcion),
        }
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.delete("/api/inventario/categorias/{categoria_id}")
def inventario_eliminar_categoria(categoria_id: int) -> dict:
    try:
        return eliminar_categoria(categoria_id)
    except InventarioError as exc:
        raise _inv_error(exc) from exc


@app.get("/api/cobros")
def cobros_resumen(buscar: str = "", estado: str = "", fecha: str = "") -> dict:
    return cobros_snapshot(buscar, estado, fecha)


@app.post("/api/cobros")
def cobros_crear(body: CobroIn) -> dict:
    try:
        cobro = registrar_cobro(body.dict())
        return {"ok": True, "cobro": cobro, **cobros_snapshot()}
    except CobroError as exc:
        raise _cob_error(exc) from exc


@app.get("/api/cobros/{cobro_id}")
def cobros_detalle(cobro_id: int) -> dict:
    try:
        return detalle_cobro(cobro_id)
    except CobroError as exc:
        raise _cob_error(exc) from exc


@app.middleware("http")
async def no_cache_ui_assets(request, call_next):
    response = await call_next(request)
    path = (request.url.path or "").lower()
    if path.endswith((".html", ".css", ".js")) or path in ("/", "/index.html", "/inventario", "/cobros"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/inventario")
def inventario_page():
    return FileResponse(STATIC / "inventario.html")


@app.get("/cobros")
def cobros_page():
    return FileResponse(STATIC / "cobros.html")


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.app:app", host="127.0.0.1", port=8787, reload=False)
