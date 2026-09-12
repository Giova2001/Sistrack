# -*- coding: utf-8 -*-
"""Persistencia JSON + Excel por fecha (escritura atomica + lock)."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ARCHIVE_DIR = DATA_DIR / "archivo"
SETTINGS_PATH = DATA_DIR / "settings.json"
PRODUCT_KEYWORDS_PATH = DATA_DIR / "product_keywords.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

_IO_LOCK = threading.RLock()

DEFAULT_SETTINGS = {
    "fields": None,
    "fields_sistrack": None,
    "fields_forza": None,
    "theme": "light",
    "view": "lista",
    "sistrack_email": "",
    "sistrack_password": "",
    "upload_platform": "sistrack",
    "forza_codigo": "",
    "forza_usuario": "",
    "forza_password": "",
    "upload_headless": False,
    "upload_dry_run": False,
}

DEFAULT_PRODUCT_KEYWORDS = [
    "casio", "seiko", "wood", "aviador", "ray-ban", "rayban", "old money",
    "retro", "rose gold", "mrw", "mtp", "ltp", "qq ", "f105", "classic",
    "vintage", "luxury", "kit", "lente", "lentes", "promocion", "promoción",
    "2x1", "producto", "contenido", "reloj", "gafas",
]


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def normalize_zona(zona: str | None) -> str:
    z = str(zona or "dept").strip().lower().replace(" ", "")
    if z in ("ss", "sansalvador", "san_salvador"):
        return "ss"
    return "dept"


def split_fecha_zona(fecha: str, zona: str | None = None) -> tuple[str, str]:
    """Devuelve (YYYY-MM-DD, dept|ss). Acepta clave ya sufijada 2026-08-10_SS."""
    f = (fecha or "").strip()
    if f.upper().endswith("_SS"):
        return f[:-3], "ss"
    return f, normalize_zona(zona)


def day_key(fecha: str, zona: str | None = None) -> str:
    day, z = split_fecha_zona(fecha, zona)
    return f"{day}_SS" if z == "ss" else day


def month_dir(fecha: str, zona: str | None = None) -> Path:
    """Carpeta data/YYYY/MM/ para el día del pedido."""
    day, _ = split_fecha_zona(fecha, zona)
    path = DATA_DIR / day[0:4] / day[5:7]
    path.mkdir(parents=True, exist_ok=True)
    return path


def json_path(fecha: str, zona: str | None = None) -> Path:
    return month_dir(fecha, zona) / f"pedidos_{day_key(fecha, zona)}.json"


def excel_path(fecha: str, zona: str | None = None) -> Path:
    return month_dir(fecha, zona) / f"pedidos_{day_key(fecha, zona)}.xlsx"


def legacy_json_path(fecha: str, zona: str | None = None) -> Path:
    """Ruta plana antigua: data/pedidos_YYYY-MM-DD.json"""
    return DATA_DIR / f"pedidos_{day_key(fecha, zona)}.json"


def legacy_excel_path(fecha: str, zona: str | None = None) -> Path:
    return DATA_DIR / f"pedidos_{day_key(fecha, zona)}.xlsx"


def _is_under_archivo(path: Path) -> bool:
    try:
        return "archivo" in path.resolve().relative_to(DATA_DIR.resolve()).parts
    except ValueError:
        return "archivo" in path.parts


def iter_pedido_json_paths(
    year: int | None = None, month: int | None = None
) -> list[Path]:
    """Lista pedidos_*.json (excluye data/archivo). Un archivo por día (prioriza carpeta mes)."""
    with _IO_LOCK:
        if year is not None and month is not None:
            base = DATA_DIR / f"{year:04d}" / f"{month:02d}"
            paths = list(base.glob("pedidos_*.json")) if base.exists() else []
            paths.extend(DATA_DIR.glob(f"pedidos_{year:04d}-{month:02d}-*.json"))
        else:
            paths = [
                p
                for p in DATA_DIR.rglob("pedidos_*.json")
                if p.is_file() and not _is_under_archivo(p)
            ]
        by_stem: dict[str, Path] = {}
        for p in paths:
            stem = p.stem
            prev = by_stem.get(stem)
            if prev is None:
                by_stem[stem] = p
                continue
            p_month = _is_month_layout(p)
            prev_month = _is_month_layout(prev)
            if p_month and not prev_month:
                by_stem[stem] = p
            elif prev_month and not p_month:
                continue
            elif _record_count(p) > _record_count(prev):
                by_stem[stem] = p
        return sorted(by_stem.values())


def migrate_flat_pedidos_to_months() -> int:
    """Mueve data/pedidos_* planos a data/YYYY/MM/, conservando el JSON con más pedidos."""
    moved = 0
    with _IO_LOCK:
        for path in list(DATA_DIR.glob("pedidos_*.*")):
            if not path.is_file():
                continue
            m = re.match(r"pedidos_(\d{4}-\d{2}-\d{2})(?:_SS)?$", path.stem)
            if not m:
                continue
            day = m.group(1)
            dest = month_dir(day) / path.name
            if dest.resolve() == path.resolve():
                continue
            if dest.exists():
                if path.suffix.lower() == ".json":
                    # No borrar pedidos reales por un plano vacío del servidor viejo
                    try:
                        flat_n = len(
                            (json.loads(path.read_text(encoding="utf-8")).get("records") or [])
                        )
                        dest_n = len(
                            (json.loads(dest.read_text(encoding="utf-8")).get("records") or [])
                        )
                    except Exception:
                        flat_n, dest_n = 0, 1
                    if flat_n > dest_n:
                        dest.unlink()
                        path.replace(dest)
                    else:
                        path.unlink()
                else:
                    path.unlink()
                moved += 1
                continue
            path.replace(dest)
            moved += 1
    return moved


def _record_count(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data.get("records") or [])
    except Exception:
        return -1


def _is_month_layout(path: Path) -> bool:
    """True si está en data/YYYY/MM/archivo."""
    try:
        rel = path.resolve().relative_to(DATA_DIR.resolve()).parts
    except ValueError:
        return False
    return len(rel) >= 3 and rel[0].isdigit() and len(rel[0]) == 4 and rel[1].isdigit()


def _merge_settings_defaults(data: dict[str, Any] | None = None) -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    if isinstance(data, dict):
        out.update(data)
    plat = str(out.get("upload_platform") or "sistrack").strip().lower()
    out["upload_platform"] = "forza" if plat == "forza" else "sistrack"
    return out


def ensure_platform_fields(settings: dict[str, Any]) -> dict[str, Any]:
    """Garantiza fields_sistrack / fields_forza y sincroniza `fields` activo."""
    from web.parser import (
        DEFAULT_FIELDS,
        DEFAULT_FIELDS_FORZA,
        prune_forza_field_defs,
    )

    s = dict(settings or {})
    legacy = s.get("fields")
    if not isinstance(s.get("fields_sistrack"), list) or not s.get("fields_sistrack"):
        if isinstance(legacy, list) and legacy:
            s["fields_sistrack"] = legacy
        else:
            s["fields_sistrack"] = [dict(f) for f in DEFAULT_FIELDS]
    if not isinstance(s.get("fields_forza"), list) or not s.get("fields_forza"):
        s["fields_forza"] = [dict(f) for f in DEFAULT_FIELDS_FORZA]
    else:
        # Quitar fecha/grabado/emergencia aunque estuvieran guardados
        pruned = prune_forza_field_defs(s["fields_forza"])
        if not pruned:
            pruned = [dict(f) for f in DEFAULT_FIELDS_FORZA]
        else:
            # Actualizar etiquetas canónicas sin perder enabled/orden
            label_by_key = {
                str(f.get("key")): str(f.get("label") or "")
                for f in DEFAULT_FIELDS_FORZA
            }
            for f in pruned:
                k = str(f.get("key") or "")
                if k in label_by_key and label_by_key[k]:
                    f["label"] = label_by_key[k]
            # Insertar campos nuevos del default que falten (p. ej. devolucion)
            have = {str(f.get("key") or "") for f in pruned}
            for i, f in enumerate(DEFAULT_FIELDS_FORZA):
                k = str(f.get("key") or "")
                if not k or k in have:
                    continue
                # Insertar cerca de la posición canónica
                insert_at = min(i, len(pruned))
                # Preferir después de pagado si existe
                if k == "devolucion":
                    for j, cur in enumerate(pruned):
                        if str(cur.get("key") or "") == "pagado":
                            insert_at = j + 1
                            break
                pruned.insert(insert_at, dict(f))
                have.add(k)
        s["fields_forza"] = pruned
    plat = str(s.get("upload_platform") or "sistrack").strip().lower()
    s["upload_platform"] = "forza" if plat == "forza" else "sistrack"
    s["fields"] = (
        s["fields_forza"] if s["upload_platform"] == "forza" else s["fields_sistrack"]
    )
    return s


def active_fields(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    s = ensure_platform_fields(settings or load_settings())
    fields = s.get("fields")
    return list(fields) if isinstance(fields, list) else []


def load_settings() -> dict[str, Any]:
    with _IO_LOCK:
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            return ensure_platform_fields(
                _merge_settings_defaults(data if isinstance(data, dict) else {})
            )
        return ensure_platform_fields(_merge_settings_defaults())


def save_settings(settings: dict[str, Any]) -> None:
    with _IO_LOCK:
        payload = ensure_platform_fields(
            _merge_settings_defaults(settings if isinstance(settings, dict) else {})
        )
        _atomic_write_text(
            SETTINGS_PATH,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )


def public_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Settings seguros para la API (sin contraseña en claro)."""
    s = ensure_platform_fields(dict(settings or load_settings()))
    has_pwd = bool(str(s.get("sistrack_password") or "").strip())
    s["sistrack_password_set"] = has_pwd
    s["sistrack_password"] = ""  # nunca exponer
    forza_pwd = bool(str(s.get("forza_password") or "").strip())
    s["forza_password_set"] = forza_pwd
    s["forza_password"] = ""
    plat = str(s.get("upload_platform") or "sistrack").strip().lower()
    s["upload_platform"] = "forza" if plat == "forza" else "sistrack"
    s["fields"] = (
        s["fields_forza"] if s["upload_platform"] == "forza" else s["fields_sistrack"]
    )
    return s


