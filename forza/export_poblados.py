# -*- coding: utf-8 -*-
"""
Exporta el catálogo de poblados de Forza a CSV y XLSX.

Fuente preferida: API GetSettlementsCatalog (Home.aspx/API) al abrir Crear Guías.
Fallback: semilla local.

Uso:
  python -m forza.export_poblados
  python -m forza.export_poblados --headless
  python -m forza.export_poblados --seed-only

Credenciales: Ajustes de Order Track (data/settings.json) o env FORZA_*.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
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
                add(distrito.upper(), distrito, dept, "semilla_distrito")
                if (
                    mun_nuevo
                    and _norm(mun_nuevo) != _norm(distrito)
                    and not re.search(r"(?i)\b(norte|sur|este|oeste|centro)\b", mun_nuevo)
                ):
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


def rows_from_settlements_catalog(catalog: list[dict]) -> list[dict]:
    """Convierte Catalog de GetSettlementsCatalog a filas CSV."""
    found: dict[str, dict] = {}
    for item in catalog or []:
        label = (item.get("SettlementName") or "").strip()
        if not label or "," not in label:
            continue
        if _norm(label).startswith("qa "):
            continue
        col, mun, dep = _parse_label(label)
        mun = (item.get("TownshipName") or mun or "").strip()
        dep = (item.get("ProvinceName") or dep or "").strip()
        r = _row(col, mun, dep, "forza_portal")
        key = _norm(r["label_forza"])
        if key and key not in found:
            found[key] = r
    rows = list(found.values())
    rows.sort(key=lambda r: (_norm(r["departamento"]), _norm(r["municipio"]), _norm(r["colonia"])))
    return rows


def _unwrap_forza_payload(raw: str) -> dict | None:
    """Desanida d → Data → PayLoad (strings JSON escapados)."""
    try:
        outer = json.loads(raw)
        node = outer.get("d", outer)
        if isinstance(node, str):
            node = json.loads(node)
        data = node.get("Data", node)
        if isinstance(data, str):
            data = json.loads(data)
        payload = data.get("PayLoad", data)
        if isinstance(payload, str):
            payload = json.loads(payload)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _extract_catalog_from_bodies(bodies: list[str]) -> list[dict]:
    for raw in sorted(bodies, key=len, reverse=True):
        if "GetSettlementsCatalog" not in raw and "SettlementName" not in raw:
            continue
        payload = _unwrap_forza_payload(raw)
        if not payload:
            continue
        catalog = payload.get("Catalog")
        if isinstance(catalog, list) and catalog:
            print(
                f"  API {payload.get('Method')}: {len(catalog)} settlements "
                f"(status={payload.get('StatusCode')})"
            )
            return catalog
    return []


def _drain_api_bodies(driver) -> list[str]:
    """Lee performance log + CDP y devuelve cuerpos de Home.aspx/API."""
    bodies: list[str] = []
    try:
        entries = driver.get_log("performance")
    except Exception:
        entries = []
    request_ids: list[str] = []
    for entry in entries:
        try:
            msg = json.loads(entry.get("message") or "{}").get("message") or {}
        except Exception:
            continue
        if msg.get("method") != "Network.responseReceived":
            continue
        params = msg.get("params") or {}
        url = ((params.get("response") or {}).get("url") or "").lower()
        if "home.aspx/api" in url or "/fd/" in url:
            rid = params.get("requestId")
            if rid:
                request_ids.append(rid)
    for rid in request_ids:
        try:
            raw = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
            body = raw.get("body") or ""
            if raw.get("base64Encoded") or len(body) < 200:
                continue
            bodies.append(body)
        except Exception:
            continue
    return bodies


def scrape_forza_poblados(headless: bool = False) -> list[dict]:
    """Descarga GetSettlementsCatalog desde el portal (Crear Guías)."""
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    from forza.cargar_pedidos_forza import ForzaBot

    codigo, usuario, password = _load_forza_creds()
    if not codigo or not usuario or not password:
        raise SystemExit("Faltan credenciales Forza (Ajustes o FORZA_*).")

    orig_build = ForzaBot._build_driver

    def _build(self):
        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1156,933")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--lang=es-SV")
        opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        service = Service(ChromeDriverManager().install())
        import selenium.webdriver as wd

        return wd.Chrome(service=service, options=opts)

    ForzaBot._build_driver = _build
    bot = ForzaBot(headless=headless, dry_run=True)
    try:
        try:
            bot.driver.execute_cdp_cmd("Network.enable", {})
            bot.driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        except Exception:
            pass

        bot.login(codigo=codigo, usuario=usuario, password=password)
        bot.go_crear_guias()
        bot._dismiss_overlays()
        bot._pause(1.0)

        bodies = _drain_api_bodies(bot.driver)
        catalog = _extract_catalog_from_bodies(bodies)
        if not catalog:
            # Forzar carga abriendo el selector
            try:
                bot._step_destino_manual()
            except Exception:
                pass
            bot._abrir_selector_poblado()
            bot._pause(1.2)
            bodies.extend(_drain_api_bodies(bot.driver))
            catalog = _extract_catalog_from_bodies(bodies)

        if not catalog:
            raise RuntimeError("No se capturó GetSettlementsCatalog en la red")

        return rows_from_settlements_catalog(catalog)
    finally:
        bot.close()
        ForzaBot._build_driver = orig_build


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
            # Catálogo completo del portal: no mezclar semillas inventadas
            if len(scraped) >= 500:
                rows = scraped
            else:
                rows = merge_rows(scraped, seed)
            print(f"Portal: {len(scraped)} | Total: {len(rows)}")
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
