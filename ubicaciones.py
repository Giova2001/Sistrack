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

# Ortografia coloquial / typos frecuentes -> distrito del catalogo
DISTRITO_SPELLINGS: dict[str, str] = {
    "quetzaltepeque": "Quezaltepeque",
    "quezaltepeqe": "Quezaltepeque",
    "quezaltepeq": "Quezaltepeque",
    "quetsaltepeque": "Quezaltepeque",
    "quezzaltepeque": "Quezaltepeque",
    "antiguo cuscatlan": "Antiguo Cuscatlan",
    "nuevo cuscatlan": "Nuevo Cuscatlan",
    "san jose villanueva": "San Jose Villanueva",
    "santiago texacuangos": "Santiago Texacuangos",
    "san martin": "San Martin",
    "ciudad delgado": "Ciudad Delgado",
    "mejicanos": "Mejicanos",
    "cuscatancingo": "Cuscatancingo",
    "ayutuxtepeque": "Ayutuxtepeque",
    "tonacatepeque": "Tonacatepeque",
    "soyapango": "Soyapango",
    "ilopango": "Ilopango",
    "apopa": "Apopa",
    "nejapa": "Nejapa",
    "panchimalco": "Panchimalco",
    "rosario de mora": "Rosario de Mora",
    "san marco": "San Marcos",
    "san marcos": "San Marcos",
    "opico": "San Juan Opico",
    "san juan opico": "San Juan Opico",
    "ciudad arce": "Ciudad Arce",
    "huizucar": "Huizucar",
    "zaragoza": "Zaragoza",
    "comasagua": "Comasagua",
    "santa tecla": "Santa Tecla",
    "colon": "Colon",
    "lourdes": "Colon",
    "zacatecoluca": "Zacatecoluca",
    "olocuilta": "Olocuilta",
    "san pedro masahuat": "San Pedro Masahuat",
    "san luis talpa": "San Luis Talpa",
    "san juan nonualco": "San Juan Nonualco",
    "santiago nonualco": "Santiago Nonualco",
    "cojutepeque": "Cojutepeque",
    "suchitoto": "Suchitoto",
    "sensuntepeque": "Sensuntepeque",
    "chalchuapa": "Chalchuapa",
    "metapan": "Metapan",
    "ahuachapan": "Ahuachapan",
    "sonsonate": "Sonsonate",
    "izalco": "Izalco",
    "acajutla": "Acajutla",
    "san miguel": "San Miguel",
    "usulutan": "Usulutan",
    "santiago de maria": "Santiago de Maria",
    "berlin": "Berlin",
    "jiquilisco": "Jiquilisco",
    "la union": "La Union",
    "santa rosa de lima": "Santa Rosa de Lima",
    "san francisco gotera": "San Francisco Gotera",
    "gotera": "San Francisco Gotera",
}