def load_product_keywords() -> list[str]:
    with _IO_LOCK:
        if PRODUCT_KEYWORDS_PATH.exists():
            try:
                data = json.loads(PRODUCT_KEYWORDS_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return [str(x).strip().lower() for x in data if str(x).strip()]
            except Exception:
                pass
        _atomic_write_text(
            PRODUCT_KEYWORDS_PATH,
            json.dumps(DEFAULT_PRODUCT_KEYWORDS, ensure_ascii=False, indent=2),
        )
        return list(DEFAULT_PRODUCT_KEYWORDS)


def load_day(fecha: str, zona: str | None = None) -> dict[str, Any]:
    day, z = split_fecha_zona(fecha, zona)
    with _IO_LOCK:
        path = json_path(day, z)
        legacy = legacy_json_path(day, z)
        if legacy.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                legacy.replace(path)
            else:
                # Servidor viejo pudo crear un plano vacío encima de datos reales
                if _record_count(legacy) > _record_count(path):
                    path.unlink()
                    legacy.replace(path)
                else:
                    legacy.unlink()
            xlsx_legacy = legacy_excel_path(day, z)
            xlsx_new = excel_path(day, z)
            if xlsx_legacy.exists():
                if not xlsx_new.exists():
                    xlsx_legacy.replace(xlsx_new)
                else:
                    xlsx_legacy.unlink()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("fecha", day)
            data.setdefault("zona", z)
            return data
        return {"fecha": day, "zona": z, "records": [], "updated_at": None}


def save_day(fecha: str, records: list[dict[str, Any]], zona: str | None = None) -> dict[str, Any]:
    day, z = split_fecha_zona(fecha, zona)
    with _IO_LOCK:
        payload = {
            "fecha": day,
            "zona": z,
            "records": records,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        _atomic_write_text(
            json_path(day, z),
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        return payload


def update_record_status(
    fecha: str, index: int, status: str, error: str = "", zona: str | None = None
) -> None:
    """Actualiza un registro bajo lock (para callbacks de subida)."""
    day, z = split_fecha_zona(fecha, zona)
    with _IO_LOCK:
        path = json_path(day, z)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {"fecha": day, "zona": z, "records": [], "updated_at": None}
        recs = list(data.get("records") or [])
        if 0 <= index < len(recs):
            recs[index]["upload_status"] = status
            recs[index]["upload_error"] = error
            data["records"] = recs
            data["fecha"] = day
            data["zona"] = z
            data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def export_excel(
    fecha: str, records: list[dict[str, Any]], fields: list[dict], zona: str | None = None
) -> Path:
    enabled = [f for f in fields if f.get("enabled", True)]
    headers = [f["label"] for f in enabled] + ["Estado subida", "Error"]
    keys = [f["key"] for f in enabled]

    path = excel_path(fecha, zona)
    wb = Workbook()
    ws = wb.active
    ws.title = "Pedidos"
    ws.append(headers)
    for i, rec in enumerate(records, start=1):
        row = [rec.get(k, "") for k in keys]
        row.append(rec.get("upload_status", "pending"))
        row.append(rec.get("upload_error", ""))
        ws.append(row)
    # guardado excel (openpyxl no es atomico; ok para uso local)
    with _IO_LOCK:
        wb.save(path)
    return path


def default_delivery_date(from_day: date | None = None) -> str:
    """Hoy + 2 dias; si cae domingo, pasa a lunes."""
    d = (from_day or date.today()) + timedelta(days=2)
    if d.weekday() == 6:
        d += timedelta(days=1)
    return d.isoformat()


def parse_natural_delivery_date(text: str, base: date | None = None) -> str | None:
    """Detecta mañana / dias de la semana / DD/MM en el texto."""
    import re
    import unicodedata

    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return re.sub(r"\s+", " ", s.lower()).strip()

    base = base or date.today()
    n = _norm(text)

    if re.search(r"\bpasado\s+manana\b", n):
        d = base + timedelta(days=2)
        if d.weekday() == 6:
            d += timedelta(days=1)
        return d.isoformat()
    if re.search(r"\bmanana\b", n):
        d = base + timedelta(days=1)
        if d.weekday() == 6:
            d += timedelta(days=1)
        return d.isoformat()

    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else base.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass

    weekdays = {
        "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3,
        "viernes": 4, "sabado": 5, "domingo": 6,
    }
    for name, wd in weekdays.items():
        if re.search(rf"\b{name}\b", n):
            delta = (wd - base.weekday()) % 7
            if delta == 0:
                delta = 7
            d = base + timedelta(days=delta)
            if d.weekday() == 6:
                d += timedelta(days=1)
            return d.isoformat()
    return None


def format_fecha_es(fecha: str) -> str:
    day, _z = split_fecha_zona(fecha)
    dt = datetime.strptime(day, "%Y-%m-%d")
    dias = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    return f"{dias[dt.weekday()]} {dt.day} de {meses[dt.month - 1]} del {dt.year}"


def _parse_precio(raw: Any) -> float:
    s = str(raw or "").strip().replace(",", ".")
    if not s:
        return 0.0
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return 0.0
    try:
        return float(m.group(0))
    except ValueError:
        return 0.0


def _product_items(raw: Any) -> list[str]:
    """Separa productos de un pedido ('; / saltos / comas tipicas)."""
    text = str(raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[\n|;]+", text)
    out: list[str] = []
    for part in parts:
        item = re.sub(r"^[\-\*•\d]+[\.\)\-:\s]*", "", part).strip(" -,\t")
        item = re.sub(r"\s+", " ", item).strip()
        if not item:
            continue
        low = item.lower()
        if low in ("producto", "productos", "contenido", "2x1", "promocion", "promoción"):
            # 2x1/promocion solos no cuentan como producto
            if low in ("2x1", "promocion", "promoción"):
                continue
            if low in ("producto", "productos", "contenido"):
                continue
        out.append(item)
    return out


def _norm_product_key(name: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFD", name or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9\s]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def month_sales_stats(year: int, month: int) -> dict[str, Any]:
    """Agrega pedidos del mes (dept + SS) para totales, depto y top productos."""
    if not (1 <= month <= 12) or year < 2000:
        raise ValueError("Mes o anio invalido")
    by_dept: dict[str, dict[str, float | int]] = {}
    by_product: dict[str, dict[str, Any]] = {}
    total_pedidos = 0
    total_ventas = 0.0
    pagados = 0
    days_with_data = 0

    with _IO_LOCK:
        paths = iter_pedido_json_paths(year, month)
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            records = data.get("records") or []
            if not records:
                continue
            days_with_data += 1
            for rec in records:
                total_pedidos += 1
                amount = _parse_precio(rec.get("precio"))
                total_ventas += amount
                if str(rec.get("pagado") or "").strip().lower() in ("si", "sí", "yes", "true", "1"):
                    pagados += 1
                dept = str(rec.get("departamento") or "").strip() or "Sin departamento"
                bucket = by_dept.setdefault(dept, {"pedidos": 0, "ventas": 0.0})
                bucket["pedidos"] = int(bucket["pedidos"]) + 1
                bucket["ventas"] = float(bucket["ventas"]) + amount

                items = _product_items(rec.get("producto"))
                if not items:
                    continue
                share = amount / len(items) if amount else 0.0
                for item in items:
                    key = _norm_product_key(item)
                    if not key:
                        continue
                    pb = by_product.setdefault(
                        key, {"producto": item, "cantidad": 0, "ventas": 0.0}
                    )
                    # Conservar la etiqueta mas larga/legible
                    if len(item) > len(str(pb["producto"])):
                        pb["producto"] = item
                    pb["cantidad"] = int(pb["cantidad"]) + 1
                    pb["ventas"] = float(pb["ventas"]) + share

    by_department = [
        {
            "departamento": name,
            "pedidos": int(vals["pedidos"]),
            "ventas": round(float(vals["ventas"]), 2),
        }
        for name, vals in sorted(
            by_dept.items(),
            key=lambda kv: (-float(kv[1]["ventas"]), kv[0].lower()),
        )
    ]
    top_products = [
        {
            "producto": str(vals["producto"]),
            "cantidad": int(vals["cantidad"]),
            "ventas": round(float(vals["ventas"]), 2),
        }
        for vals in sorted(
            by_product.values(),
            key=lambda v: (-int(v["cantidad"]), -float(v["ventas"]), str(v["producto"]).lower()),
        )[:10]
    ]
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    return {
        "year": year,
        "month": month,
        "label": f"{meses[month - 1].capitalize()} {year}",
        "total_pedidos": total_pedidos,
        "total_ventas": round(total_ventas, 2),
        "pagados": pagados,
        "no_pagados": max(0, total_pedidos - pagados),
        "promedio": round(total_ventas / total_pedidos, 2) if total_pedidos else 0.0,
        "dias_con_datos": days_with_data,
        "by_department": by_department,
        "top_products": top_products,
    }


def archive_old_days(keep_days: int = 60) -> int:
    """Mueve JSON/XLSX mas viejos que keep_days a data/archivo/YYYY/MM/."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = date.today() - timedelta(days=keep_days)
    moved = 0
    with _IO_LOCK:
        candidates = [
            p
            for p in DATA_DIR.rglob("pedidos_*.*")
            if p.is_file() and not _is_under_archivo(p)
        ]
        for path in candidates:
            m = re.match(r"pedidos_(\d{4}-\d{2}-\d{2})(?:_SS)?$", path.stem)
            if not m:
                continue
            try:
                day = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if day < cutoff:
                dest_dir = ARCHIVE_DIR / f"{day.year:04d}" / f"{day.month:02d}"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / path.name
                if dest.exists():
                    dest.unlink()
                path.replace(dest)
                moved += 1
    return moved
