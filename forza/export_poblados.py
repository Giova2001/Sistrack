# -*- coding: utf-8 -*-
"""
Exporta el catálogo de poblados de Forza a CSV y XLSX.

Uso:
  python -m forza.export_poblados
  python -m forza.export_poblados --seed-only

Credenciales: Ajustes de Order Track (data/settings.json) o env FORZA_*.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PACKAGE_DIR = Path(__file__).resolve().parent
OUT_CSV = PACKAGE_DIR / "catalogo_ubicaciones_forza.csv"
OUT_XLSX = PACKAGE_DIR / "catalogo_ubicaciones_forza.xlsx"

# Ejemplos confirmados en portal / Selenium IDE
KNOWN_FORZA = [
    ("EL ARENAL", "Ciudad Delgado", "San Salvador"),
    ("Santa Ana", "Santa Ana Centro", "Santa Ana"),
    ("ZONA CENTRAL Ahuachapán", "Ahuachapán", "Ahuachapán"),
    ("ASHAPUCO", "Ahuachapán", "Ahuachapán"),
    ("CHANCUYO", "Ahuachapán", "Ahuachapán"),
    ("CHIPILAPA", "Ahuachapán", "Ahuachapán"),
    ("CUYANAUSUL", "Ahuachapán", "Ahuachapán"),
    ("EL ANONAL", "Ahuachapán", "Ahuachapán"),
    ("EL BARRO", "Ahuachapán", "Ahuachapán"),
    ("EL JUNQUILLO", "Ahuachapán", "Ahuachapán"),
    ("MONCAGUA", "San Juan Opico", "La Libertad"),
]


def _norm(s: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFD", s or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip().lower()


def _parse_label(label: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in (label or "").split(",")]
    parts = [p for p in parts if p]
    if len(parts) >= 3:
        return parts[0], parts[1], ", ".join(parts[2:]).strip() if len(parts) > 3 else parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    if len(parts) == 1:
        return parts[0], "", ""
    return "", "", ""


def _row(colonia: str, municipio: str, departamento: str, fuente: str = "") -> dict:
    col = (colonia or "").strip()
    mun = (municipio or "").strip()
    dep = (departamento or "").strip()
    label = ", ".join(p for p in (col, mun, dep) if p)
    return {
        "colonia": col,
        "municipio": mun,
        "departamento": dep,
        "label_forza": label,
        "alias_busqueda": col or mun,
        "fuente": fuente,
    }


def seed_rows() -> list[dict]:
    """Semilla: ejemplos Forza + distritos del catálogo SV (aproximación)."""
    rows: list[dict] = []
    seen: set[str] = set()

    def add(colonia: str, municipio: str, departamento: str, fuente: str) -> None:
        r = _row(colonia, municipio, departamento, fuente)
        key = _norm(r["label_forza"])
        if not key or key in seen:
            return
        seen.add(key)
        rows.append(r)

    for col, mun, dep in KNOWN_FORZA:
        add(col, mun, dep, "forza_confirmado")

    cat_path = ROOT / "sistrack" / "catalogo_ubicaciones_el_salvador.csv"
    if cat_path.exists():
        with cat_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                dept = (row.get("departamento") or "").strip()
                distrito = (row.get("distrito") or "").strip()
                mun_nuevo = (row.get("municipio") or "").strip()
                if not dept or not distrito:
                    continue
                # En Forza el "municipio" del label suele ser el distrito clásico
                add(distrito.upper(), distrito, dept, "semilla_distrito")
                # Variante con municipio nuevo (ej. Santa Ana Centro)
                if mun_nuevo and _norm(mun_nuevo) != _norm(distrito):
                    add(distrito.upper(), mun_nuevo, dept, "semilla_municipio_nuevo")

    rows.sort(key=lambda r: (_norm(r["departamento"]), _norm(r["municipio"]), _norm(r["colonia"])))
    return rows


def write_csv(rows: list[dict], path: Path = OUT_CSV) -> None:
    fields = ["colonia", "municipio", "departamento", "label_forza", "alias_busqueda", "fuente"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def write_xlsx(rows: list[dict], path: Path = OUT_XLSX) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Poblados Forza"
    headers = ["colonia", "municipio", "departamento", "label_forza", "alias_busqueda", "fuente"]
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h, "") for h in headers])
    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes = "A2"
    for col in ws.columns:
        letter = col[0].column_letter
        width = min(48, max(12, max(len(str(c.value or "")) for c in col) + 2))
        ws.column_dimensions[letter].width = width
    wb.save(path)


def _load_forza_creds() -> tuple[str, str, str]:
    import os

    codigo = os.getenv("FORZA_CODIGO", "").strip()
    usuario = os.getenv("FORZA_USUARIO", "").strip()
    password = os.getenv("FORZA_PASSWORD", "")
    settings = ROOT / "data" / "settings.json"
    if settings.exists():
        data = json.loads(settings.read_text(encoding="utf-8"))
        codigo = codigo or str(data.get("forza_codigo") or "").strip()
        usuario = usuario or str(data.get("forza_usuario") or "").strip()
        password = password or str(data.get("forza_password") or "")
    return codigo, usuario, password


def scrape_forza_poblados(headless: bool = False) -> list[dict]:
    """Abre el modal de poblados y recolecta labels visibles (A-Z + scroll)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    from forza.cargar_pedidos_forza import ForzaBot

    codigo, usuario, password = _load_forza_creds()
    if not codigo or not usuario or not password:
        raise SystemExit("Faltan credenciales Forza (Ajustes o FORZA_*).")

    bot = ForzaBot(headless=headless, dry_run=True)
    found: dict[str, dict] = {}
    try:
        bot.login(codigo=codigo, usuario=usuario, password=password)
        bot.go_crear_guias()
        bot._dismiss_overlays()

        # Abrir selector
        opened = False
        for sel in (
            "app-cotizador-corporativo app-select ion-icon[name='caret-down-outline']",
            "app-cotizador-corporativo .form-control ion-icon",
            "app-cotizador-corporativo app-select",
        ):
            for el in bot.driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    bot._js_click(el)
                    opened = True
                    break
            if opened:
                break
        if not opened:
            raise RuntimeError("No se abrió el selector de poblados")

        time.sleep(1.0)
        search = bot.driver.find_element(
            By.CSS_SELECTOR,
            "ion-modal input.searchbar-input, ion-searchbar input, input[type='search']",
        )

        queries = [""] + list("abcdefghijklmnopqrstuvwxyz") + [
            "san",
            "santa",
            "ciudad",
            "colonia",
            "el ",
            "la ",
            "res",
            "zona",
        ]

        def harvest() -> None:
            items = bot.driver.find_elements(
                By.CSS_SELECTOR, "ion-modal ion-item, ion-modal ion-list ion-item"
            )
            for el in items:
                try:
                    text = (el.text or "").strip()
                except Exception:
                    continue
                if not text or "," not in text:
                    continue
                col, mun, dep = _parse_label(text)
                if not col:
                    continue
                key = _norm(text)
                if key not in found:
                    found[key] = _row(col, mun, dep, "forza_portal")

            # Scroll virtual list
            try:
                bot.driver.execute_script(
                    """
                    const content = document.querySelector('ion-modal ion-content');
                    if (!content) return;
                    const sc = content.shadowRoot && content.shadowRoot.querySelector('.inner-scroll');
                    if (sc) sc.scrollTop += 600;
                    else content.scrollBy && content.scrollBy(0, 600);
                    """
                )
            except Exception:
                pass

        for q in queries:
            bot._set_native(search, q)
            time.sleep(0.55)
            for _ in range(8):
                before = len(found)
                harvest()
                time.sleep(0.2)
                if len(found) == before:
                    break
            print(f"  query={q!r} total={len(found)}")

        # Limpiar búsqueda y un último pase
        bot._set_native(search, "")
        time.sleep(0.4)
        for _ in range(15):
            before = len(found)
            harvest()
            if len(found) == before:
                break
    finally:
        bot.quit()

    rows = list(found.values())
    rows.sort(key=lambda r: (_norm(r["departamento"]), _norm(r["municipio"]), _norm(r["colonia"])))
    return rows


