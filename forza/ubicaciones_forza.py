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
    fuente: str = ""


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
                    fuente=(row.get("fuente") or "").strip(),
                )
            )
    return tuple(out)


def _token_in_blob(token: str, nblob: str) -> bool:
    """True si token aparece como substring con bordes razonables."""
    t = (token or "").strip()
    if not t or t not in nblob:
        return False
    if len(t) >= 8:
        return True
    # Evitar falsos positivos tipo "ana" dentro de "santa"
    for m in re.finditer(re.escape(t), nblob):
        a = m.start()
        b = m.end()
        left_ok = a == 0 or not nblob[a - 1].isalnum()
        right_ok = b >= len(nblob) or not nblob[b].isalnum()
        if left_ok and right_ok:
            return True
    return False


def _muni_variants(name: str) -> list[str]:
    """Variantes de municipio (con/sin Centro|Norte|Sur|Costa), sin chocar con depto."""
    n = norm(name)
    if not n:
        return []
    out = [n]
    base = re.sub(r"\s+(centro|norte|sur|este|oeste|costa)$", "", n).strip()
    if base and base != n:
        try:
            dept_names = {norm(d) for d in get_catalog().departments}
        except Exception:
            dept_names = set()
        # "La Libertad Centro" → no usar "la libertad" (es el departamento)
        if base not in dept_names:
            out.append(base)
    return out


def score_catalog_entry_against_text(
    entry: ForzaCatalogEntry,
    nblob: str,
    *,
    want_dep: str = "",
    want_mun: str = "",
    want_col: str = "",
) -> int:
    """Puntúa una fila del catálogo Forza contra el texto libre del pedido."""
    if not nblob:
        return 0
    col_n = norm(entry.colonia)
    mun_n = norm(entry.municipio)
    dep_n = norm(entry.departamento)
    alias_n = norm(entry.alias)
    score = 0
    muni_vars = _muni_variants(entry.municipio)
    is_muni_nuevo = bool(
        re.search(r"\s+(centro|norte|sur|este|oeste|costa)$", mun_n or "")
    )
    _NUEVO_MARKERS = (
        " centro",
        " norte",
        " sur",
        " este",
        " oeste",
        " costa",
    )

    # Semilla distrito: colonia inventada = nombre del municipio (no es poblado real)
    colonia_es_municipio = bool(
        col_n and mun_n and (col_n == mun_n or col_n in muni_vars)
    )

    col_hit = bool(
        col_n
        and len(col_n) >= 4
        and not colonia_es_municipio
        and _token_in_blob(col_n, nblob)
    )
    # Semilla municipio nuevo: colonia = distrito, municipio = "Depto Centro"
    # No contar el distrito como poblado si el texto no pide Centro/Norte/Sur
    if col_hit and is_muni_nuevo:
        if not any(x in nblob for x in _NUEVO_MARKERS):
            col_hit = False

    alias_hit = bool(
        alias_n
        and len(alias_n) >= 4
        and alias_n != col_n
        and not colonia_es_municipio
        and _token_in_blob(alias_n, nblob)
    )
    if col_hit:
        score += 10 + min(len(col_n), 24)
    elif alias_hit:
        score += 8 + min(len(alias_n), 20)

    muni_hit = False
    for mv in muni_vars:
        if len(mv) >= 4 and _token_in_blob(mv, nblob):
            muni_hit = True
            break
    # También si el texto menciona la "colonia" semilla (= municipio)
    if not muni_hit and colonia_es_municipio and col_n and _token_in_blob(col_n, nblob):
        muni_hit = True
    if muni_hit:
        score += 7
        # Bonus si el municipio es más específico que el departamento
        if mun_n and dep_n and mun_n != dep_n:
            score += 2
    elif mun_n and want_mun and any(
        norm(want_mun) == v or norm(want_mun) in v or v in norm(want_mun)
        for v in muni_vars
    ):
        score += 3

    dep_hit = bool(dep_n and len(dep_n) >= 4 and _token_in_blob(dep_n, nblob))
    if dep_hit:
        score += 5
    elif dep_n and want_dep and norm(want_dep) == dep_n:
        score += 3

    # Coherencia con lo ya inferido (Sistrack)
    if want_dep and dep_n and norm(want_dep) == dep_n:
        score += 4
    elif want_dep and dep_n and norm(want_dep) != dep_n:
        # Otro departamento y el texto no lo menciona → descartar
        if not dep_hit and want_dep and norm(want_dep) not in nblob:
            return 0
        score -= 10

    if want_mun:
        wm = norm(want_mun)
        if any(wm == v or wm in v or v in wm for v in muni_vars):
            score += 5
        elif col_hit and not muni_hit:
            score -= 2

    if want_col and col_n and norm(want_col) == col_n and not colonia_es_municipio:
        score += 6

    fuente = (entry.fuente or "").lower()
    if fuente == "forza_confirmado":
        score += 4
    elif fuente == "manual":
        score += 3
    elif "municipio_nuevo" in fuente:
        if not any(x in nblob for x in _NUEVO_MARKERS):
            score -= 2

    # Sin colonia/poblado real en texto: solo municipio+depto (semilla distrito)
    if not col_hit and not alias_hit:
        if muni_hit and dep_hit:
            score += 2
        elif muni_hit:
            score += 0
        else:
            return 0

    return score


