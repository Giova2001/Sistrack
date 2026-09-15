# -*- coding: utf-8 -*-
"""Nomenclatura de abreviaturas de producto para nombres Forza."""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CATALOG_PATH = DATA_DIR / "product_abbr_forza.json"

_TRAILING_JUNK = re.compile(
    r"(?i)\s*(?:telefono|tel[eé]fono|whats?app|llamada|producto|contenido)\s*:?\s*.*$"
)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _fold_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _fold(s))


def _tokens(s: str) -> set[str]:
    return {t for t in _fold(s).split() if t and t not in {"casio", "de", "la", "el"}}


@lru_cache(maxsize=1)
def load_product_abbr_catalog() -> tuple[dict[str, Any], ...]:
    if not CATALOG_PATH.exists():
        return tuple()
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return tuple()
    if not isinstance(data, list):
        return tuple()
    out: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        abbr = str(row.get("abbr") or "").strip()
        full = str(row.get("full") or "").strip()
        if not abbr or not full:
            continue
        matches = row.get("match") or []
        if not isinstance(matches, list):
            matches = []
        out.append(
            {
                "abbr": abbr,
                "full": full,
                "match": [str(m).strip() for m in matches if str(m).strip()],
                "abbr_key": _fold_key(abbr),
                "full_key": _fold_key(full),
            }
        )
    out.sort(
        key=lambda x: max((len(_fold_key(m)) for m in x["match"]), default=0),
        reverse=True,
    )
    return tuple(out)


def reload_product_abbr_catalog() -> None:
    load_product_abbr_catalog.cache_clear()


def catalog_for_api() -> list[dict[str, Any]]:
    reload_product_abbr_catalog()
    return [
        {
            "abbr": e["abbr"],
            "full": e["full"],
            "match": list(e.get("match") or []),
        }
        for e in load_product_abbr_catalog()
    ]


def clean_client_name(nombre: str, producto: str = "") -> str:
    """Quita basura tipo 'Teléfono:' y restos de producto del nombre del cliente."""
    base = re.sub(r"\s+", " ", (nombre or "").strip())
    if not base:
        return ""
    # Conservar prefijo "N. "
    prefix = ""
    m = re.match(r"^(\d{1,2}[.)]\s*)", base)
    if m:
        prefix = m.group(1)
        base = base[m.end() :].strip()
    base = _TRAILING_JUNK.sub("", base).strip(" -|,")
    # Quitar producto/abbr pegados al final
    full = expand_product_label(producto) if producto else ""
    abbr = abbreviate_product_label(producto) if producto else ""
    for piece in (full, abbr, producto):
        p = re.sub(r"\s+", " ", (piece or "").strip())
        if len(p) < 2:
            continue
        rx = re.compile(r"(?:^|\s+)" + re.escape(p) + r"\s*$", re.I)
        base = rx.sub("", base).strip(" -|,")
    # Si quedó vacío, devolver original sin junk de teléfono
    if not base:
        raw = re.sub(r"\s+", " ", (nombre or "").strip())
        raw = _TRAILING_JUNK.sub("", raw).strip(" -|,")
        return raw or (nombre or "").strip()
    return f"{prefix}{base}".strip()


def expand_product_label(producto: str) -> str:
    """Abreviatura → nombre completo; si es texto libre, intenta resolver por match."""
    s = re.sub(r"\s+", " ", (producto or "").strip())
    if not s:
        return ""
    key = _fold_key(s)
    for e in load_product_abbr_catalog():
        if key == e["abbr_key"]:
            return e["full"]
    # Texto libre → abreviatura conocida → full
    abbr = abbreviate_product_label(s)
    if abbr:
        ak = _fold_key(abbr)
        for e in load_product_abbr_catalog():
            if ak == e["abbr_key"]:
                return e["full"]
    return s


