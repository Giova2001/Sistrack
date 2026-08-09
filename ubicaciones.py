# -*- coding: utf-8 -*-
"""Catalogo de ubicaciones SV + inferencia depto/municipio para Sistrack."""
from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("catalogo_ubicaciones_el_salvador.csv")

# Valor exacto del <select id=state> en Sistrack (typos incluidos)
STATE_ALIASES: dict[str, str] = {
    "chalatenango": "Chaletenango",
    "chaletenango": "Chaletenango",
    "cuscatlan": "Cuscatlan",
    "usulutan": "Usulutan",
    "ahuachapan": "Ahuachapan",
    "cabanas": "Cabanas",
    "morazan": "Morazan",
    "la union": "La Union",
}

# Alias coloquiales / zonas que no son distrito oficial -> (depto, distrito_para_sistrack)
EXTRA_ALIASES: dict[str, tuple[str, str]] = {
    "lourdes": ("La Libertad", "Colon"),
    "lourdes colon": ("La Libertad", "Colon"),
    "nuevo lourdes": ("La Libertad", "Colon"),
    "santa elena": ("La Libertad", "Antiguo Cuscatlan"),  # zona Merliot
    "atami": ("La Libertad", "Tamanique"),
    "la rabida": ("San Salvador", "San Salvador"),
    "san jacinto": ("San Salvador", "San Salvador"),
    "colonia satelite": ("San Salvador", "San Salvador"),
    "bella vista": ("Santa Ana", "Santa Ana"),
    "plan de las mesas": ("Chalatenango", "Chalatenango"),
    "el jalacatal": ("San Miguel", "San Miguel"),
    "san jose villanueva": ("La Libertad", "San Jose Villanueva"),
    "villanueva": ("La Libertad", "San Jose Villanueva"),
    "perulapan": ("Cuscatlan", "San Pedro Perulapan"),
    "cd barrios": ("San Miguel", "Ciudad Barrios"),
    "tierra blanca": ("Usulutan", "Jiquilisco"),
    "gotera": ("Morazan", "San Francisco Gotera"),
}

_FALSE_DEPT_PHRASES = (
    "banco cuscatlan",
    "puente cuscatlan",
    "almacen es bout",
)


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(text or "").lower()).strip()


def sistrack_city_label(distrito: str) -> str:
    return strip_accents(distrito).upper()


def _canon_dept(name: str, departments: list[str]) -> str:
    n = norm(name)
    for d in departments:
        if norm(d) == n:
            return d
    return name


