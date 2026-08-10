# -*- coding: utf-8 -*-
"""Order Track — API local FastAPI."""
from __future__ import annotations

import re
import sys
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

from ubicaciones import locations_for_ui
from web.parser import DEFAULT_FIELDS, parse_order_text
from web.store import (
    archive_old_days,
    default_delivery_date,
    export_excel,
    format_fecha_es,
    load_day,
    load_product_keywords,
    load_settings,
    public_settings,
    save_day,
    save_settings,
    update_record_status,
)
from web.upload_runner import UploadRunner

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


class ConfirmChatIn(BaseModel):
    fecha: str
    records: list[dict[str, Any]]


class SaveIn(BaseModel):
    fecha: str
    records: list[dict[str, Any]]


class SettingsIn(BaseModel):
    fields: list[dict[str, Any]] | None = None
    theme: str | None = None
    view: str | None = None
    sistrack_email: str | None = None
    sistrack_password: str | None = None
    upload_headless: bool | None = None
    upload_dry_run: bool | None = None


class UploadIn(BaseModel):
    fecha: str
    start_index: int | None = None


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


@app.get("/api/meta")
def meta() -> dict:
    settings = load_settings()
    if not settings.get("fields"):
        settings["fields"] = DEFAULT_FIELDS
        save_settings(settings)
    today = date.today()
    return {
        "today": today.isoformat(),
        "default_fecha": today.isoformat(),
        "fecha_label": format_fecha_es(today.isoformat()),
        "default_entrega": default_delivery_date(today),
        "settings": public_settings(settings),
        "product_keywords": load_product_keywords(),
        "upload": runner.status_snapshot(),
    }


@app.get("/api/day/{fecha}")
def get_day(fecha: str) -> dict:
    data = load_day(fecha)
    return {
        **data,
        "fecha_label": format_fecha_es(fecha),
        "upload": runner.status_snapshot(),
    }


@app.post("/api/day")
def post_day(body: SaveIn) -> dict:
    payload = save_day(body.fecha, body.records)
    settings = load_settings()
    fields = settings.get("fields") or DEFAULT_FIELDS
    path = export_excel(body.fecha, body.records, fields)
    return {**payload, "excel": str(path), "fecha_label": format_fecha_es(body.fecha)}


@app.post("/api/chat/preview")
def chat_preview(body: ChatIn) -> dict:
    entrega = default_delivery_date()
    parsed = parse_order_text(body.message, default_delivery=entrega)
    if not parsed:
        return {
            "ok": False,
            "reply": "No pude detectar un pedido. Incluye nombre, telefono y direccion.",
            "preview": [],
        }
    data = load_day(body.fecha)
    preview = _number_records(list(data.get("records") or []), parsed)
    for rec in preview:
        if not rec.get("fecha_entrega"):
            rec["fecha_entrega"] = entrega
    warn_n = sum(1 for r in preview if r.get("incomplete"))
    return {
        "ok": True,
        "reply": f"Vista previa: {len(preview)} pedido(s)"
        + (f", {warn_n} con avisos" if warn_n else "")
        + f". Entrega tipica: {format_fecha_es(entrega)}.",
        "preview": preview,
        "entrega": entrega,
    }


@app.post("/api/chat/confirm")
def chat_confirm(body: ConfirmChatIn) -> dict:
    data = load_day(body.fecha)
    records = list(data.get("records") or [])
    added = body.records or []
    if not added:
        return {"ok": False, "reply": "No hay pedidos para confirmar.", "records": records, "added": 0}
    records.extend(added)
    save_day(body.fecha, records)
    settings = load_settings()
    export_excel(body.fecha, records, settings.get("fields") or DEFAULT_FIELDS)
    names = ", ".join(r.get("nombre", "?") for r in added)
    return {
        "ok": True,
        "reply": f"Agregados {len(added)} pedido(s): {names}",
        "records": records,
        "added": len(added),
    }


@app.post("/api/chat")
def chat(body: ChatIn) -> dict:
    prev = chat_preview(body)
    if not prev.get("ok"):
        data = load_day(body.fecha)
        return {**prev, "records": data.get("records") or [], "added": 0}
    return chat_confirm(ConfirmChatIn(fecha=body.fecha, records=prev["preview"]))


@app.get("/api/settings")
def get_settings() -> dict:
    settings = load_settings()
    if not settings.get("fields"):
        settings["fields"] = DEFAULT_FIELDS
    return public_settings(settings)


@app.post("/api/settings")
def post_settings(body: SettingsIn) -> dict:
    settings = load_settings()
    if body.fields is not None:
        settings["fields"] = body.fields
    if body.theme is not None:
        settings["theme"] = body.theme
    if body.view is not None:
        settings["view"] = body.view
    if body.sistrack_email is not None:
        settings["sistrack_email"] = body.sistrack_email.strip()
    if body.sistrack_password is not None and body.sistrack_password != "":
        settings["sistrack_password"] = body.sistrack_password
    if body.upload_headless is not None:
        settings["upload_headless"] = bool(body.upload_headless)
    if body.upload_dry_run is not None:
        settings["upload_dry_run"] = bool(body.upload_dry_run)
    save_settings(settings)
    return public_settings(settings)


@app.post("/api/export/{fecha}")
def export_day(fecha: str) -> dict:
    data = load_day(fecha)
    settings = load_settings()
    path = export_excel(fecha, data.get("records") or [], settings.get("fields") or DEFAULT_FIELDS)
    return {"ok": True, "path": str(path), "filename": path.name}


@app.get("/api/export/{fecha}/download")
def download_excel(fecha: str):
    data = load_day(fecha)
    settings = load_settings()
    path = export_excel(fecha, data.get("records") or [], settings.get("fields") or DEFAULT_FIELDS)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/upload/validate")
def upload_validate(body: UploadIn) -> dict:
    data = load_day(body.fecha)
    records = list(data.get("records") or [])
    pending = [r for r in records if r.get("upload_status") != "success"]
    issues = _validate_records_for_upload(records)
    return {"ok": len(issues) == 0, "pending": len(pending), "issues": issues}


@app.post("/api/upload/start")
def upload_start(body: UploadIn) -> dict:
    if runner.running:
        raise HTTPException(400, "Ya hay una subida en curso")
    data = load_day(body.fecha)
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
        update_record_status(body.fecha, index, status, error)

    settings = load_settings()
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
    )
    return {
        "ok": True,
        "started_at": start,
        "running": True,
        "warnings": issues,
        **runner.status_snapshot(),
    }


@app.get("/api/upload/status")
def upload_status(fecha: str) -> dict:
    data = load_day(fecha)
    return {**runner.status_snapshot(), "records": data.get("records") or []}


@app.post("/api/upload/stop")
def upload_stop() -> dict:
    runner.stop()
    return {"ok": True, **runner.status_snapshot()}


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.app:app", host="127.0.0.1", port=8787, reload=False)