def merge_rows(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """Prioriza primary (portal) y completa con secondary."""
    out: dict[str, dict] = {}
    for r in secondary + primary:
        key = _norm(r.get("label_forza") or "")
        if not key:
            continue
        prev = out.get(key)
        if not prev or (r.get("fuente") == "forza_portal"):
            out[key] = r
        elif prev.get("fuente") != "forza_portal" and r.get("fuente") == "forza_confirmado":
            out[key] = r
    rows = list(out.values())
    rows.sort(key=lambda r: (_norm(r["departamento"]), _norm(r["municipio"]), _norm(r["colonia"])))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Exportar poblados Forza a CSV/XLSX")
    ap.add_argument("--seed-only", action="store_true", help="Solo semilla local, sin login")
    ap.add_argument("--headless", action="store_true", help="Chrome headless al scrapear")
    args = ap.parse_args()

    seed = seed_rows()
    if args.seed_only:
        rows = seed
        print(f"Semilla: {len(rows)} filas")
    else:
        print("Descargando poblados desde Forza…")
        try:
            scraped = scrape_forza_poblados(headless=args.headless)
            rows = merge_rows(scraped, seed)
            print(f"Portal: {len(scraped)} | Total mezclado: {len(rows)}")
        except Exception as exc:
            print(f"No se pudo scrapear ({exc}). Usando semilla local.")
            rows = seed

    write_csv(rows, OUT_CSV)
    write_xlsx(rows, OUT_XLSX)
    print(f"OK CSV : {OUT_CSV}")
    print(f"OK XLSX: {OUT_XLSX}")
    print(f"Filas  : {len(rows)}")


if __name__ == "__main__":
    main()