class UbicacionesCatalog:
    def __init__(self, path: Path | None = None):
        self.path = path or CATALOG_PATH
        self.departments: list[str] = []
        self.distrito_to_dept: dict[str, str] = {}
        self.distrito_canon: dict[str, str] = {}
        self.municipio_nuevo_to_dept: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"No se encontro catalogo: {self.path}")
        depts: list[str] = []
        seen_dept: set[str] = set()
        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                dept = (row.get("departamento") or "").strip()
                mun_nuevo = (row.get("municipio") or "").strip()
                distrito = (row.get("distrito") or "").strip()
                if not dept or not distrito:
                    continue
                if dept not in seen_dept:
                    seen_dept.add(dept)
                    depts.append(dept)
                dkey = norm(distrito)
                self.distrito_to_dept.setdefault(dkey, dept)
                self.distrito_canon.setdefault(dkey, distrito)
                if mun_nuevo:
                    self.municipio_nuevo_to_dept.setdefault(norm(mun_nuevo), dept)
        self.departments = sorted(depts, key=lambda d: len(norm(d)), reverse=True)
        print(
            f"Catalogo ubicaciones: {len(self.distrito_to_dept)} distritos, "
            f"{len(self.departments)} deptos ({self.path.name})"
        )

    def state_for_sistrack(self, departamento: str) -> str:
        key = norm(departamento)
        if key in STATE_ALIASES:
            return STATE_ALIASES[key]
        for d in self.departments:
            if norm(d) == key:
                return STATE_ALIASES.get(norm(d), d)
        return STATE_ALIASES.get(key, departamento)

    def infer(self, direccion: str, referencia: str = "") -> tuple[str, str]:
        blob = f"{direccion} {referencia}"
        nblob = norm(blob)

        nblob_dept = nblob
        for phrase in _FALSE_DEPT_PHRASES:
            nblob_dept = nblob_dept.replace(phrase, " ")

        dept = ""
        for d in self.departments:
            if norm(d) in nblob_dept:
                dept = d
                break

        # (is_dept_name, -len, pos, dept, city_label)
        candidates: list[tuple[bool, int, int, str, str]] = []

        for dkey, distrito in self.distrito_canon.items():
            pos = nblob.find(dkey)
            if pos < 0:
                continue
            dpt = self.distrito_to_dept[dkey]
            is_dept = bool(dept) and dkey == norm(dept)
            candidates.append(
                (is_dept, -len(dkey), pos, dpt, sistrack_city_label(distrito))
            )

        for key, (dpt, distrito) in EXTRA_ALIASES.items():
            pos = nblob.find(key)
            if pos < 0:
                continue
            dpt_canon = _canon_dept(
                self.distrito_to_dept.get(norm(distrito), dpt), self.departments
            )
            is_dept = bool(dept) and key == norm(dept)
            candidates.append(
                (is_dept, -len(key), pos, dpt_canon, sistrack_city_label(distrito))
            )

        municipio = ""
        if candidates:
            pool = candidates
            if dept:
                same = [c for c in candidates if norm(c[3]) == norm(dept)]
                if same:
                    pool = same
            pool.sort()  # is_dept False first, then longer, then earlier
            municipio = pool[0][4]
            if not dept:
                dept = pool[0][3]

        if not municipio:
            for mkey, dpt in sorted(
                self.municipio_nuevo_to_dept.items(), key=lambda x: -len(x[0])
            ):
                if mkey in nblob:
                    if not dept:
                        dept = dpt
                    break

        if not dept:
            dept = "San Salvador"

        # Overrides de ambiguedad
        if "santa elena" in nblob:
            if "usulutan" in nblob:
                dept = _canon_dept("Usulutan", self.departments)
                municipio = "SANTA ELENA"
            elif "la libertad" in nblob:
                dept = _canon_dept("La Libertad", self.departments)
                municipio = "ANTIGUO CUSCATLAN"

        if "cojutepeque" in nblob:
            dept = _canon_dept("Cuscatlan", self.departments)
            municipio = "COJUTEPEQUE"

        if "san vicente" in nblob and "puente cuscatlan" in nblob:
            dept = _canon_dept("San Vicente", self.departments)
            municipio = "SAN VICENTE"

        if "san miguel" in nblob and (
            "banco cuscatlan" in nblob or "cuscatlan" in norm(dept)
        ):
            dept = _canon_dept("San Miguel", self.departments)
            municipio = "SAN MIGUEL"

        if "santa tecla" in nblob:
            dept = _canon_dept("La Libertad", self.departments)
            municipio = "SANTA TECLA"

        # Preferir Olocuilta si aparece explicitamente (evitar match por "Zacatecoluca" en ruta)
        if "olocuilta" in nblob:
            dept = _canon_dept("La Paz", self.departments)
            municipio = "OLOCUILTA"

        return dept, municipio


_CATALOG: UbicacionesCatalog | None = None


def get_catalog() -> UbicacionesCatalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = UbicacionesCatalog()
    return _CATALOG


def infer_location(direccion: str, referencia: str = "") -> tuple[str, str]:
    return get_catalog().infer(direccion, referencia)


def state_label_for_sistrack(departamento: str) -> str:
    return get_catalog().state_for_sistrack(departamento)
