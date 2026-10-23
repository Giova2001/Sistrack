"""
Carga masiva de pedidos a Express (Facilito ERP) desde CSV.
Basado en el flujo Selenium IDE: express2.side
  https://xpress.facilitoerp.com
"""

"""Carga asistida de pedidos CSV en la interfaz Express2 (Selenium IDE).

Por seguridad, el modo predeterminado NO guarda órdenes. Use --submit para
habilitar el guardado, después de verificar un pedido con --limit 1.
La selección del punto en el mapa requiere confirmación humana.
"""
from __future__ import annotations
import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from sistrack.coordenadas import extract_coordenadas, resolve_map_query
from sistrack.ubicaciones import infer_location, norm, state_label_for_sistrack

BASE_URL = "https://xpress.facilitoerp.com"
LOGIN_URL = f"{BASE_URL}/login"
ORDERS_NEW_URL = f"{BASE_URL}/tnt/shipments/new"

def phone(value):
    digits = re.sub(r'\D', '', re.split(r'[/;,]', value or '')[0])
    if digits.startswith('503') and len(digits) == 11: digits = digits[3:]
    return digits

def load_csv(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        for number, row in enumerate(csv.DictReader(f), 2):
            if not (row.get('nombre') or '').strip(): continue
            yield number, row

def get(row, key, default=''):
    return (row.get(key) or '').strip() or default

@dataclass
class Pedido:
    nombre: str
    telefono: str
    direccion: str
    referencia: str
    producto: str
    precio: str
    peso: str
    fecha_registro: str
    fecha_entrega: str
    notas: str
    departamento: str
    municipio: str
    payment_type: str
    fila: int
    emergencia: str = ""
    colonia: str = ""
    pagado: str = ""
    forza_label: str = ""
    devolucion: str = ""
    coordenadas: str = ""

    def find(self, locator, timeout=None):
        wait = WebDriverWait(self.driver, timeout) if timeout else self.wait
        return wait.until(EC.visibility_of_element_located(locator))

    def fill(self, element_id, value):
        el = self.find((By.ID, element_id))
        el.clear()
        el.send_keys(str(value))

    def login(self):
        self.driver.get(BASE_URL + '/login')
        email = os.getenv('EXPRESS2_EMAIL') or os.getenv('SISTRACK_EMAIL')
        password = os.getenv('EXPRESS2_PASSWORD') or os.getenv('SISTRACK_PASSWORD')
        if not email or not password:
            raise RuntimeError('Defina EXPRESS2_EMAIL y EXPRESS2_PASSWORD en variables de entorno.')
        self.fill('login-email', email)
        self.fill('login-password', password)
        self.driver.find_element(By.ID, 'login-password').send_keys(Keys.ENTER)
        self.wait.until(lambda d: '/login' not in d.current_url.rstrip('/').split('?')[0] or not d.find_elements(By.ID, 'login-email'))

    def open_order(self):
        # Navegar con enlaces observados en express2.side, sin asumir una URL interna.
        self.wait.until(EC.element_to_be_clickable((By.LINK_TEXT, 'Ordenes'))).click()
        self.wait.until(EC.element_to_be_clickable((By.LINK_TEXT, 'Crear Orden'))).click()
        self.find((By.ID, 'legacy-contents'))

def infer_payment(notas: str) -> str:
    n = norm(notas)
    if "transferencia" in n:
        return "Transferencia"
    if "pagado" in n:
        # Si no hay opción exacta, se intenta Transferencia y luego Efectivo
        return "Transferencia"
    return DEFAULT_PAYMENT


def normalize_notes(notas: str) -> str:
    """Si la nota es/contiene solo 'envío gratis', usar contacto estándar."""
    raw = (notas or "").strip()
    if not raw:
        return DEFAULT_OBSERVATIONS
    # Quitar variantes de "envío gratis"
    without = re.sub(r"(?i)env[ií]o\s*gratis", "", raw).strip(" -|;,.!")
    if not without:
        return DEFAULT_OBSERVATIONS
    # Si quedaba "envío gratis" junto a otro texto, ya removido
    return without


def build_observations(pedido: Pedido) -> str:
    note = normalize_notes(pedido.notas)
    note = note or DEFAULT_OBSERVATIONS
    emerg = getattr(pedido, "emergencia", "") or ""
    # emergencia puede venir en notas ya; evitar duplicar
    if emerg and emerg not in note:
        note = f"{note} | Emergencia: {emerg}".strip(" |")
    return note


def load_pedidos(csv_path: Path) -> list[Pedido]:
    pedidos: list[Pedido] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            nombre = (row.get("nombre") or "").strip()
            if not nombre:
                continue
            direccion = (row.get("direccion") or "").strip()
            referencia = (row.get("punto de referencia") or "").strip()
            notas = (row.get("notas de entrega") or "").strip()
            precio = (row.get("precio") or "").strip() or DEFAULT_PRICE
            peso = (row.get("peso") or "").strip() or DEFAULT_WEIGHT
            dept, city = infer_location(direccion, referencia)
            coordenadas = (
                row.get("coordenadas")
                or row.get("coordenadas / maps")
                or row.get("maps")
                or ""
            ).strip()
            if not coordenadas:
                coordenadas = extract_coordenadas(
                    " ".join([direccion, referencia, notas])
                )
            pedidos.append(
                Pedido(
                    nombre=nombre,
                    telefono=clean_phone(row.get("telefono") or ""),
                    direccion=direccion,
                    referencia=referencia or "Sin referencia",
                    producto=(row.get("producto") or "").strip() or "Producto",
                    precio=precio,
                    peso=peso,
                    fecha_registro=(row.get("fecha de registro") or "").strip(),
                    fecha_entrega=(row.get("fecha de entrega") or "").strip()
                    or DEFAULT_DELIVERY_DATE,
                    notas=notas,
                    departamento=dept,
                    municipio=city,
                    payment_type=infer_payment(notas),
                    fila=i,
                    coordenadas=coordenadas,
                )
            )
    return pedidos


def format_phone_hyphen(raw: str) -> str:
    digits = clean_phone(raw)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:]}"
    return digits or (raw or "").strip()