_FALSE_DEPT_PHRASES = (
    "banco cuscatlan",
    "puente cuscatlan",
    "almacen es bout",
)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _word_find(hay: str, key: str) -> int:
    m = re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", hay)
    return m.start() if m else -1


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
        # silencio en carga (evitar ruido en cada request)

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
            if _word_find(nblob_dept, norm(d)) >= 0:
                dept = d
                break

        # (penalty, -len, -pos, dept, city_label)
        # penalty: 0=exact distrito distinto del depto, 1=alias/spelling, 2=fuzzy, 3=homonimo depto
        candidates: list[tuple[int, int, int, str, str]] = []

        def add_cand(
            key: str,
            pos: int,
            dpt: str,
            distrito: str,
            *,
            penalty: int = 0,
        ) -> None:
            if pos < 0:
                return
            city = sistrack_city_label(distrito)
            pen = penalty
            if norm(city) == norm(dpt) or norm(key) == norm(dpt):
                pen = max(pen, 3)  # "La Libertad" ciudad vs depto
            candidates.append((pen, -len(key), -pos, dpt, city))

        for dkey, distrito in self.distrito_canon.items():
            add_cand(dkey, _word_find(nblob, dkey), self.distrito_to_dept[dkey], distrito)

        for key, (dpt, distrito) in EXTRA_ALIASES.items():
            dpt_canon = _canon_dept(
                self.distrito_to_dept.get(norm(distrito), dpt), self.departments
            )
            add_cand(key, _word_find(nblob, key), dpt_canon, distrito, penalty=1)

        for key, distrito in DISTRITO_SPELLINGS.items():
            dkey = norm(distrito)
            dpt = self.distrito_to_dept.get(dkey) or self.distrito_to_dept.get(
                norm(strip_accents(distrito))
            )
            if not dpt:
                # resolver por etiqueta
                for dk, name in self.distrito_canon.items():
                    if norm(name) == dkey:
                        dpt = self.distrito_to_dept[dk]
                        distrito = name
                        break
            if not dpt:
                continue
            add_cand(key, _word_find(nblob, key), dpt, distrito, penalty=1)

        # Fuzzy: tokens largos vs distritos (1-2 edits)
        tokens = set(re.findall(r"[a-z]{6,}", nblob))
        dept_norms = {norm(d) for d in self.departments}
        known_exact = set(self.distrito_canon) | set(DISTRITO_SPELLINGS) | set(EXTRA_ALIASES)
        for tok in tokens:
            if tok in dept_norms or tok in known_exact:
                continue
            best: tuple[int, str, str] | None = None
            for dkey, distrito in self.distrito_canon.items():
                if abs(len(dkey) - len(tok)) > 2:
                    continue
                parts = dkey.split()
                targets = [dkey]
                if len(parts) > 1 and len(parts[-1]) >= 6:
                    targets.append(parts[-1])
                for t in targets:
                    dist = _levenshtein(tok, t)
                    max_d = 1 if len(tok) <= 8 else 2
                    if dist == 0 or dist > max_d:
                        continue
                    if best is None or dist < best[0]:
                        best = (dist, dkey, distrito)
            if best:
                dkey, distrito = best[1], best[2]
                add_cand(
                    tok,
                    _word_find(nblob, tok),
                    self.distrito_to_dept[dkey],
                    distrito,
                    penalty=2,
                )

        municipio = ""
        if candidates:
            pool = candidates
            if dept:
                same = [c for c in candidates if norm(c[3]) == norm(dept)]
                if same:
                    pool = same
            # Preferir no-homonimos; si solo hay homonimo, usarlo
            non_homo = [c for c in pool if c[0] < 3]
            if non_homo:
                pool = non_homo
            pool.sort()
            municipio = pool[0][4]
            if not dept:
                dept = pool[0][3]

        if not municipio:
            for mkey, dpt in sorted(
                self.municipio_nuevo_to_dept.items(), key=lambda x: -len(x[0])
            ):
                if _word_find(nblob, mkey) >= 0:
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

        if "olocuilta" in nblob:
            dept = _canon_dept("La Paz", self.departments)
            municipio = "OLOCUILTA"

        # Si el municipio quedo igual al depto pero hay un spelling/fuzzy mejor, no forzar
        if municipio and dept and norm(municipio) == norm(dept):
            non_homo = [c for c in candidates if c[0] < 3 and norm(c[3]) == norm(dept)]
            if non_homo:
                non_homo.sort()
                municipio = non_homo[0][4]

        return dept, municipio


_CATALOG: UbicacionesCatalog | None = None


def get_catalog() -> UbicacionesCatalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = UbicacionesCatalog()
    return _CATALOG


def infer_location(direccion: str, referencia: str = "") -> tuple[str, str]:
    return get_catalog().infer(direccion, referencia)


def locations_for_ui() -> dict:
    """Departamentos y municipios (etiqueta Sistrack UPPER) para selects de la web."""
    catalog = get_catalog()
    by_dept: dict[str, list[str]] = {}
    for dkey, distrito in catalog.distrito_canon.items():
        dept = catalog.distrito_to_dept[dkey]
        by_dept.setdefault(dept, [])
        label = sistrack_city_label(distrito)
        if label not in by_dept[dept]:
            by_dept[dept].append(label)
    for dept in by_dept:
        by_dept[dept].sort()
    departments = sorted(by_dept.keys(), key=lambda d: norm(d))
    return {
        "departments": departments,
        "by_department": by_dept,
        "state_aliases": {d: catalog.state_for_sistrack(d) for d in departments},
    }


def state_label_for_sistrack(departamento: str) -> str:
    return get_catalog().state_for_sistrack(departamento)
