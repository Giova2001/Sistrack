# -*- coding: utf-8 -*-
"""Persistencia JSON + Excel por fecha."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "fields": None,  # se llena desde parser.DEFAULT_FIELDS
    "theme": "light",
    "view": "lista",
    "sistrack_email": "",
    "sistrack_password": "",
}


def _date_key(fecha: str) -> str:
    # YYYY-MM-DD
    return fecha.strip()


def json_path(fecha: str) -> Path:
    return DATA_DIR / f"pedidos_{_date_key(fecha)}.json"


def excel_path(fecha: str) -> Path:
    return DATA_DIR / f"pedidos_{_date_key(fecha)}.xlsx"


def load_settings() -> dict[str, Any]:
    if SETTINGS_PATH.exists():
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_day(fecha: str) -> dict[str, Any]:
    path = json_path(fecha)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"fecha": fecha, "records": [], "updated_at": None}


def save_day(fecha: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "fecha": fecha,
        "records": records,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    json_path(fecha).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def export_excel(fecha: str, records: list[dict[str, Any]], fields: list[dict]) -> Path:
    """Crea/actualiza Excel nombrado con la fecha."""
    enabled = [f for f in fields if f.get("enabled", True)]
    headers = [f["label"] for f in enabled] + ["Estado subida", "Error"]
    keys = [f["key"] for f in enabled]

    path = excel_path(fecha)
    wb = Workbook()
    ws = wb.active
    ws.title = "Pedidos"
    ws.append(headers)
    for i, rec in enumerate(records, start=1):
        row = []
        for k in keys:
            val = rec.get(k, "")
            if k == "nombre" and val and not str(val).strip().startswith(f"{i}."):
                # numeracion visual en excel opcional: dejar nombre tal cual
                pass
            row.append(val)
        row.append(rec.get("upload_status", "pending"))
        row.append(rec.get("upload_error", ""))
        ws.append(row)
    wb.save(path)
    return path


def default_delivery_date(from_day: date | None = None) -> str:
    """Hoy + 2 dias; si cae domingo, pasa a lunes."""
    d = (from_day or date.today()) + timedelta(days=2)
    if d.weekday() == 6:  # domingo
        d += timedelta(days=1)
    return d.isoformat()


def format_fecha_es(fecha: str) -> str:
    """2026-08-09 -> Domingo 9 de agosto del 2026"""
    dt = datetime.strptime(fecha, "%Y-%m-%d")
    dias = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    # Monday=0
    return f"{dias[dt.weekday()]} {dt.day} de {meses[dt.month - 1]} del {dt.year}"