class SistrackBot:
    """Bot de Express El Salvador en Facilito ERP (xpress.facilitoerp.com)."""

    def __init__(self, headless: bool = False, dry_run: bool = False):
        self.dry_run = dry_run
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )
        self.wait = WebDriverWait(self.driver, 25)

    def quit(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass

    def login(self, email: str | None = None, password: str | None = None) -> None:
        user = (email or EMAIL or "").strip()
        pwd_val = (password if password is not None else PASSWORD) or ""
        if not user or not pwd_val:
            raise RuntimeError("Faltan credenciales de Express (email/contraseña)")
        self.driver.get(LOGIN_URL)
        email_el = self.wait.until(
            EC.presence_of_element_located((By.ID, "login-email"))
        )
        self._set_value(email_el, user)
        pwd = self.driver.find_element(By.ID, "login-password")
        self._set_value(pwd, pwd_val)
        self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        self.wait.until(
            EC.any_of(
                EC.presence_of_element_located((By.LINK_TEXT, "Ordenes")),
                EC.presence_of_element_located((By.LINK_TEXT, "Crear Orden")),
                EC.url_contains("/tnt"),
            )
        )
        time.sleep(1)

    def go_crear_orden(self) -> None:
        self.driver.get(ORDERS_NEW_URL)
        self.wait.until(
            EC.any_of(
                EC.presence_of_element_located((By.ID, "legacy-contents")),
                EC.presence_of_element_located(
                    (By.XPATH, "//a[contains(.,'Crear Orden') or contains(.,'Crear')]")
                ),
                EC.presence_of_element_located(
                    (By.XPATH, "//button[contains(.,'Crear') and not(contains(.,'agregar'))]")
                ),
            )
        )
        time.sleep(0.5)

    def _set_value(self, el, value: str) -> None:
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el
        )
        time.sleep(0.1)
        self.driver.execute_script(
            """
            const el = arguments[0], val = arguments[1];
            el.focus();
            const proto = el.tagName === 'TEXTAREA'
              ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) desc.set.call(el, val);
            else el.value = val;
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            """,
            el,
            value or "",
        )

    def _fill(self, element_id: str, value: str) -> None:
        el = self.wait.until(EC.presence_of_element_located((By.ID, element_id)))
        self._set_value(el, value)

    def _click_js(self, el) -> None:
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el
        )
        time.sleep(0.15)
        self.driver.execute_script("arguments[0].click();", el)

    def _dismiss_toasts(self) -> None:
        try:
            self.driver.execute_script(
                """
                document.querySelectorAll(
                  '.toasted, .toasted-container, [role="status"], [data-sonner-toast]'
                ).forEach(e => e.remove());
                """
            )
        except Exception:
            pass

    def click_crear_destinatario_modal(self) -> None:
        """Abre el formulario de destinatario (botón Crear, no 'Crear y agregar otro')."""
        try:
            if self.driver.find_element(By.ID, "legacy-recipient-name").is_displayed():
                return
        except Exception:
            pass
        self._dismiss_toasts()
        candidates = [
            (By.XPATH, "//button[normalize-space()='Crear']"),
            (By.XPATH, "//button[contains(.,'Crear') and not(contains(.,'agregar')) and not(contains(.,'Destinatario')) and not(contains(.,'Orden'))]"),
            (By.CSS_SELECTOR, ".ml-1"),
        ]
        opened = False
        for by, sel in candidates:
            try:
                for el in self.driver.find_elements(by, sel):
                    try:
                        if not el.is_displayed():
                            continue
                        txt = (el.text or "").strip().lower()
                        if "agregar" in txt or "destinatario" in txt or "orden" in txt:
                            continue
                        self._click_js(el)
                        WebDriverWait(self.driver, 6).until(
                            EC.presence_of_element_located((By.ID, "legacy-recipient-name"))
                        )
                        opened = True
                        break
                    except Exception:
                        continue
                if opened:
                    break
            except Exception:
                continue
        if not opened:
            self.wait.until(
                EC.presence_of_element_located((By.ID, "legacy-recipient-name"))
            )
        time.sleep(0.3)

    def _visible_selects(self):
        return [
            s
            for s in self.driver.find_elements(By.TAG_NAME, "select")
            if s.is_displayed()
        ]

    def _select_option(self, select_el, label: str) -> str:
        select = Select(select_el)
        if not label:
            return ""
        try:
            select.select_by_visible_text(label)
            return label
        except NoSuchElementException:
            pass
        try:
            select.select_by_value(label)
            return label
        except NoSuchElementException:
            pass
        target = norm(label).replace(" ", "")
        for opt in select.options:
            text = (opt.text or "").strip()
            val = (opt.get_attribute("value") or "").strip()
            if not text or text in ("-",):
                continue
            blob = norm(text).replace(" ", "")
            blob_val = norm(val).replace(" ", "")
            if target and (target == blob or target == blob_val or target in blob):
                select.select_by_visible_text(text)
                self.driver.execute_script(
                    "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                    select_el,
                )
                return text
        return ""

    def _select_departamento(self, departamento: str) -> None:
        label = state_label_for_sistrack(departamento) or departamento
        end = time.time() + 8
        while time.time() < end:
            sels = self._visible_selects()
            if sels:
                chosen = self._select_option(sels[0], label) or self._select_option(
                    sels[0], departamento
                )
                if chosen:
                    return
            time.sleep(0.2)

    def _select_municipio(self, preferred: str, departamento: str) -> str:
        end = time.time() + 10
        while time.time() < end:
            sels = self._visible_selects()
            if len(sels) >= 2:
                select = Select(sels[1])
                opts = [
                    (o.text or "").strip()
                    for o in select.options
                    if (o.text or "").strip()
                    and "seleccion" not in norm(o.text or "")
                ]
                if len(opts) >= 2 or (preferred and any(norm(preferred) in norm(o) for o in opts)):
                    chosen = self._select_option(sels[1], preferred)
                    if chosen:
                        return chosen
                    if departamento:
                        chosen = self._select_option(sels[1], departamento)
                        if chosen:
                            return chosen
                    for text in opts:
                        if text and text != "-":
                            Select(sels[1]).select_by_visible_text(text)
                            return text
            time.sleep(0.25)
        return ""

    def _quitar_punto(self) -> None:
        try:
            for el in self.driver.find_elements(
                By.XPATH, "//button[contains(.,'Quitar punto')]"
            ):
                if el.is_displayed():
                    self._click_js(el)
                    time.sleep(0.3)
                    return
        except Exception:
            pass

    def _leaflet_set(self, lat: float, lng: float) -> bool:
        try:
            return bool(
                self.driver.execute_script(
                    """
                    const lat = arguments[0], lng = arguments[1];
                    const el = document.querySelector('.leaflet-container');
                    if (!el) return false;
                    let map = el._leaflet_map || null;
                    if (!map) {
                      for (const k of Object.keys(el)) {
                        try {
                          const v = el[k];
                          if (v && typeof v.setView === 'function' && typeof v.fire === 'function') {
                            map = v; break;
                          }
                        } catch (e) {}
                      }
                    }
                    if (!map) return false;
                    const ll = (window.L && L.latLng) ? L.latLng(lat, lng) : {lat, lng};
                    try { map.setView(ll, 16); } catch (e) {}
                    try { map.fire('click', { latlng: ll }); } catch (e) {}
                    return true;
                    """,
                    lat,
                    lng,
                )
            )
        except Exception:
            return False

    def _search_map(self, query: str) -> None:
        q = (query or "").strip()
        if not q:
            return
        el = self.wait.until(
            EC.presence_of_element_located((By.ID, "legacy-recipient-search"))
        )
        self._set_value(el, q)
        time.sleep(0.2)
        try:
            el.send_keys(Keys.ENTER)
        except Exception:
            self.driver.execute_script(
                """
                arguments[0].dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}));
                arguments[0].dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', bubbles:true}));
                """,
                el,
            )
        time.sleep(1.1)
        # Si aparece un dropdown de resultados, tomar el primero
        try:
            items = self.driver.find_elements(
                By.CSS_SELECTOR,
                "[role='option'], .leaflet-control-geocoder-alternatives li, li[class*='result']",
            )
            for item in items:
                if item.is_displayed():
                    self._click_js(item)
                    time.sleep(0.4)
                    break
        except Exception:
            pass

    def _set_map_point(self, pedido: Pedido) -> None:
        self._quitar_punto()
        resolved = resolve_map_query(getattr(pedido, "coordenadas", "") or "")
        lat, lng = resolved.get("lat"), resolved.get("lng")
        query = (resolved.get("query") or "").strip()
        if lat is not None and lng is not None:
            if self._leaflet_set(float(lat), float(lng)):
                time.sleep(0.4)
                return
            query = query or f"{lat},{lng}"
        if not query:
            ref = (pedido.referencia or "").strip()
            if ref and norm(ref) != "sin referencia":
                query = ref
            else:
                query = (pedido.direccion or "").strip()
        if query:
            self._search_map(query)
            if lat is not None and lng is not None:
                self._leaflet_set(float(lat), float(lng))

    def crear_destinatario(self, pedido: Pedido) -> None:
        self.click_crear_destinatario_modal()
        self._fill("legacy-recipient-name", pedido.nombre)
        self._fill("legacy-recipient-phone", format_phone_hyphen(pedido.telefono))
        self._fill("legacy-recipient-address", pedido.direccion)
        self._fill(
            "legacy-recipient-reference",
            pedido.referencia or "Sin referencia",
        )
        self._select_departamento(pedido.departamento)
        city = self._select_municipio(pedido.municipio, pedido.departamento)
        if city:
            pedido.municipio = city
        self._set_map_point(pedido)
        self._dismiss_toasts()
        btn = self.wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[contains(.,'Crear Destinatario')]",
                )
            )
        )
        self._click_js(btn)
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "legacy-contents"))
            )
        except TimeoutException:
            time.sleep(0.8)
        self._dismiss_toasts()

    def llenar_orden(self, pedido: Pedido) -> None:
        self._fill("legacy-contents", pedido.producto)
        pagado = norm(getattr(pedido, "pagado", "") or "")
        cod = "0" if pagado in ("si", "yes", "true", "1", "pagado") else str(pedido.precio or "0")
        self._fill("legacy-cod", cod)
        if DEFAULT_FRAGILE:
            try:
                fragile = self.wait.until(
                    EC.presence_of_element_located((By.ID, "legacy-fragile"))
                )
                if not fragile.is_selected():
                    self._click_js(fragile)
            except (NoSuchElementException, TimeoutException):
                print("  AVISO: no se pudo marcar 'Frágil'")
        self._fill("legacy-notes", build_observations(pedido))

    def guardar_y_agregar_otro(self) -> None:
        if self.dry_run:
            print("  [dry-run] No se guarda la orden.")
            return
        self._dismiss_toasts()
        try:
            prev = self.driver.find_element(By.ID, "legacy-contents").get_attribute("value") or ""
        except Exception:
            prev = ""
        btn = self.wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(.,'Crear y agregar otro')]")
            )
        )
        self._click_js(btn)
        end = time.time() + 14
        ok = False
        while time.time() < end:
            try:
                el = self.driver.find_element(By.ID, "legacy-contents")
                val = el.get_attribute("value") or ""
                if val == "" or (prev and val != prev):
                    ok = True
                    break
            except StaleElementReferenceException:
                ok = True
                break
            except Exception:
                pass
            if "shipments/new" in (self.driver.current_url or ""):
                try:
                    name_el = self.driver.find_elements(By.ID, "legacy-recipient-name")
                    if name_el and not (self.driver.find_element(By.ID, "legacy-contents").get_attribute("value") or ""):
                        ok = True
                        break
                except Exception:
                    pass
            time.sleep(0.25)
        if not ok and "shipments" in (self.driver.current_url or ""):
            ok = True
        if not ok:
            raise RuntimeError("No se confirmó el guardado de la orden en Express")
        self._dismiss_toasts()

    def procesar_pedido(self, pedido: Pedido, index: int, total: int) -> None:
        print(
            f"[{index}/{total}] Fila {pedido.fila}: {pedido.nombre} | "
            f"{pedido.departamento}/{pedido.municipio or '?'} | ${pedido.precio}"
            + (f" | geo={pedido.coordenadas}" if getattr(pedido, "coordenadas", "") else "")
        )
        if "shipments/new" not in (self.driver.current_url or ""):
            self.go_crear_orden()
        self.crear_destinatario(pedido)
        self.llenar_orden(pedido)
        self.guardar_y_agregar_otro()
        print(f"  OK -> {pedido.producto[:60]}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cargar pedidos CSV a Express (Facilito)")
    p.add_argument(
        "--csv",
        default=r"c:\Users\Chris Garcia\Downloads\pedidos_estandarizados.csv",
        help="Ruta al CSV de pedidos",
    )
    p.add_argument("--headless", action="store_true", help="Chrome sin ventana")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Llena formularios pero no hace click en guardar",
    )
    p.add_argument("--limit", type=int, default=0, help="Procesar solo N pedidos (0=todos)")
    p.add_argument("--start", type=int, default=1, help="Empezar desde el pedido N (1-based)")
    p.add_argument(
        "--pause",
        type=float,
        default=0.5,
        help="Pausa extra entre pedidos (segundos)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"No existe el CSV: {csv_path}", file=sys.stderr)
        return 1

    pedidos = load_pedidos(csv_path)
    if not pedidos:
        print("No hay pedidos en el CSV.", file=sys.stderr)
        return 1

    start_idx = max(args.start, 1) - 1
    pedidos = pedidos[start_idx:]
    if args.limit > 0:
        pedidos = pedidos[: args.limit]

    print(f"Pedidos a cargar: {len(pedidos)}")
    print(f"Usuario: {EMAIL}")
    if args.dry_run:
        print("Modo dry-run: NO se guardarán órdenes.")

    bot = SistrackBot(headless=args.headless, dry_run=args.dry_run)
    ok = 0
    errors: list[str] = []
    pendientes: list[str] = []
    try:
        bot.login()
        bot.go_crear_orden()
        total = len(pedidos)
        for i, pedido in enumerate(pedidos, start=1):
            try:
                bot.procesar_pedido(pedido, i, total)
                ok += 1
                time.sleep(args.pause)
            except Exception as e:
                msg = f"Error en fila {pedido.fila} ({pedido.nombre}): {e}"
                print(f"  FAIL: {msg}")
                print("  STOP: se detiene la carga para evitar pedidos repetidos.")
                errors.append(msg)
                # Pedidos que no se alcanzaron a procesar (este + siguientes)
                restantes = pedidos[i - 1 :]
                pendientes = [f"{p.nombre} (fila CSV {p.fila})" for p in restantes]
                if len(restantes) > 1:
                    print(
                        f"  Pendientes ({len(restantes)}), reanuda con: "
                        f"--start {i}"
                    )
                break
    finally:
        print(f"\nListo. OK={ok}  Errores={len(errors)}")
        if errors:
            print("Detalle de errores:")
            for e in errors:
                print(f"  - {e}")
        if pendientes:
            print("No cargados (por el stop):")
            for p in pendientes:
                print(f"  - {p}")
        if not args.headless:
            print("Cierra la ventana del navegador o presiona Enter para salir...")
            try:
                input()
            except EOFError:
                time.sleep(3)
        bot.quit()

    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
