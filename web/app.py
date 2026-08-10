# -*- coding: utf-8 -*-
"""Order Track — API local FastAPI."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ubicaciones import locations_for_ui
from web.parser import DEFAULT_FIELDS, parse_order_text
from web.store import (
    default_delivery_date,
    export_excel,
    format_fecha_es,
    load_day,
    load_settings,
    save_day,
    save_settings,
)
from web.upload_runner import UploadRunner
from cargar_pedidos_sistrack import EMAIL as DEFAULT_EMAIL, PASSWORD as DEFAULT_PASSWORD

app = FastAPI(title="Order Track")
runner = UploadRunner()
STATIC = Path(__file__).parent / "static"


class ChatIn(BaseModel):
    message: str
    fecha: str


class SaveIn(BaseModel):
    fecha: str
    records: list[dict[str, Any]]


class SettingsIn(BaseModel):
    fields: list[dict[str, Any]] | None = None
    theme: str | None = None
    view: str | None = None
    sistrack_email: str | None = None
    sistrack_password: str | None = None


class UploadIn(BaseModel):
    fecha: str
    start_index: int | None = None


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/ubicaciones")
def ubicaciones() -> dict:
    """Catalogo depto -> municipios (mismo que usa la carga a Sistrack)."""
    return locations_for_ui()


def _ensure_credentials(settings: dict) -> dict:
    if "sistrack_email" not in settings:
        settings["sistrack_email"] = DEFAULT_EMAIL
    elif not str(settings.get("sistrack_email") or "").strip():
        settings["sistrack_email"] = DEFAULT_EMAIL
    if "sistrack_password" not in settings:
        settings["sistrack_password"] = DEFAULT_PASSWORD
    return settings


@app.get("/api/meta")
def meta() -> dict:
    settings = load_settings()
    if not settings.get("fields"):
        settings["fields"] = DEFAULT_FIELDS
    settings = _ensure_credentials(settings)
    save_settings(settings)
    today = date.today()
    return {
        "today": today.isoformat(),
        "default_fecha": today.isoformat(),
        "fecha_label": format_fecha_es(today.isoformat()),
        "default_entrega": default_delivery_date(today),
        "settings": settings,
        "upload": {
            "running": runner.running,
            "paused_at": runner.paused_at,
            "last_error": runner.last_error,
        },
    }


@app.get("/api/day/{fecha}")
def get_day(fecha: str) -> dict:
    data = load_day(fecha)
    return {
        **data,
        "fecha_label": format_fecha_es(fecha),
        "upload": {
            "running": runner.running,
            "paused_at": runner.paused_at,
            "last_error": runner.last_error,
        },
    }


@app.post("/api/day")
def post_day(body: SaveIn) -> dict:
    payload = save_day(body.fecha, body.records)
    settings = load_settings()
    fields = settings.get("fields") or DEFAULT_FIELDS
    path = export_excel(body.fecha, body.records, fields)
    return {**payload, "excel": str(path), "fecha_label": format_fecha_es(body.fecha)}


@app.post("/api/chat")
def chat(body: ChatIn) -> dict:
    data = load_day(body.fecha)
    records = list(data.get("records") or [])
    entrega = default_delivery_date()
    parsed = parse_order_text(body.message, default_delivery=entrega)
    if not parsed:
        return {
            "ok": False,
            "reply": "No pude detectar un pedido en el mensaje. Incluye nombre, telefono y direccion.",
            "records": records,
            "added": 0,
        }
    # Numeracion correlativa segun ingresado
    base = len(records)
    for i, rec in enumerate(parsed, start=1):
        num = base + i
        name = rec["nombre"]
        if not name.startswith(f"{num}."):
            # quitar numeracion previa
            name = __import__("re").sub(r"^\d{1,2}[\.\)]\s*", "", name).strip()
            rec["nombre"] = f"{num}. {name}"
        rec["fecha_entrega"] = entrega
        records.append(rec)
    save_day(body.fecha, records)
    settings = load_settings()
    export_excel(body.fecha, records, settings.get("fields") or DEFAULT_FIELDS)
    names = ", ".join(r["nombre"] for r in parsed)
    return {
        "ok": True,
        "reply": f"Registre {len(parsed)} pedido(s): {names}. Entrega: {format_fecha_es(entrega)}.",
        "records": records,
        "added": len(parsed),
    }


@app.get("/api/settings")
def get_settings() -> dict:
    settings = load_settings()
    if not settings.get("fields"):
        settings["fields"] = DEFAULT_FIELDS
    return _ensure_credentials(settings)


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
    if body.sistrack_password is not None:
        settings["sistrack_password"] = body.sistrack_password
    save_settings(settings)
    return settings


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


@app.post("/api/upload/start")
def upload_start(body: UploadIn) -> dict:
    if runner.running:
        raise HTTPException(400, "Ya hay una subida en curso")
    data = load_day(body.fecha)
    records = list(data.get("records") or [])
    if not records:
        raise HTTPException(400, "No hay registros para subir")

    start = body.start_index
    if start is None:
        start = runner.paused_at if runner.paused_at is not None else 0
        # si paused_at apunta a error, reanudar ahi; si no, primer pending
        if runner.paused_at is None:
            for i, r in enumerate(records):
                if r.get("upload_status") != "success":
                    start = i
                    break
            else:
                return {"ok": True, "message": "Todos ya estan subidos", "records": records}

    def on_progress(index: int, status: str, error: str) -> None:
        cur = load_day(body.fecha)
        recs = list(cur.get("records") or [])
        if 0 <= index < len(recs):
            recs[index]["upload_status"] = status
            recs[index]["upload_error"] = error
            save_day(body.fecha, recs)

    def on_done() -> None:
        pass

    settings = _ensure_credentials(load_settings())
    email = (settings.get("sistrack_email") or DEFAULT_EMAIL or "").strip()
    password = settings.get("sistrack_password") or DEFAULT_PASSWORD or ""
    if not email or not password:
        raise HTTPException(400, "Configura email y contraseña de Sistrack en Ajustes")

    runner.start(
        records,
        start,
        on_progress,
        on_done,
        email=email,
        password=password,
    )
    return {"ok": True, "started_at": start, "running": True}


@app.get("/api/upload/status")
def upload_status(fecha: str) -> dict:
    data = load_day(fecha)
    return {
        "running": runner.running,
        "paused_at": runner.paused_at,
        "last_error": runner.last_error,
        "records": data.get("records") or [],
    }


@app.post("/api/upload/stop")
def upload_stop() -> dict:
    runner.stop()
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.app:app", host="127.0.0.1", port=8787, reload=True)
