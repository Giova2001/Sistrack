from __future__ import annotations

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
import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from webdriver_manager.chrome import ChromeDriverManager

from sistrack.coordenadas import extract_coordenadas, resolve_map_query
from sistrack.ubicaciones import (
    infer_location,
    norm,
    sistrack_municipio_candidates,
    state_label_for_sistrack,
)

BASE_URL = "https://xpress.facilitoerp.com"
LOGIN_URL = f"{BASE_URL}/login"
ORDERS_NEW_URL = f"{BASE_URL}/tnt/shipments/new"

DEFAULT_WEIGHT = "0.1"
DEFAULT_PRICE = "30"
DEFAULT_PAYMENT = "Efectivo"
DEFAULT_OBSERVATIONS = "Contactar al cliente para coordinar la entrega"
DEFAULT_DELIVERY_DATE = "2026-08-07"
DEFAULT_FRAGILE = True

# seguro espera a que Express termine cada orden antes de la siguiente.
# rapido usa las pausas cortas de antes.
_SPEED_PROFILES = {
    "seguro": {
        "step": 0.55,
        "after_select": 0.9,
        "after_map": 1.0,
        "after_modal": 1.0,
        "save_wait": 18.0,
        "settle": 2.2,
        "click_timeout": 18.0,
    },
    "rapido": {
        "step": 0.0,
        "after_select": 0.35,
        "after_map": 0.4,
        "after_modal": 0.2,
        "save_wait": 8.0,
        "settle": 0.35,
        "click_timeout": 12.0,
    },
}


def _normalize_speed_mode(mode: str | None) -> str:
    m = str(mode or "seguro").strip().lower()
    if m in ("rapido", "rápido", "fast", "quick"):
        return "rapido"
    return "seguro"


def phone(value):
    digits = re.sub(r'\D', '', re.split(r'[/;,]', value or '')[0])
    if digits.startswith('503') and len(digits) == 11: digits = digits[3:]
    return digits


