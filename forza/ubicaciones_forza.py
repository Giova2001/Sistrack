# -*- coding: utf-8 -*-
"""
Resolver de ubicaciones para Forza Delivery.

Forza busca poblados en el formato del selector de Envío:
  Colonia/Poblado, Municipio, Departamento
Ejemplos del modal:
  EL ARENAL, Ciudad Delgado, San Salvador
  ASHAPUCO, Ahuachapán, Ahuachapán

En Detalles a veces se muestra:
  Departamento, Municipio, Poblado
  (ej. Ahuachapán, Ahuachapán, ASHAPUCO)

Catálogo local (opcional, mejora el match):
  forza/catalogo_ubicaciones_forza.csv
Generar/actualizar:
  python -m forza.export_poblados
  python -m forza.export_poblados --seed-only
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sistrack.ubicaciones import (
    DISTRITO_SPELLINGS,
    EXTRA_ALIASES,
    get_catalog,
    infer_location,
    norm,
    strip_accents,
)

FORZA_CATALOG_PATH = Path(__file__).with_name("catalogo_ubicaciones_forza.csv")

_COLONIA_PREFIX = (
    r"(?:colonia|col\.?|residencial|res\.?|urbanizaci[oó]n|urb\.?|"
    r"lotificaci[oó]n|lot\.?|barrio|cant[oó]n|comunidad|aldea|"
    r"caser[ií]o|parcelaci[oó]n|zona)"
)

_COLONIA_RE = re.compile(
    rf"(?i)\b{_COLONIA_PREFIX}\s+([A-Za-zÁÉÍÓÚáéíóúÜüÑñ0-9][\wÁÉÍÓÚáéíóúÜüÑñ0-9 .'\-]{{1,60}})"
)

_COLONIA_STOP = re.compile(
    r"(?i)\s*(?:,|/|\||\bcalle\b|\bav(?:enida|\.)?\b|\bpje\b|\bpasaje\b|"
    r"\bpoligono\b|\bpolígono\b|\bbloque\b|\bapto\b|\bapartamento\b|"
    r"\bcasa\b|\bno\.?\b|\bnumero\b|\bn[uú]mero\b|\bkm\b|\bfinal\b|"
    r"\bfrente\b|\bcerca\b|\bdetras\b|\bdetrás\b|\breferencia\b)"
)


@dataclass(frozen=True)
class ForzaUbicacion:
    """Ubicación en el formato que espera el selector de Forza."""

    colonia: str
    municipio: str
    departamento: str
    search_hint: str = ""
    catalog_label: str = ""

    @property
    def label(self) -> str:
        if self.catalog_label:
            return self.catalog_label
        parts = [p for p in (self.colonia, self.municipio, self.departamento) if p]
        return ", ".join(parts)

    def search_queries(self) -> list[str]:
        """Consultas de búsqueda ordenadas de más a menos específicas."""
        out: list[str] = []
        seen: set[str] = set()

        def add(q: str) -> None:
            q = re.sub(r"\s+", " ", (q or "").strip(" ,"))
            if not q:
                return
            key = norm(q)
            if key in seen:
                return
            seen.add(key)
            out.append(q)

        same_col_muni = bool(
            self.colonia and self.municipio and norm(self.colonia) == norm(self.municipio)
        )

        # 1) Label exacto del catálogo / colonia+municipio
        if self.catalog_label:
            add(self.catalog_label)
            col, mun, _dep = parse_forza_label(self.catalog_label)
            if col and mun:
                add(f"{col}, {mun}")
            add(col)
        if self.search_hint and not same_col_muni:
            for part in re.split(r"[|;]+", self.search_hint):
                add(part.strip())
        if self.colonia and self.municipio and not same_col_muni:
            add(f"{self.colonia}, {self.municipio}")
            add(self.colonia)

        # 2) Municipio (+ departamento) — más fiable que tokens sueltos
        if self.municipio:
            add(self.municipio)
            if self.departamento:
                add(f"{self.municipio}, {self.departamento}")
            mtoks = [t for t in self.municipio.split() if t]
            if len(mtoks) >= 2:
                add(" ".join(mtoks[-2:]))  # "Juan Opico"
                add(mtoks[-1])  # "Opico"

        if self.colonia and not same_col_muni:
            tokens = [t for t in re.split(r"\s+", self.colonia) if t]
            if len(tokens) >= 2:
                add(" ".join(tokens[:2]))
            if tokens and len(tokens[-1]) >= 4:
                add(tokens[-1])
            if tokens and len(tokens[0]) >= 4 and tokens[0].upper() not in {
                "EL",
                "LA",
                "LOS",
                "LAS",
                "SAN",
                "SANTA",
                "COL",
                "RES",
                "CANTON",
                "CANTÓN",
            }:
                add(tokens[0])

        return out


@dataclass(frozen=True)
class ForzaCatalogEntry:
    colonia: str
    municipio: str
    departamento: str
    label: str
    alias: str = ""


@lru_cache(maxsize=1)
def load_forza_catalog(path: str | None = None) -> tuple[ForzaCatalogEntry, ...]:
    """Carga catalogo_ubicaciones_forza.csv si existe."""
    p = Path(path) if path else FORZA_CATALOG_PATH
    if not p.exists():
        return tuple()
    out: list[ForzaCatalogEntry] = []
    with p.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            col = (row.get("colonia") or "").strip()
            mun = (row.get("municipio") or "").strip()
            dep = (row.get("departamento") or "").strip()
            label = (row.get("label_forza") or "").strip()
            if not label:
                label = ", ".join(x for x in (col, mun, dep) if x)
            if not label:
                continue
            out.append(
                ForzaCatalogEntry(
                    colonia=col,
                    municipio=mun,
                    departamento=dep,
                    label=label,
                    alias=(row.get("alias_busqueda") or col or mun).strip(),
                )
            )
    return tuple(out)


def match_in_forza_catalog(ubic: ForzaUbicacion) -> ForzaCatalogEntry | None:
    """Busca la mejor fila del CSV local para la ubicación resuelta."""
    catalog = load_forza_catalog()
    if not catalog:
        return None
    best: ForzaCatalogEntry | None = None
    best_score = 0
    for entry in catalog:
        score = score_forza_label(entry.label, ubic)
        if ubic.colonia and norm(entry.colonia) == norm(ubic.colonia):
            score += 3
        if score > best_score:
            best_score = score
            best = entry
    if best_score < 6:
        return None
    return best


def title_sv(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return ""
    parts = []
    for w in raw.split(" "):
        if w.isupper() and len(w) <= 3:
            parts.append(w)
        else:
            parts.append(w[:1].upper() + w[1:].lower() if w else w)
    return " ".join(parts)


def colonia_display(name: str) -> str:
    s = re.sub(r"\s+", " ", (name or "").strip(" ,.-"))
    if not s:
        return ""
    s = re.sub(rf"(?i)^\s*{_COLONIA_PREFIX}\s+", "", s).strip()
    return strip_accents(s).upper()


def municipio_display(name: str) -> str:
    s = re.sub(r"\s+", " ", (name or "").strip())
    if not s:
        return ""
    key = norm(s)
    if key in DISTRITO_SPELLINGS:
        return DISTRITO_SPELLINGS[key]
    cat = get_catalog()
    if key in cat.distrito_canon:
        return cat.distrito_canon[key]
    if s.isupper():
        return title_sv(s)
    return title_sv(s)


def departamento_display(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    cat = get_catalog()
    n = norm(s)
    for d in cat.departments:
        if norm(d) == n:
            return d
    return title_sv(s)


def extract_colonia(*texts: str) -> str:
    blob = " | ".join(t for t in texts if (t or "").strip())
    if not blob:
        return ""

    nblob = norm(blob)
    for alias, (_dept, _dist) in EXTRA_ALIASES.items():
        if alias in nblob and len(alias) >= 4:
            if any(
                k in alias
                for k in (
                    "lourdes",
                    "santa elena",
                    "satelite",
                    "bella vista",
                    "jalacatal",
                    "rabida",
                    "san jacinto",
                    "atami",
                )
            ):
                return colonia_display(alias)

    for entry in load_forza_catalog():
        col_n = norm(entry.colonia)
        if len(col_n) >= 4 and col_n in nblob:
            return entry.colonia

    matches = list(_COLONIA_RE.finditer(blob))
    if not matches:
        return ""

    raw = matches[-1].group(1).strip(" ,.-")
    raw = _COLONIA_STOP.split(raw, maxsplit=1)[0].strip(" ,.-")
    raw = re.split(
        r"(?i)\s*,\s*|\s+(?:san salvador|la libertad|santa ana|san miguel|"
        r"sonsonate|ahuachap[aá]n|la paz|cuscatl[aá]n|chalatenango|"
        r"caba[nñ]as|usulut[aá]n|moraz[aá]n|la uni[oó]n|san vicente)\b",
        raw,
        maxsplit=1,
    )[0].strip(" ,.-")
    if len(raw) < 2:
        return ""
    return colonia_display(raw)


def _resolve_municipio_departamento(
    direccion: str,
    referencia: str,
    departamento: str = "",
    municipio: str = "",
) -> tuple[str, str]:
    dept = (departamento or "").strip()
    muni = (municipio or "").strip()
    if not dept or not muni:
        d2, m2 = infer_location(direccion or "", referencia or "")
        dept = dept or d2
        muni = muni or m2
    return departamento_display(dept), municipio_display(muni)


def resolve_forza_location(
    direccion: str = "",
    referencia: str = "",
    departamento: str = "",
    municipio: str = "",
    colonia: str = "",
) -> ForzaUbicacion:
    """Arma colonia/municipio/departamento; usa CSV Forza si existe."""
    dept, muni = _resolve_municipio_departamento(
        direccion, referencia, departamento, municipio
    )
    col = colonia_display(colonia) if colonia else extract_colonia(direccion, referencia)
    if not col and muni:
        col = colonia_display(muni)

    hint = ""
    if col and muni and norm(col) != norm(muni):
        hint = col
    elif muni:
        hint = muni

    ubic = ForzaUbicacion(
        colonia=col,
        municipio=muni,
        departamento=dept,
        search_hint=hint,
    )
    hit = match_in_forza_catalog(ubic)
    if hit:
        return ForzaUbicacion(
            colonia=hit.colonia or col,
            municipio=hit.municipio or muni,
            departamento=hit.departamento or dept,
            search_hint=hit.alias or hint,
            catalog_label=hit.label,
        )
    return ubic


def parse_forza_label(label: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in (label or "").split(",")]
    parts = [p for p in parts if p]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    if len(parts) == 1:
        return parts[0], "", ""
    return "", "", ""


def score_forza_label(label: str, ubic: ForzaUbicacion) -> int:
    text = (label or "").strip()
    if not text:
        return 0
    t = norm(text)
    col_l, mun_l, dep_l = parse_forza_label(text)
    col_n, mun_n, dep_n = norm(col_l), norm(mun_l), norm(dep_l)

    want_col = norm(ubic.colonia)
    want_mun = norm(ubic.municipio)
    want_dep = norm(ubic.departamento)

    # Rechazar de plano otro departamento (evita Ilopango/SS cuando es La Libertad)
    if want_dep and dep_n and dep_n != want_dep and want_dep not in t:
        return 0
    if want_dep and not dep_n and want_dep not in t:
        # label sin depto explícito pero tampoco lo menciona
        if want_mun and want_mun not in t and want_col and want_col not in t:
            return 0

    score = 0
    if want_col and col_n == want_col:
        score += 8
    elif want_col and want_col in col_n:
        score += 5
    elif want_col and want_col in t:
        score += 3

    if want_mun and (mun_n == want_mun or want_mun == mun_n):
        score += 6
    elif want_mun and want_mun in mun_n:
        score += 4
    elif want_mun and want_mun in t:
        score += 2
    elif want_mun and mun_n and want_mun not in t and mun_n not in want_mun:
        # Municipio distinto → casi seguro incorrecto
        score -= 8

    if want_dep and (dep_n == want_dep or want_dep in t):
        score += 4

    if ubic.label and norm(ubic.label) == t:
        score += 10
    if ubic.catalog_label and norm(ubic.catalog_label) == t:
        score += 12

    for tok in want_col.split():
        if len(tok) >= 4 and tok in col_n:
            score += 1
    return score


def best_forza_match(labels: list[str], ubic: ForzaUbicacion) -> str | None:
    if not labels:
        return None
    ranked = sorted(
        ((score_forza_label(lab, ubic), lab) for lab in labels),
        key=lambda x: x[0],
        reverse=True,
    )
    best_score, best_label = ranked[0]
    # Exigir al menos municipio o colonia fuerte (+ depto ya filtrado)
    if best_score < 6:
        return None
    return best_label


def forza_location_from_pedido(
    *,
    direccion: str = "",
    referencia: str = "",
    departamento: str = "",
    municipio: str = "",
    colonia: str = "",
) -> ForzaUbicacion:
    return resolve_forza_location(
        direccion=direccion,
        referencia=referencia,
        departamento=departamento,
        municipio=municipio,
        colonia=colonia,
    )