def best_catalog_match_from_text(
    *texts: str,
    departamento: str = "",
    municipio: str = "",
    colonia: str = "",
    min_score: int = 14,
) -> ForzaCatalogEntry | None:
    """Mejor fila del catálogo Forza según dirección / referencia / hints."""
    blob = " | ".join(t for t in texts if (t or "").strip())
    nblob = norm(blob)
    if not nblob:
        return None
    catalog = load_forza_catalog()
    if not catalog:
        return None
    best: ForzaCatalogEntry | None = None
    best_score = 0

    def _prefer(cand: ForzaCatalogEntry, cur: ForzaCatalogEntry | None) -> bool:
        if cur is None:
            return True
        # Preferir municipio distinto del departamento (Soyapango > San Salvador)
        c_same = norm(cand.municipio) == norm(cand.departamento)
        u_same = norm(cur.municipio) == norm(cur.departamento)
        if c_same != u_same:
            return not c_same
        # Preferir colonia/poblado más largo y específico
        if len(norm(cand.colonia)) != len(norm(cur.colonia)):
            return len(norm(cand.colonia)) > len(norm(cur.colonia))
        # Preferir confirmados Forza
        return (cand.fuente or "") == "forza_confirmado" and (
            cur.fuente or ""
        ) != "forza_confirmado"

    for entry in catalog:
        score = score_catalog_entry_against_text(
            entry,
            nblob,
            want_dep=departamento,
            want_mun=municipio,
            want_col=colonia,
        )
        if score > best_score or (
            score == best_score and score > 0 and _prefer(entry, best)
        ):
            best_score = score
            best = entry
    if best is None or best_score < min_score:
        return None
    return best


def match_in_forza_catalog(ubic: ForzaUbicacion) -> ForzaCatalogEntry | None:
    """Busca la mejor fila del CSV local para la ubicación resuelta."""
    catalog = load_forza_catalog()
    if not catalog:
        return None
    # Primero: match directo por texto compuesto
    hit = best_catalog_match_from_text(
        ubic.colonia,
        ubic.municipio,
        ubic.departamento,
        ubic.catalog_label,
        ubic.search_hint,
        departamento=ubic.departamento,
        municipio=ubic.municipio,
        colonia=ubic.colonia,
        min_score=12,
    )
    if hit:
        return hit
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

    hit = best_catalog_match_from_text(blob, min_score=14)
    if hit and hit.colonia:
        return hit.colonia

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
    col = colonia_display(colonia) if colonia else ""

    # Descifrado primario: catálogo Forza sobre el texto completo
    hit = best_catalog_match_from_text(
        direccion,
        referencia,
        colonia,
        departamento,
        municipio,
        departamento=dept,
        municipio=muni,
        colonia=col,
        min_score=14,
    )
    if hit:
        return ForzaUbicacion(
            colonia=hit.colonia or col or colonia_display(muni),
            municipio=hit.municipio or muni,
            departamento=hit.departamento or dept,
            search_hint=hit.alias or hit.colonia or muni,
            catalog_label=hit.label,
        )

    if not col:
        col = extract_colonia(direccion, referencia)
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
    hit2 = match_in_forza_catalog(ubic)
    if hit2:
        return ForzaUbicacion(
            colonia=hit2.colonia or col,
            municipio=hit2.municipio or muni,
            departamento=hit2.departamento or dept,
            search_hint=hit2.alias or hint,
            catalog_label=hit2.label,
        )
    return ubic


def decipher_forza_fields(
    *,
    direccion: str = "",
    referencia: str = "",
    departamento: str = "",
    municipio: str = "",
    colonia: str = "",
) -> dict[str, str]:
    """Campos Forza (poblado/municipio/depto + label) a partir del pedido."""
    ubic = resolve_forza_location(
        direccion=direccion,
        referencia=referencia,
        departamento=departamento,
        municipio=municipio,
        colonia=colonia,
    )
    return {
        "colonia": ubic.colonia or "",
        "municipio": ubic.municipio or "",
        "departamento": ubic.departamento or "",
        "forza_label": ubic.label or "",
        "forza_search_hint": ubic.search_hint or "",
    }


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