def clean_phone(raw: str) -> str:
    """Toma el primer número y deja solo dígitos (8 o con 503)."""
    if not raw:
        return ""
    first = re.split(r"[/;,]", raw)[0]
    digits = re.sub(r"\D", "", first)
    if digits.startswith("503") and len(digits) >= 11:
        digits = digits[3:]
    return digits[-8:] if len(digits) >= 8 else digits

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

    def __init__(
        self,
        headless: bool = False,
        dry_run: bool = False,
        speed_mode: str = "seguro",
    ):
        self.dry_run = dry_run
        self.speed_mode = _normalize_speed_mode(speed_mode)
        self._speed = dict(_SPEED_PROFILES[self.speed_mode])
        print(
            f"  Express velocidad={self.speed_mode} "
            f"(espera guardado {self._speed['save_wait']:.0f}s, "
            f"pausa entre órdenes {self._speed['settle']:.1f}s)"
        )
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

    def _pace(self, key: str) -> None:
        delay = float(self._speed.get(key) or 0)
        if delay > 0:
            time.sleep(delay)

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

    def _pagina_express_rota(self) -> bool:
        """Express (Next) muestra esta pantalla al pasar al siguiente pedido."""
        try:
            text = self.driver.execute_script(
                "return document.body ? document.body.innerText : ''"
            ) or ""
        except Exception:
            return False
        low = text.lower()
        return "client-side exception" in low or "application error" in low

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
            """
            const el = arguments[0], val = arguments[1];
            if (!el) return;
            try { el.scrollIntoView({block:'center'}); } catch (e) {}
            try { el.focus(); } catch (e) {}
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
        self.wait.until(EC.presence_of_element_located((By.ID, element_id)))
        # Busca el nodo en el momento de escribir: el formulario se redibuja al tipear.
        self.driver.execute_script(
            """
            const el = document.getElementById(arguments[0]);
            if (!el) return;
            const val = arguments[1] || '';
            try { el.scrollIntoView({block:'center'}); } catch (e) {}
            try { el.focus(); } catch (e) {}
            const proto = el.tagName === 'TEXTAREA'
              ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) desc.set.call(el, val);
            else el.value = val;
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            """,
            element_id,
            value or "",
        )
        self._pace("step")

    def _click_js(self, el) -> None:
        self.driver.execute_script(
            """
            const el = arguments[0];
            try { el.scrollIntoView({block:'center'}); } catch (e) {}
            el.click();
            """,
            el,
        )

    def _dismiss_overlays(self, *, allow_escape: bool = True) -> None:
        """Cierra toasts y backdrops que tapan botones. No cancela un guardar."""
        self._dismiss_toasts()
        self._confirm_pending_dialog()
        if allow_escape:
            try:
                self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
            except Exception:
                pass
        try:
            self.driver.execute_script(
                """
                document.querySelectorAll('[role="dialog"][aria-modal="true"]').forEach(d => {
                  const form = d.querySelector('#legacy-recipient-name, #legacy-contents');
                  if (form) return;
                  const txt = (d.innerText || '').toLowerCase();
                  if (txt.includes('guardar') || txt.includes('confirmar') || txt.includes('crear'))
                    return;
                  d.style.pointerEvents = 'none';
                });
                """
            )
        except Exception:
            pass

    def _confirm_pending_dialog(self) -> bool:
        """Pulsa Aceptar/Confirmar en un modal que no es el de destinatario."""
        try:
            clicked = bool(
                self.driver.execute_script(
                    """
                    const dialogs = Array.from(document.querySelectorAll(
                      '[role="dialog"], [aria-modal="true"]'
                    )).filter(d => d.offsetParent || d.getClientRects().length);
                    const skip = (d) => !!d.querySelector('#legacy-recipient-name');
                    const prefer = /^(aceptar|confirmar|guardar|ok|entendido|sí|si|crear)$/i;
                    for (const d of dialogs) {
                      if (skip(d)) continue;
                      const buttons = Array.from(d.querySelectorAll('button')).filter(b => {
                        if (b.disabled) return false;
                        const t = (b.innerText || '').replace(/\\s+/g, ' ').trim();
                        return t && !/^cancelar$/i.test(t);
                      });
                      const hit = buttons.find(b => prefer.test((b.innerText || '').trim()))
                        || buttons.find(b => /aceptar|confirmar|guardar|crear/i.test(b.innerText || ''))
                        || buttons[buttons.length - 1];
                      if (hit) { hit.click(); return true; }
                    }
                    return false;
                    """
                )
            )
            if clicked:
                time.sleep(0.35)
            return clicked
        except Exception:
            return False

    def _validation_errors(self) -> list[str]:
        try:
            errs = self.driver.execute_script(
                """
                const nodes = document.querySelectorAll(
                  '.text-red-500, .text-red-600, .text-danger, [class*="error"], p, span, small'
                );
                const out = [];
                for (const n of nodes) {
                  const t = (n.innerText || '').replace(/\\s+/g, ' ').trim();
                  if (!t) continue;
                  if (/elige|obligator|requerid|inválid|invalido|falt/i.test(t) && t.length < 80)
                    out.push(t);
                }
                return [...new Set(out)];
                """
            )
            return [str(x) for x in (errs or []) if str(x).strip()]
        except Exception:
            return []

    def _contents_value(self) -> str | None:
        try:
            el = self.driver.find_element(By.ID, "legacy-contents")
            return el.get_attribute("value") or ""
        except StaleElementReferenceException:
            return None
        except Exception:
            return None

    def _save_succeeded(self, before: str) -> bool:
        if self._pagina_express_rota():
            return True
        now = self._contents_value()
        if now is None:
            return True
        if now == "":
            return True
        if before and now != before and len(now) < 3:
            return True
        try:
            body = (self.driver.execute_script(
                "return document.body ? document.body.innerText : ''"
            ) or "").lower()
            if any(
                k in body
                for k in (
                    "orden creada",
                    "creado con éxito",
                    "creada con éxito",
                    "guardada con éxito",
                    "éxito al crear",
                )
            ):
                return True
        except Exception:
            pass
        return False

    def _safe_click(self, el) -> None:
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el
            )
        except Exception:
            pass
        time.sleep(0.1)
        try:
            el.click()
            return
        except ElementClickInterceptedException:
            self._dismiss_overlays()
            time.sleep(0.2)
        except StaleElementReferenceException:
            raise
        self._click_js(el)

    def _click_xpath(self, xpath: str, timeout: float = 12) -> None:
        """Pulsa el botón visible. Ignora copias ocultas y espera a que se habilite."""
        end = time.time() + timeout
        saw_disabled = False
        while time.time() < end:
            try:
                for el in self.driver.find_elements(By.XPATH, xpath):
                    try:
                        if not el.is_displayed():
                            continue
                        if not el.is_enabled():
                            saw_disabled = True
                            continue
                        self._safe_click(el)
                        return
                    except StaleElementReferenceException:
                        continue
            except StaleElementReferenceException:
                pass
            time.sleep(0.25)
        if saw_disabled:
            raise TimeoutException(
                "El botón está visible pero Express lo tiene deshabilitado"
            )
        raise TimeoutException("No se encontró el botón para continuar")

    def _find_crear_agregar_otro(self):
        """Botón 'Crear y agregar otro' (azul si existe, si no cualquier coincidencia)."""
        return self.driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('button'));
            const hits = buttons.filter(b => {
              const text = (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim()
                .toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
              return text === 'crear y agregar otro' || text.includes('crear y agregar otro');
            });
            const blue = hits.filter(b => String(b.className || '').includes('4099de'));
            return blue.length ? blue : hits;
            """
        )

    def _click_crear_agregar_otro(self, timeout: float = 12) -> None:
        end = time.time() + timeout
        saw_disabled = False
        while time.time() < end:
            try:
                buttons = self._find_crear_agregar_otro() or []
            except StaleElementReferenceException:
                time.sleep(0.25)
                continue
            for el in buttons:
                try:
                    if not el.is_displayed():
                        continue
                    if not el.is_enabled():
                        saw_disabled = True
                        continue
                    self._click_js(el)
                    return
                except StaleElementReferenceException:
                    continue
            time.sleep(0.25)
        if saw_disabled:
            raise TimeoutException(
                "El botón Crear y agregar otro está visible pero Express lo tiene deshabilitado"
            )
        raise TimeoutException(
            "No se encontró el botón azul Crear y agregar otro"
        )

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

    def _find_select_by_label(self, label: str):
        """Select nativo junto a 'Departamento' / 'Municipio', aunque esté oculto."""
        try:
            return self.driver.execute_script(
                """
                const want = (arguments[0] || '').toLowerCase()
                  .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
                const isPlaceholder = (t) => {
                  const n = (t || '').toLowerCase();
                  return !n || n === '-' || n.includes('elegir') || n.includes('opción')
                    || n.includes('opcion') || n.includes('select');
                };
                const nearSelect = (node) => {
                  if (!node) return null;
                  const row = node.closest('tr, .form-group, .row, label, div');
                  const roots = [node, node.parentElement, row].filter(Boolean);
                  for (const r of roots) {
                    const sel = r.querySelector && r.querySelector('select');
                    if (sel) return sel;
                  }
                  return null;
                };
                const nodes = document.querySelectorAll('label, th, td, span, p, div');
                for (const n of nodes) {
                  const raw = (n.childNodes.length && n.childNodes[0].nodeType === 3)
                    ? n.childNodes[0].textContent
                    : n.textContent;
                  const t = (raw || '').replace('*','').trim().toLowerCase()
                    .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
                  if (t !== want && !t.startsWith(want + ' ')) continue;
                  const sel = nearSelect(n);
                  if (sel) return sel;
                }
                const all = Array.from(document.querySelectorAll('select'));
                if (want.includes('municipio')) {
                  const muni = all.find(s => (s.id || s.name || '').toLowerCase().includes('city')
                    || (s.id || s.name || '').toLowerCase().includes('municip'));
                  if (muni) return muni;
                  const withOpts = all.filter(s => Array.from(s.options).some(o => !isPlaceholder(o.text)));
                  return withOpts[1] || all[1] || null;
                }
                if (want.includes('departamento')) {
                  const dept = all.find(s => (s.id || s.name || '').toLowerCase().includes('state')
                    || (s.id || s.name || '').toLowerCase().includes('dept'));
                  if (dept) return dept;
                  return all[0] || null;
                }
                return null;
                """,
                label,
            )
        except Exception:
            return None

    def _select_option(self, select_el, labels) -> str:
        """Elige una opción (React-friendly). Espera a que el select tenga opciones reales."""
        wants = [str(x).strip() for x in (labels if isinstance(labels, (list, tuple)) else [labels]) if str(x).strip()]
        if not select_el or not wants:
            return ""
        try:
            chosen = self.driver.execute_script(
                """
                const sel = arguments[0];
                const wants = arguments[1] || [];
                const norm = (s) => (s || '').toLowerCase()
                  .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                  .replace(/\\s+/g, ' ').trim();
                const compact = (s) => norm(s).replace(/\\s+/g, '');
                const isPlaceholder = (t) => {
                  const n = norm(t);
                  return !n || n === '-' || n.includes('elegir') || n.includes('opcion')
                    || n.includes('select');
                };
                if (!sel || !sel.options) return '';
                const opts = Array.from(sel.options).filter(o => !isPlaceholder(o.text));
                if (!opts.length) return '';
                let exact = null, starts = null, partial = null;
                for (const want of wants) {
                  const target = compact(want);
                  const targetSp = norm(want);
                  if (!target) continue;
                  for (const opt of opts) {
                    const text = (opt.text || '').trim();
                    const val = (opt.value || '').trim();
                    const bt = compact(text), bv = compact(val);
                    const nt = norm(text);
                    if (bt === target || bv === target || nt === targetSp || text === want) {
                      exact = opt; break;
                    }
                    if (!starts && (bt.startsWith(target) || nt.startsWith(targetSp))) starts = opt;
                    if (!partial && (bt.includes(target) || nt.includes(targetSp))) partial = opt;
                  }
                  if (exact) break;
                }
                const opt = exact || starts || partial;
                if (!opt) return '';
                const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLSelectElement.prototype, 'value'
                );
                if (setter && setter.set) setter.set.call(sel, opt.value);
                else sel.value = opt.value;
                opt.selected = true;
                sel.dispatchEvent(new Event('input', {bubbles:true}));
                sel.dispatchEvent(new Event('change', {bubbles:true}));
                try { sel.dispatchEvent(new Event('blur', {bubbles:true})); } catch (e) {}
                return (opt.text || '').trim();
                """,
                select_el,
                wants,
            )
            return chosen or ""
        except StaleElementReferenceException:
            return ""

    def _click_dropdown_option(self, field_label: str, labels: list[str]) -> str:
        """Clic en combobox custom (si el select nativo no pinta la UI)."""
        wants = [str(x).strip() for x in labels if str(x).strip()]
        if not wants:
            return ""
        try:
            return (
                self.driver.execute_script(
                    """
                    const field = (arguments[0] || '').toLowerCase()
                      .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
                    const wants = arguments[1] || [];
                    const compact = (s) => (s || '').toLowerCase()
                      .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                      .replace(/\\s+/g, '');
                    const row = Array.from(document.querySelectorAll('tr, .form-group, .row, div'))
                      .find(n => {
                        const t = (n.innerText || '').split('\\n')[0].replace('*','').trim()
                          .toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
                        return t === field || t.startsWith(field);
                      });
                    if (!row) return '';
                    const ctrl = row.querySelector('select, button, [role="combobox"], [aria-haspopup="listbox"]');
                    if (!ctrl) return '';
                    ctrl.click();
                    return 'opened';
                    """,
                    field_label,
                    wants,
                )
                or ""
            )
        except Exception:
            return ""

    def _pick_open_option(self, labels: list[str]) -> str:
        wants = [str(x).strip() for x in labels if str(x).strip()]
        try:
            return (
                self.driver.execute_script(
                    """
                    const wants = arguments[0] || [];
                    const compact = (s) => (s || '').toLowerCase()
                      .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                      .replace(/\\s+/g, '');
                    const nodes = Array.from(document.querySelectorAll(
                      '[role="option"], [role="listbox"] li, ul[role="listbox"] li, li, div[class*="option"]'
                    )).filter(n => n.offsetParent);
                    for (const want of wants) {
                      const t = compact(want);
                      if (!t) continue;
                      const hit = nodes.find(n => compact(n.innerText) === t)
                        || nodes.find(n => compact(n.innerText).startsWith(t))
                        || nodes.find(n => compact(n.innerText).includes(t));
                      if (hit) { hit.click(); return (hit.innerText || '').trim(); }
                    }
                    return '';
                    """,
                    wants,
                )
                or ""
            )
        except Exception:
            return ""

    def _select_departamento(self, departamento: str) -> None:
        labels = [
            state_label_for_sistrack(departamento) or departamento,
            departamento,
            "La Union" if norm(departamento) == "la union" else "",
        ]
        end = time.time() + 10
        while time.time() < end:
            try:
                sel = self._find_select_by_label("Departamento")
                if sel:
                    chosen = self._select_option(sel, labels)
                    if chosen:
                        self._pace("after_select")
                        return
            except StaleElementReferenceException:
                pass
            time.sleep(0.25)

    def _select_municipio(self, preferred: str, departamento: str) -> str:
        labels = sistrack_municipio_candidates(preferred, departamento)
        end = time.time() + 14
        while time.time() < end:
            try:
                sel = self._find_select_by_label("Municipio")
                if sel:
                    chosen = self._select_option(sel, labels)
                    if chosen:
                        return chosen
                    opened = self._click_dropdown_option("Municipio", labels)
                    if opened:
                        time.sleep(0.25)
                        picked = self._pick_open_option(labels)
                        if picked:
                            return picked
            except StaleElementReferenceException:
                pass
            time.sleep(0.3)
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
            centered = bool(
                self.driver.execute_script(
                    """
                    const lat = Number(arguments[0]), lng = Number(arguments[1]);
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
                    if (!map || !window.L) return false;
                    const ll = L.latLng(lat, lng);
                    try { map.setView(ll, 17); } catch (e) { return false; }
                    return true;
                    """,
                    str(lat),
                    str(lng),
                )
            )
        except Exception:
            return False
        if not centered:
            return False
        try:
            map_el = self.driver.find_element(By.CSS_SELECTOR, ".leaflet-container")
            ActionChains(self.driver).move_to_element(map_el).click().perform()
            time.sleep(0.25)
        except Exception:
            return False
        return True

    def _search_map(self, query: str) -> None:
        q = (query or "").strip()
        if not q:
            return
        self.wait.until(
            EC.presence_of_element_located((By.ID, "legacy-recipient-search"))
        )
        self.driver.execute_script(
            """
            const el = document.getElementById('legacy-recipient-search');
            if (!el) return;
            const val = arguments[0] || '';
            el.focus();
            const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
            if (desc && desc.set) desc.set.call(el, val);
            else el.value = val;
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}));
            el.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', bubbles:true}));
            """,
            q,
        )
        time.sleep(1.1)
        try:
            items = self.driver.find_elements(
                By.CSS_SELECTOR,
                "[role='option'], .leaflet-control-geocoder-alternatives li, li[class*='result']",
            )
            for item in items:
                try:
                    if item.is_displayed():
                        self._click_js(item)
                        time.sleep(0.4)
                        break
                except StaleElementReferenceException:
                    continue
        except Exception:
            pass

    def _usar_coordenadas(self, text: str) -> bool:
        """Pega el texto tal cual en legacy-recipient-paste y pulsa su botón Usar."""
        value = (text or "").strip()
        if not value:
            return False
        try:
            self.wait.until(EC.presence_of_element_located((By.ID, "legacy-recipient-paste")))
        except TimeoutException:
            return False
        self._fill("legacy-recipient-paste", value)
        try:
            btn = self.driver.find_element(
                By.XPATH,
                "//input[@id='legacy-recipient-paste']/following::button[normalize-space()='Usar'][1]",
            )
            if btn.is_displayed() and btn.is_enabled():
                btn.click()
        except Exception:
            pass
        return True

    def _set_map_point(self, pedido: Pedido) -> None:
        self._quitar_punto()
        raw_geo = (getattr(pedido, "coordenadas", "") or "").strip()
        resolved = resolve_map_query(raw_geo)
        lat_s, lng_s = resolved.get("lat_s"), resolved.get("lng_s")
        paste = ""
        if raw_geo.lower().startswith("http"):
            paste = raw_geo
        elif lat_s and lng_s:
            paste = f"{lat_s}, {lng_s}"
        elif raw_geo:
            paste = raw_geo
        if paste and self._usar_coordenadas(paste):
            self._pace("after_map")
            return
        query = (resolved.get("query") or "").strip()
        if not query:
            ref = (pedido.referencia or "").strip()
            if ref and norm(ref) != "sin referencia":
                query = ref
            else:
                query = (pedido.direccion or "").strip()
        if query:
            self._search_map(query)

    def crear_destinatario(self, pedido: Pedido) -> None:
        self.click_crear_destinatario_modal()
        self._fill("legacy-recipient-name", pedido.nombre)
        self._fill("legacy-recipient-phone", format_phone_hyphen(pedido.telefono))
        self._fill("legacy-recipient-address", pedido.direccion)
        self._fill(
            "legacy-recipient-reference",
            pedido.referencia or "Sin referencia",
        )
        inferred_dept, inferred_muni = infer_location(
            pedido.direccion or "", pedido.referencia or ""
        )
        dept = pedido.departamento or inferred_dept
        muni = pedido.municipio or inferred_muni
        self._select_departamento(dept)
        city = self._select_municipio(muni, dept)
        if not city and inferred_muni and norm(inferred_muni) != norm(muni):
            city = self._select_municipio(inferred_muni, dept)
        if city:
            pedido.municipio = city
        self._set_map_point(pedido)
        self._dismiss_toasts()
        self._click_xpath("//button[contains(.,'Crear Destinatario')]")
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "legacy-contents"))
            )
        except TimeoutException:
            time.sleep(0.8)
        self._dismiss_overlays()
        end = time.time() + 8
        while time.time() < end:
            try:
                rec = self.driver.find_element(By.ID, "legacy-recipient-name")
                if rec.is_displayed():
                    time.sleep(0.25)
                    continue
            except Exception:
                break
            break
        self._pace("after_modal")

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
        self._dismiss_overlays(allow_escape=False)
        try:
            self.driver.execute_script(
                """
                document.querySelectorAll('input, textarea, select').forEach(el => {
                  if (!el.offsetParent) return;
                  el.dispatchEvent(new Event('input', {bubbles:true}));
                  el.dispatchEvent(new Event('change', {bubbles:true}));
                  el.dispatchEvent(new Event('blur', {bubbles:true}));
                });
                """
            )
        except Exception:
            pass
        errs = self._validation_errors()
        if any("municipio" in e.lower() for e in errs):
            raise RuntimeError(
                "Express pide municipio antes de guardar: " + "; ".join(errs[:3])
            )
        saved = False
        wait_s = float(self._speed["save_wait"])
        last_errs: list[str] = []
        for _attempt in range(4):
            before = self._contents_value() or ""
            self._click_crear_agregar_otro(timeout=float(self._speed["click_timeout"]))
            time.sleep(0.35)
            self._confirm_pending_dialog()
            end = time.time() + wait_s
            empty_hits = 0
            while time.time() < end:
                if self._pagina_express_rota():
                    print(
                        "  AVISO: Express se cayó al abrir el siguiente pedido. "
                        "Recargando el formulario."
                    )
                    self.go_crear_orden()
                    saved = True
                    break
                try:
                    alert = self.driver.switch_to.alert
                    alert.accept()
                except Exception:
                    pass
                self._confirm_pending_dialog()
                if self._save_succeeded(before):
                    now = self._contents_value()
                    if now == "":
                        empty_hits += 1
                        if empty_hits >= 2:
                            saved = True
                            break
                    else:
                        saved = True
                        break
                last_errs = self._validation_errors()
                if last_errs and any("municipio" in e.lower() for e in last_errs):
                    raise RuntimeError(
                        "Express pide municipio antes de guardar: " + "; ".join(last_errs[:3])
                    )
                time.sleep(0.4)
            if saved:
                break
            self._dismiss_toasts()
        if not saved:
            extra = ("; ".join(last_errs[:3]) + ". ") if last_errs else ""
            raise RuntimeError(
                extra
                + "El botón Crear y agregar otro no guardó la orden. "
                "La lista se detiene aquí para no saltarse el orden."
            )
        self._dismiss_toasts()
        self._pace("settle")

    def procesar_pedido(self, pedido: Pedido, index: int, total: int) -> None:
        print(
            f"[{index}/{total}] Fila {pedido.fila}: {pedido.nombre} | "
            f"{pedido.departamento}/{pedido.municipio or '?'} | ${pedido.precio}"
            + (f" | geo={pedido.coordenadas}" if getattr(pedido, "coordenadas", "") else "")
        )
        if self._pagina_express_rota() or "shipments/new" not in (self.driver.current_url or ""):
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