def abbreviate_product_label(producto: str, *, max_len: int = 18) -> str:
    """Descripción/producto → abreviatura oficial (para nombres Forza)."""
    s = re.sub(r"\s+", " ", (producto or "").strip())
    s = re.sub(r"(?i)\s*[—\-]\s*Grabado\s*:.*$", "", s).strip()
    if not s:
        return ""
    s = re.split(r"[;|/]", s)[0].strip()
    low = _fold(s)
    if low in {"producto", "productos", "contenido"}:
        return ""

    key = _fold_key(s)
    for e in load_product_abbr_catalog():
        if key == e["abbr_key"]:
            abbr = e["abbr"]
            return abbr[:max_len] if max_len > 0 else abbr

    # Lentes / wood / aviador / gafas → siempre L2x1
    if any(
        t in _tokens(s) or t in low.split()
        for t in ("lentes", "lente", "gafas", "gafa", "wood", "aviador", "aviadores")
    ):
        return "L2x1"[:max_len] if max_len > 0 else "L2x1"
    if "2x1" in low and any(x in low for x in ("lent", "gafa", "wood", "aviador")):
        return "L2x1"[:max_len] if max_len > 0 else "L2x1"

    prod_tokens = _tokens(s)
    blob = f" {_fold(s)} "
    best: dict[str, Any] | None = None
    best_score = -1
    for e in load_product_abbr_catalog():
        candidates = list(e["match"]) + [e["full"], e["abbr"]]
        for m in candidates:
            mk = _fold(m)
            if not mk:
                continue
            # Frase exacta (orden fijo)
            if f" {mk} " in blob or _fold_key(m) == key:
                score = len(_fold_key(m)) + 20
                if score > best_score:
                    best_score = score
                    best = e
                continue
            # Tokens en cualquier orden (rose gold retro ↔ retro rose gold)
            need = _tokens(m)
            if len(need) >= 2 and need.issubset(prod_tokens):
                score = len("".join(need)) + len(need) * 3
                if score > best_score:
                    best_score = score
                    best = e
    if best is not None and best_score >= 6:
        abbr = best["abbr"]
        return abbr[:max_len] if max_len > 0 else abbr

    # Fallback: iniciales / recorte
    s2 = re.sub(r"\$?\d+([.,]\d+)?", "", s).strip(" -,\t")
    s2 = re.sub(r"\s+", " ", s2).strip()
    if not s2:
        return ""
    parts = [p for p in re.split(r"\s+", s2) if p]
    if len(parts) >= 3 and sum(len(p) for p in parts) > max_len:
        initials = "".join(p[0].upper() for p in parts if p[:1].isalnum())
        if 2 <= len(initials) <= max_len:
            return initials
    if max_len > 0 and len(s2) > max_len:
        cut = s2[:max_len].rsplit(" ", 1)[0].strip()
        s2 = cut or s2[:max_len]
    return s2.strip(" -,\t")


def forza_nombre_display(nombre: str, producto: str) -> str:
    """Nombre completo legible (cliente limpio + producto expandido)."""
    base = clean_client_name(nombre, producto)
    full = expand_product_label(producto)
    if not base:
        return full or "Cliente"
    if not full:
        return base
    if _fold(full) in _fold(base) or _fold_key(full) in _fold_key(base):
        return base
    abbr = abbreviate_product_label(producto)
    if abbr and _fold_key(abbr) in _fold_key(base):
        pattern = re.compile(re.escape(abbr) + r"\s*$", re.I)
        if pattern.search(base):
            return pattern.sub(full, base).strip()
    return f"{base} {full}".strip()


def forza_nombre_portal(nombre: str, producto: str, *, max_len: int = 50) -> str:
    """Nombre portal Forza: cliente limpio + abreviatura (~50)."""
    base = clean_client_name(nombre, producto)
    full = expand_product_label(producto)
    abbr = abbreviate_product_label(producto, max_len=18)
    if not base:
        out = abbr or full or "Cliente"
    elif abbr:
        if _fold_key(abbr) in _fold_key(base):
            out = base
        else:
            out = f"{base} {abbr}".strip()
    else:
        out = base
    if max_len > 0 and len(out) > max_len:
        out = out[:max_len].rsplit(" ", 1)[0].strip() or out[:max_len]
    return out.strip()
