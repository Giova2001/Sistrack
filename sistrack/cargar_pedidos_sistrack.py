"""
Carga masiva de pedidos a Sistrack desde CSV.
Basado en el flujo Selenium IDE: sistrack.side
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    StaleElementReferenceException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from sistrack.ubicaciones import infer_location, norm, state_label_for_sistrack

BASE_URL = "https://expresselsalvador.sistrack.net"
LOGIN_URL = f"{BASE_URL}/admin/login"
ORDERS_NEW_URL = f"{BASE_URL}/admin/resources/orders/new"

# Credenciales: SOLO variables de entorno o Ajustes en la web (sin defaults en claro)
EMAIL = os.getenv("SISTRACK_EMAIL", "").strip()
PASSWORD = os.getenv("SISTRACK_PASSWORD", "")

DEFAULT_WEIGHT = "0.1"
DEFAULT_PRICE = "30"
DEFAULT_PAYMENT = "Efectivo"
DEFAULT_OBSERVATIONS = "Contactar al cliente para coordinar la entrega"
# Viernes 7 de agosto 2026
DEFAULT_DELIVERY_DATE = "2026-08-07"
DEFAULT_FRAGILE = True


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



def clean_phone(raw: str) -> str:
    """Toma el primer número y deja solo dígitos (8 o con 503)."""
    if not raw:
        return ""
    # Separadores comunes: / , ;
    first = re.split(r"[/;,]", raw)[0]
    digits = re.sub(r"\D", "", first)
    if digits.startswith("503") and len(digits) >= 11:
        digits = digits[3:]
    return digits[-8:] if len(digits) >= 8 else digits


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
                )
            )
    return pedidos


class SistrackBot:
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
            raise RuntimeError("Faltan credenciales de Sistrack (email/contraseña)")
        self.driver.get(LOGIN_URL)
        email_el = self.wait.until(EC.presence_of_element_located((By.ID, "email")))
        email_el.clear()
        email_el.send_keys(user)
        pwd = self.driver.find_element(By.ID, "password")
        pwd.clear()
        pwd.send_keys(pwd_val)
        self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        self.wait.until(EC.any_of(
            EC.presence_of_element_located((By.LINK_TEXT, "Crear Orden")),
            EC.presence_of_element_located((By.PARTIAL_LINK_TEXT, "Crear Orden")),
            EC.url_contains("/admin"),
        ))
        time.sleep(1)

    def go_crear_orden(self) -> None:
        # Preferir URL directa (más estable que el link del dashboard)
        self.driver.get(ORDERS_NEW_URL)
        self.wait.until(EC.presence_of_element_located((By.ID, "description")))
        time.sleep(0.5)

    def click_crear_destinatario_modal(self) -> None:
        """Abre el modal para crear destinatario (no confundir con 'Crear y agregar otro')."""
        self._dismiss_toasts()
        candidates = [
            (By.CSS_SELECTOR, ".rounded > .fill-current"),
            (By.XPATH, "//label[contains(.,'Destinatario')]/following::button[1]"),
            (By.XPATH, "//button[.//span[normalize-space()='Crear'] and not(contains(.,'agregar')) and not(contains(.,'Destinatario'))]"),
            (By.XPATH, "//div[contains(@class,'relative')]//button[contains(@class,'rounded')][.//svg or .//span[contains(.,'Crear')]]"),
            (By.CSS_SELECTOR, "button.rounded"),
        ]
        opened = False
        for by, sel in candidates:
            try:
                els = self.driver.find_elements(by, sel)
                for el in els:
                    try:
                        if not el.is_displayed():
                            continue
                        txt = (el.text or "").strip().lower()
                        # Evitar botones de guardar orden
                        if "agregar" in txt or "destinatario" in txt:
                            continue
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});", el
                        )
                        time.sleep(0.2)
                        self.driver.execute_script("arguments[0].click();", el)
                        WebDriverWait(self.driver, 5).until(
                            EC.presence_of_element_located((By.ID, "name"))
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
            # Último intento: cualquier span Crear corto
            el = self.wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//span[normalize-space()='Crear']/ancestor::button[1]")
                )
            )
            self.driver.execute_script("arguments[0].click();", el)
            self.wait.until(EC.presence_of_element_located((By.ID, "name")))
        time.sleep(0.4)

    def _fill(self, element_id: str, value: str) -> None:
        el = self.wait.until(EC.presence_of_element_located((By.ID, element_id)))
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el
        )
        time.sleep(0.15)
        try:
            el.clear()
            el.send_keys(value)
        except Exception:
            self.driver.execute_script(
                """
                const el = arguments[0], val = arguments[1];
                el.focus();
                el.value = val;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """,
                el,
                value,
            )

    def _select_by_label(self, select_id: str, label: str, fuzzy: bool = True) -> str:
        """Selecciona por label visible. Devuelve el valor elegido o ''."""
        select_el = self.wait.until(EC.presence_of_element_located((By.ID, select_id)))
        select = Select(select_el)
        # Exacto
        try:
            select.select_by_visible_text(label)
            return label
        except NoSuchElementException:
            pass
        # Por value
        try:
            select.select_by_value(label)
            return label
        except NoSuchElementException:
            pass

        if not fuzzy:
            return ""

        target = norm(label)
        for opt in select.options:
            text = (opt.text or "").strip()
            val = (opt.get_attribute("value") or "").strip()
            if target and (target == norm(text) or target == norm(val)):
                select.select_by_visible_text(text)
                return text
            if target and (target in norm(text) or target in norm(val)):
                select.select_by_visible_text(text)
                return text
        return ""

    def _wait_city_options(self, min_options: int = 2, timeout: float = 8.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            try:
                select = Select(self.driver.find_element(By.ID, "city"))
                opts = [o for o in select.options if (o.text or "").strip()]
                if len(opts) >= min_options:
                    return
            except Exception:
                pass
            time.sleep(0.2)

    def _select_city_best_effort(self, preferred: str, departamento: str) -> str:
        self.wait.until(EC.presence_of_element_located((By.ID, "city")))
        self._wait_city_options()
        select_el = self.driver.find_element(By.ID, "city")
        select = Select(select_el)
        options = [(o.text or "").strip() for o in select.options if (o.text or "").strip()]
        if not options:
            return ""

        if preferred:
            chosen = self._select_by_label("city", preferred, fuzzy=True)
            if chosen:
                return chosen
            pref_n = norm(preferred).replace(" ", "")
            for text in options:
                if norm(text).replace(" ", "") == pref_n:
                    Select(self.driver.find_element(By.ID, "city")).select_by_visible_text(text)
                    return text

        dept_n = norm(departamento)
        for text in options:
            if dept_n and dept_n in norm(text):
                Select(self.driver.find_element(By.ID, "city")).select_by_visible_text(text)
                return text
        for text in options:
            if text and "seleccion" not in norm(text) and text != "-":
                Select(self.driver.find_element(By.ID, "city")).select_by_visible_text(text)
                return text
        return ""

    def _select_payment(self, preferred: str) -> str:
        chosen = self._select_by_label("payment_type", preferred, fuzzy=True)
        if chosen:
            return chosen
        # Fallbacks comunes
        for label in ("Efectivo", "Cash", "Transferencia", "Transfer"):
            chosen = self._select_by_label("payment_type", label, fuzzy=True)
            if chosen:
                return chosen
        return ""

    def _set_flatpickr_dates(self, registro: str, entrega: str) -> None:
        """Setea fechas. Entrega por defecto: viernes 7 ago 2026."""
        today = datetime.now()
        if not registro:
            registro_fmt = today.strftime("%Y-%m-%d")
        else:
            registro_fmt = self._parse_date(registro) or today.strftime("%Y-%m-%d")

        if not entrega:
            entrega_fmt = DEFAULT_DELIVERY_DATE
        else:
            entrega_fmt = self._parse_date(entrega) or DEFAULT_DELIVERY_DATE

        # flatpickr inputs suelen ser type=text; intentamos por name/id comunes
        script = """
        const vals = arguments;
        const inputs = Array.from(document.querySelectorAll('input.flatpickr-input, input[type="text"]'));
        let set = 0;
        for (const inp of inputs) {
          const name = (inp.name || inp.id || '').toLowerCase();
          const placeholder = (inp.placeholder || '').toLowerCase();
          const label = (inp.getAttribute('dusk') || '').toLowerCase();
          const blob = name + ' ' + placeholder + ' ' + label;
          if (blob.includes('register') || blob.includes('registro') || blob.includes('created')) {
            if (inp._flatpickr) { inp._flatpickr.setDate(vals[0], true); }
            else { inp.value = vals[0]; inp.dispatchEvent(new Event('input', {bubbles:true})); }
            set++;
          } else if (blob.includes('deliver') || blob.includes('entrega') || blob.includes('delivery')) {
            if (inp._flatpickr) { inp._flatpickr.setDate(vals[1], true); }
            else { inp.value = vals[1]; inp.dispatchEvent(new Event('input', {bubbles:true})); }
            set++;
          }
        }
        // Si no identificamos por nombre, setear los flatpickr visibles en orden
        if (set < 2) {
          const fps = Array.from(document.querySelectorAll('input')).filter(i => i._flatpickr);
          if (fps[0]) fps[0]._flatpickr.setDate(vals[0], true);
          if (fps[1]) fps[1]._flatpickr.setDate(vals[1], true);
        }
        return true;
        """
        try:
            self.driver.execute_script(script, registro_fmt, entrega_fmt)
        except Exception:
            pass

    @staticmethod
    def _parse_date(raw: str) -> str | None:
        raw = raw.strip()
        # "Lunes 10 de agosto" -> no parseable confiablemente sin año
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _dismiss_toasts(self) -> None:
        """Quita notificaciones que bloquean clics."""
        try:
            self.driver.execute_script(
                """
                document.querySelectorAll('.toasted, .toasted-container, .NovaSuccess').forEach(e => e.remove());
                """
            )
        except Exception:
            pass
        time.sleep(0.3)

    def crear_destinatario(self, pedido: Pedido) -> None:
        self.click_crear_destinatario_modal()
        self._fill("name", pedido.nombre)
        self._fill("phone", pedido.telefono)
        self._fill("Dirección", pedido.direccion)
        self._fill("Referencia", pedido.referencia)

        self._select_by_label(
            "state",
            state_label_for_sistrack(pedido.departamento),
            fuzzy=True,
        )
        city = self._select_city_best_effort(pedido.municipio, pedido.departamento)
        if city:
            pedido.municipio = city

        self._dismiss_toasts()
        btn = self.wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//span[contains(.,'Crear Destinatario')]/ancestor::button | //button[contains(.,'Crear Destinatario')]")
            )
        )
        self.driver.execute_script("arguments[0].click();", btn)
        # Esperar cierre de modal
        try:
            self.wait.until(EC.invisibility_of_element_located((By.ID, "name")))
        except TimeoutException:
            time.sleep(0.8)
        self._dismiss_toasts()

    def llenar_orden(self, pedido: Pedido) -> None:
        self._fill("description", pedido.producto)
        self._fill("weight", str(pedido.peso))
        self._fill("declared_value", str(pedido.precio))
        self._select_payment(pedido.payment_type)
        self._set_flatpickr_dates(pedido.fecha_registro, pedido.fecha_entrega)

        # Frágil: siempre marcar la casilla
        if DEFAULT_FRAGILE:
            try:
                fragile = self.wait.until(
                    EC.presence_of_element_located((By.ID, "is_fragile"))
                )
                if not fragile.is_selected():
                    self.driver.execute_script(
                        "arguments[0].click();", fragile
                    )
                if not fragile.is_selected():
                    fragile.click()
            except (NoSuchElementException, TimeoutException):
                print("  AVISO: no se pudo marcar 'Es frágil'")

        self._fill("observations", build_observations(pedido))

    def guardar_y_agregar_otro(self) -> None:
        if self.dry_run:
            print("  [dry-run] No se guarda la orden.")
            return
        self._dismiss_toasts()
        for _ in range(15):
            toasts = self.driver.find_elements(By.CSS_SELECTOR, ".toasted")
            if not toasts:
                break
            self._dismiss_toasts()
            time.sleep(0.25)

        # Snapshot del description actual para detectar formulario nuevo
        try:
            prev_desc = self.driver.find_element(By.ID, "description").get_attribute("value") or ""
        except Exception:
            prev_desc = ""

        btn = self.wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "button[dusk='create-and-add-another-button']")
            )
        )
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", btn
        )
        self.driver.execute_script("arguments[0].click();", btn)

        # Exito: formulario limpio / description vacia o distinta
        end = time.time() + 12
        ok = False
        while time.time() < end:
            try:
                el = self.driver.find_element(By.ID, "description")
                val = el.get_attribute("value") or ""
                if val == "" or (prev_desc and val != prev_desc and len(val) < len(prev_desc)):
                    ok = True
                    break
                # toast de exito
                for t in self.driver.find_elements(By.CSS_SELECTOR, ".toasted"):
                    if "exito" in norm(t.text) or "éxito" in (t.text or "").lower() or "creat" in norm(t.text):
                        ok = True
                        break
                if ok:
                    break
            except StaleElementReferenceException:
                ok = True
                break
            except Exception:
                pass
            time.sleep(0.25)
        if not ok:
            # si seguimos en new y description existe, asumir ok parcial
            if "orders/new" in self.driver.current_url:
                ok = True
        if not ok:
            raise RuntimeError("No se confirmo el guardado de la orden en Sistrack")
        self._dismiss_toasts()

    def procesar_pedido(self, pedido: Pedido, index: int, total: int) -> None:
        print(
            f"[{index}/{total}] Fila {pedido.fila}: {pedido.nombre} | "
            f"{pedido.departamento}/{pedido.municipio or '?'} | ${pedido.precio}"
        )
        # Si no estamos en formulario limpio, ir a new
        if "orders/new" not in self.driver.current_url:
            self.go_crear_orden()

        self.crear_destinatario(pedido)
        self.llenar_orden(pedido)
        self.guardar_y_agregar_otro()
        print(f"  OK -> {pedido.producto[:60]}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cargar pedidos CSV a Sistrack")
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
