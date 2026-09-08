# -*- coding: utf-8 -*-
"""
Carga de guías en Forza Delivery (portal corporativo).

Versión optimizada: esperas adaptativas, menos sondeos DOM, diagnóstico por pedido
y configuración de rendimiento mediante FORZA_PAUSE_SCALE.

Basado en Selenium IDE: forza.side
  https://portal.forzadelivery.com/login-corporate

Flujo del .side:
  1. País El Salvador
  2. Código / Usuario / Contraseña → Iniciar sesión
  3. Menú → Crear Guías
  4. Poblado (modal searchbar → #lbl0 / mejor match)
  5. Toggle Frágil (ion-color-success)
  6. Descripción = producto
  7. CALCULAR → SELECCIONAR servicio
  8. Detalles: quién recibe, nombre, teléfono, dirección, indicaciones, referencia
  9. SIGUIENTE → monto COD → confirmar → Crear Guías

No rellena peso ni dimensiones (defaults de Forza).
Credenciales: Ajustes web / env FORZA_* (nunca hardcodeadas).
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from typing import Any

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from sistrack.cargar_pedidos_sistrack import Pedido, build_observations, clean_phone
from forza.ubicaciones_forza import (
    ForzaUbicacion,
    best_forza_match,
    forza_location_from_pedido,
    score_forza_label,
)

BASE_URL = "https://portal.forzadelivery.com"
LOGIN_URL = f"{BASE_URL}/login-corporate"
COTIZADOR_URL = f"{BASE_URL}/tabs/cotizador-corporativo"

FORZA_CODIGO = os.getenv("FORZA_CODIGO", "").strip()
FORZA_USUARIO = os.getenv("FORZA_USUARIO", "").strip()
FORZA_PASSWORD = os.getenv("FORZA_PASSWORD", "")

# Rendimiento / estabilidad. Un valor de 0.25 reduce esperas fijas sin eliminar
# los WebDriverWait que realmente sincronizan la UI. Subir a 0.40–0.60 si
# el portal está lento o la conexión tiene mucha latencia.
try:
    FORZA_PAUSE_SCALE = max(0.10, min(1.0, float(os.getenv("FORZA_PAUSE_SCALE", "0.25"))))
except (TypeError, ValueError):
    FORZA_PAUSE_SCALE = 0.25

try:
    FORZA_WAIT_TIMEOUT = max(10.0, float(os.getenv("FORZA_WAIT_TIMEOUT", "25")))
except (TypeError, ValueError):
    FORZA_WAIT_TIMEOUT = 25.0


def _log(msg: str) -> None:
    """Print seguro en consolas Windows (cp1252) sin tumbar el bot."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _fold_key(s: str) -> str:
    """Quita puntuación: 'C.O.D.' → 'cod', 'Estándar' → 'estandar'."""
    return re.sub(r"[^a-z0-9]+", "", _fold(s))


def _pedido_es_cod(pedido: Pedido) -> bool:
    """COD por defecto. Solo Estándar si pagado=Sí (campo del listado)."""
    pagado = _fold(str(getattr(pedido, "pagado", "") or ""))
    if pagado in ("si", "yes", "true", "1", "pagado"):
        return False
    # Solo "PAGADO" explícito en notas (evita falsos positivos)
    notas = _fold(getattr(pedido, "notas", "") or "")
    if re.search(r"(?:^|[\s|])pagado(?:[\s|]|$)", notas):
        return False
    return True


class ForzaBot:
    def __init__(self, headless: bool = False, dry_run: bool = False) -> None:
        self.headless = headless
        self.dry_run = dry_run
        self.driver = self._build_driver()
        self.wait = WebDriverWait(self.driver, FORZA_WAIT_TIMEOUT, poll_frequency=0.15)
        self._last_step = "inicio"
        # Evita re-pulsar Mostrar Resumen (crea guías duplicadas)
        self._resumen_confirmado = False

    def _pause(self, seconds: float, *, minimum: float = 0.03) -> None:
        """Pausa corta y configurable para animaciones/transiciones.

        Las transiciones importantes deben sincronizarse con WebDriverWait; esta
        función solo cubre animaciones de Ionic y evita sleeps largos innecesarios.
        """
        if seconds <= 0:
            return
        delay = max(minimum, min(0.35, seconds * FORZA_PAUSE_SCALE))
        time.sleep(delay)

    def _wait_document_ready(self, timeout: float = 8.0) -> None:
        """Espera a que Chrome termine de cargar el documento sin bloquear de más."""
        try:
            WebDriverWait(self.driver, timeout, poll_frequency=0.1).until(
                lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
            )
        except Exception:
            pass

    def _wait_page_marker(self, markers: tuple[str, ...], timeout: float = 8.0) -> bool:
        """Espera texto visible en la página; útil para transiciones SPA de Ionic."""
        folded = tuple(_fold(x) for x in markers if x)
        if not folded:
            return True
        try:
            return bool(WebDriverWait(self.driver, timeout, poll_frequency=0.15).until(
                lambda d: any(
                    marker in _fold(d.execute_script(
                        "return document.body ? document.body.innerText : ;"
                    ) or "")
                    for marker in folded
                )
            ))
        except Exception:
            return False

    def _debug_failure(self, prefix: str) -> None:
        """Guarda screenshot/HTML cuando un paso falla, sin ocultar la excepción original."""
        try:
            self.driver.save_screenshot(f"debug_forza_{prefix}.png")
        except Exception:
            pass
        try:
            with open(f"debug_forza_{prefix}.html", "w", encoding="utf-8") as fh:
                fh.write(self.driver.page_source)
        except Exception:
            pass

    def _build_driver(self) -> webdriver.Chrome:
        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1156,933")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--lang=es-SV")
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=opts)

    def close(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Login + navegación
    # ------------------------------------------------------------------

    def login(
        self,
        codigo: str | None = None,
        usuario: str | None = None,
        password: str | None = None,
    ) -> None:
        codigo = (codigo or FORZA_CODIGO or "").strip()
        usuario = (usuario or FORZA_USUARIO or "").strip()
        password = password if password is not None else FORZA_PASSWORD
        if not codigo or not usuario or not password:
            raise RuntimeError(
                "Faltan credenciales Forza (código, usuario, contraseña)."
            )

        _log("Forza: login corporativo...")
        self.driver.get(LOGIN_URL)
        self._wait_document_ready(8)

        # País: El Salvador (obligatorio antes del formulario)
        self._select_el_salvador()
        self._pause(0.8)

        inputs = self.wait.until(
            EC.presence_of_all_elements_located(
                (By.CSS_SELECTOR, "form input.native-input, form ion-input input")
            )
        )
        visibles = [el for el in inputs if el.is_displayed()]
        if len(visibles) < 3:
            visibles = self.driver.find_elements(
                By.CSS_SELECTOR, "input.native-input, ion-input input"
            )
            visibles = [el for el in visibles if el.is_displayed()]
        if len(visibles) < 3:
            raise RuntimeError(
                "No se encontraron los 3 campos de login Forza "
                "(¿quedó sin seleccionar El Salvador?)"
            )

        self._click_then_type(visibles[0], codigo)
        self._click_then_type(visibles[1], usuario)
        self._click_then_type(visibles[2], password)

        if not self._click_by_text(
            ["Iniciar sesión", "Iniciar sesion", "INGRESAR", "Entrar"],
            tags=("ion-button", "button"),
        ):
            # Fallback .side: css=.ion-color en el form de login
            btn = self.driver.find_elements(
                By.CSS_SELECTOR,
                "app-login-corporate ion-button, form ion-button, .ion-color",
            )
            clicked = False
            for b in btn:
                try:
                    if b.is_displayed():
                        self._js_click(b)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                raise RuntimeError("No se encontró el botón Iniciar sesión")

        self.wait.until(
            EC.any_of(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//*[contains(.,'Crear Guías') or contains(.,'Crear Guias') "
                        "or contains(.,'Mis Envíos')]",
                    )
                ),
                EC.presence_of_element_located((By.CSS_SELECTOR, "ion-menu, app-tabs")),
            )
        )
        self._wait_page_marker(("Crear Guías", "Crear Guias", "Mis Envíos", "Mis Envios"), timeout=5)
        _log("  Login OK")

    def _select_el_salvador(self) -> None:
        """
        Pantalla app-select-country-page (forza.side):
          css=.d-flex:nth-child(7)
          xpath=//.../app-select-country-page/div/div/div[6]
        """
        _log("  País: El Salvador")

        # Si ya está el form de login, el país ya fue elegido
        if self._login_form_ready():
            _log("     (formulario ya visible, país ok)")
            return

        try:
            self.wait.until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "app-select-country-page, app-login-corporate",
                    )
                )
            )
        except Exception:
            pass
        self._pause(0.6)

        clicked = False

        # 1) Exacto del .side
        for xp in (
            "//ion-router-outlet[@id='main-content']/app-login-corporate"
            "/div/app-select-country-page/div/div/div[6]",
            "//app-select-country-page/div/div/div[6]",
            "//app-select-country-page//div[contains(@class,'d-flex')][6]",
            "//app-select-country-page//*[contains(normalize-space(.),'El Salvador') "
            "and not(ancestor::*[contains(normalize-space(.),'El Salvador')][position()>1])]",
        ):
            if self._click_xpath(xp):
                clicked = True
                break

        # 2) CSS del .side dentro del selector de país
        if not clicked:
            for css in (
                "app-select-country-page .d-flex:nth-child(7)",
                "app-select-country-page .d-flex:nth-child(6)",
                "app-select-country-page div.d-flex:nth-of-type(6)",
                "app-login-corporate .d-flex:nth-child(7)",
            ):
                for el in self.driver.find_elements(By.CSS_SELECTOR, css):
                    try:
                        if not el.is_displayed():
                            continue
                        self._js_click(el)
                        clicked = True
                        break
                    except Exception:
                        continue
                if clicked:
                    break

        # 3) Cualquier tarjeta/celda cuyo texto sea (casi) solo El Salvador
        if not clicked:
            candidates = self.driver.find_elements(
                By.CSS_SELECTOR,
                "app-select-country-page .d-flex, app-select-country-page div, "
                "app-select-country-page img, app-select-country-page ion-item, "
                "app-login-corporate .d-flex",
            )
            for el in candidates:
                try:
                    if not el.is_displayed():
                        continue
                    txt = _fold(el.text or "")
                    # Preferir nodos con texto corto que mencionen el país
                    if "el salvador" in txt and len(txt) < 40:
                        self._js_click(el)
                        clicked = True
                        break
                    # A veces el texto está en un hijo y el click útil es el padre .d-flex
                    alt = _fold(el.get_attribute("innerText") or "")
                    if alt == "el salvador" or alt.startswith("el salvador"):
                        self._js_click(el)
                        clicked = True
                        break
                except Exception:
                    continue

        # 4) Click en imagen/bandera con alt/title El Salvador
        if not clicked:
            for el in self.driver.find_elements(
                By.CSS_SELECTOR, "app-select-country-page img, app-login-corporate img"
            ):
                try:
                    meta = _fold(
                        " ".join(
                            [
                                el.get_attribute("alt") or "",
                                el.get_attribute("title") or "",
                                el.get_attribute("src") or "",
                            ]
                        )
                    )
                    if "salvador" in meta:
                        self._js_click(el)
                        clicked = True
                        break
                except Exception:
                    continue

        if not clicked:
            # Guardar ayuda visual
            try:
                self.driver.save_screenshot("debug_forza_pais.png")
            except Exception:
                pass
            raise RuntimeError(
                "No se pudo seleccionar el país El Salvador en el login Forza"
            )

        # Esperar a que aparezca el formulario de código/usuario/clave
        try:
            self.wait.until(lambda d: self._login_form_ready())
        except Exception as exc:
            try:
                self.driver.save_screenshot("debug_forza_pais.png")
            except Exception:
                pass
            raise RuntimeError(
                "Se hizo click en el país pero no apareció el formulario de login"
            ) from exc
        _log("     El Salvador seleccionado")

    def _login_form_ready(self) -> bool:
        """True si ya hay al menos 3 inputs visibles de login (país ya elegido)."""
        try:
            inputs = self.driver.find_elements(
                By.CSS_SELECTOR,
                "app-login-corporate form input.native-input, "
                "app-login-corporate form ion-input input, "
                "form input.native-input, form ion-input input",
            )
            visibles = [el for el in inputs if el.is_displayed()]
            return len(visibles) >= 3
        except Exception:
            return False

    def go_crear_guias(self, *, force_new: bool = False) -> None:
        """Ir a Crear Guías/cotizador usando la ruta más corta disponible."""
        self._dismiss_overlays()
        if not force_new and self._cotizador_listo():
            _log("  Cotizador ya abierto - sin volver a Crear Guias")
            self._ensure_tab_envio()
            return

        _log("  Ir a Crear Guías" + (" (nuevo formulario)" if force_new else ""))

        clicked = self._click_xpath(
            "//ion-menu//ion-item[contains(.,'Crear Guías') or contains(.,'Crear Guias')]"
            " | //ion-item[contains(.,'Crear Guías') or contains(.,'Crear Guias')]"
        )
        if not clicked:
            clicked = self._click_by_text(
                ["SOLICITAR SERVICIO", "Solicitar servicio"],
                tags=("ion-button", "button", "a"),
            )
        if not clicked:
            self._open_menu_if_needed()
            clicked = self._click_xpath(
                "//ion-item[contains(.,'Crear Guías') or contains(.,'Crear Guias')]"
            )
        if not clicked:
            self.driver.get(COTIZADOR_URL)
            self._wait_document_ready(8)

        self._ensure_tab_envio()
        try:
            self.wait.until(lambda _d: self._cotizador_listo())
        except Exception:
            self.wait.until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//*[contains(.,'Poblado') or contains(.,'Destino') "
                        "or contains(.,'Paquete') or contains(.,'Descripción')]",
                    )
                )
            )

    def _cotizador_listo(self) -> bool:
        """True si el formulario de cotizar/crear guía ya está visible."""
        try:
            roots = self.driver.find_elements(
                By.CSS_SELECTOR, "app-cotizador-corporativo"
            )
            if not any(r.is_displayed() for r in roots):
                return False
            markers = self.driver.find_elements(
                By.XPATH,
                "//app-cotizador-corporativo"
                "//*[contains(.,'Poblado') or contains(.,'Destino') "
                "or contains(.,'Descripción') or contains(.,'Descripcion') "
                "or contains(.,'CALCULAR') or contains(.,'Paquete')]",
            )
            return any(m.is_displayed() for m in markers)
        except Exception:
            return False

    def _ensure_tab_envio(self) -> None:
        """Activa pestaña Envío solo si hace falta (no re-clicar si ya está)."""
        if self._cotizador_listo():
            return
        # Evitar tags='*' (hace matches enormes y puede reiniciar la vista)
        self._click_by_text(
            ["Envío", "ENVIO", "Envio"],
            tags=("ion-segment-button", "ion-tab-button", "button", "a", "ion-label"),
            exact=False,
        )
        self._pause(0.4)

    def _open_menu_if_needed(self) -> None:
        # Si el ítem ya es clickeable, no abrir menú otra vez
        for el in self.driver.find_elements(
            By.XPATH, "//ion-item[contains(.,'Crear Guías') or contains(.,'Crear Guias')]"
        ):
            try:
                if el.is_displayed():
                    return
            except Exception:
                continue
        for sel in (
            "ion-menu-button",
            "ion-buttons ion-menu-button",
            "button[aria-label*='menu' i]",
        ):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        self._js_click(el)
                        self._pause(0.4)
                        return
                except Exception:
                    continue

    # ------------------------------------------------------------------
    # Pedido completo
    # ------------------------------------------------------------------

    def procesar_pedido(self, pedido: Pedido, index: int, total: int) -> None:
        """Procesa un pedido completo con navegación mínima y diagnóstico de fallos."""
        _log(
            f"[Forza {index}/{total}] Fila {pedido.fila}: {pedido.nombre} | "
            f"{pedido.departamento}/{pedido.municipio or '?'} | ${pedido.precio}"
        )
        self._resumen_confirmado = False
        try:
            self._last_step = "navegación"
            self.go_crear_guias(force_new=False)

            use_cod = _pedido_es_cod(pedido)
            _log(f"  Servicio: {'C.O.D.' if use_cod else 'Estándar'}")

            self._last_step = "destino"
            self._step_destino_manual()
            self._last_step = "poblado"
            self._step_poblado(pedido)
            self._last_step = "caja"
            self._step_caja()
            self._last_step = "fragil"
            self._step_fragil()
            self._last_step = "descripcion"
            self._step_descripcion(pedido)
            self._dismiss_overlays()
            self._last_step = "calcular"
            self._step_calcular(use_cod=use_cod)
            self._last_step = "servicio"
            self._step_seleccionar_servicio(use_cod)
            self._last_step = "detalles"
            self._step_detalles(pedido, numero_pedido=index)
            self._last_step = "siguiente_detalles"
            self._step_siguiente_detalles()
            self._last_step = "cod"
            self._step_cod_monto(pedido, use_cod)

            if self.dry_run:
                _log("  Dry-run: formulario listo, no se confirma el pago.")
                self.go_crear_guias(force_new=True)
                return

            self._last_step = "siguiente_servicios"
            self._step_siguiente_servicios()
            self._last_step = "resumen"
            self._step_resumen_pago()
            self._last_step = "facturacion"
            self._step_facturacion()
            self.go_crear_guias(force_new=True)
            _log(f"  OK Forza -> {(pedido.producto or '')[:60]}")
        except Exception as exc:
            self._debug_failure(f"pedido_{index}_{self._last_step}")
            raise RuntimeError(
                f"Error procesando pedido {index} (paso={self._last_step}): {exc}"
            ) from exc

    def _step_destino_manual(self) -> None:
        """En la UI real hay que marcar Destino = Manual antes del poblado."""
        _log("  0) Destino = Manual")
        if self._click_radio_or_label("Manual"):
            self._pause(0.35)
            return
        # Fallback: ion-radio cerca de "Manual"
        radios = self.driver.find_elements(
            By.XPATH,
            "//*[contains(.,'Manual')]/ancestor::*[.//ion-radio][1]//ion-radio"
            " | //ion-radio[contains(.,'Manual')]",
        )
        for r in radios:
            try:
                if r.is_displayed():
                    self._js_click(r)
                    self._pause(0.35)
                    return
            except Exception:
                continue
        _log("     aviso: no se encontró radio Manual (se continúa)")

    def _step_caja(self) -> None:
        _log("  1b) Tipo paquete = Caja")
        if not self._click_radio_or_label("Caja"):
            self._click_xpath(
                "//*[contains(.,'Caja')]/ancestor::*[.//ion-radio or .//img][1]"
            )
        self._pause(0.25)

    def _step_poblado(self, pedido: Pedido) -> None:
        ubic = forza_location_from_pedido(
            direccion=str(pedido.direccion or ""),
            referencia=str(pedido.referencia or ""),
            departamento=str(pedido.departamento or ""),
            municipio=str(pedido.municipio or ""),
            colonia=str(getattr(pedido, "colonia", "") or ""),
        )
        _log(f"  1) Poblado: {ubic.label}")
        self._abrir_selector_poblado()
        self._buscar_y_elegir_poblado(ubic)
        self._pause(0.6)
        selected = self._selected_poblado_text()
        if selected:
            _log(f"     poblado en formulario: {selected[:80]}")
        else:
            _log("     aviso: no se leyó el valor del poblado tras elegir")

    def _selected_poblado_text(self) -> str:
        for sel in (
            "app-cotizador-corporativo app-select ion-input input",
            "app-cotizador-corporativo app-select input",
            "app-select ion-input input",
            "app-select input",
        ):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if not el.is_displayed():
                        continue
                    val = (
                        el.get_attribute("value")
                        or el.get_attribute("innerText")
                        or el.text
                        or ""
                    ).strip()
                    if val and "selecciona" not in _fold(val):
                        return val
                except Exception:
                    continue
        return ""

    def _abrir_selector_poblado(self) -> None:
        # .side: click name=ion-input-7 (app-select del poblado)
        for sel in (
            "app-cotizador-corporativo app-select ion-input input",
            "app-cotizador-corporativo app-select ion-input",
            "app-cotizador-corporativo app-select ion-icon",
            "app-cotizador-corporativo app-select",
            "app-select ion-icon[name='caret-down-outline']",
            "app-select ion-input input",
        ):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        self._js_click(el)
                        self._pause(0.5)
                        if self.driver.find_elements(
                            By.CSS_SELECTOR,
                            "ion-modal input.searchbar-input, input.searchbar-input, "
                            "ion-searchbar input, input[type='search']",
                        ):
                            return
                except Exception:
                    continue
        if self._click_xpath(
            "//*[contains(.,'Selecciona un poblado') or contains(.,'Poblado, municipio')]"
        ):
            self._pause(0.5)
            if self.driver.find_elements(
                By.CSS_SELECTOR, "ion-modal, input.searchbar-input, input[type='search']"
            ):
                return
        raise RuntimeError("No se pudo abrir el selector de poblado")

    def _buscar_y_elegir_poblado(self, ubic: ForzaUbicacion) -> None:
        # .side: css=.searchbar-input → type → #lbl0 h2
        search = self.wait.until(
            EC.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    "ion-modal input.searchbar-input, ion-searchbar input, "
                    "input.searchbar-input, input[type='search']",
                )
            )
        )
        item = None
        last = ""
        chosen = ""
        for query in ubic.search_queries():
            last = query
            self._click_then_type(search, query)
            self._pause(1.0)
            item = self._pick_location_item(ubic)
            if item is not None:
                chosen = (item.text or "").strip()
                break
        if item is None:
            # Último recurso: primer resultado #lbl0 si coincide municipio/depto
            lbl0 = self.driver.find_elements(By.CSS_SELECTOR, "#lbl0 h2, ion-item#lbl0, #lbl0")
            if lbl0:
                txt = (lbl0[0].text or "").strip()
                if score_forza_label(txt, ubic) >= 6:
                    item = lbl0[0]
                    chosen = txt
        if item is None:
            try:
                self.driver.save_screenshot("debug_forza_poblado.png")
            except Exception:
                pass
            raise RuntimeError(
                f"No hay coincidencia de poblado Forza para '{ubic.label}' "
                f"(última búsqueda: '{last}')"
            )
        try:
            h2 = item.find_elements(By.CSS_SELECTOR, "h2, ion-label")
            self._js_click(h2[0] if h2 else item)
        except Exception:
            self._js_click(item)
        self._pause(0.8)
        # Cerrar modal si quedó abierto
        for sel in ("ion-modal", ".modal-wrapper"):
            if self.driver.find_elements(By.CSS_SELECTOR, sel):
                self._pause(0.3)
        score = score_forza_label(chosen, ubic)
        _log(f"     elegido: {chosen[:80]} (score={score})")
        if score < 6:
            raise RuntimeError(
                f"Poblado dudoso/incorrecto: '{chosen}' (esperado ~ '{ubic.label}')"
            )

    def _pick_location_item(self, ubic: ForzaUbicacion) -> Any:
        items = self.driver.find_elements(
            By.CSS_SELECTOR, "ion-modal ion-item, ion-modal ion-list ion-item, #lbl0"
        )
        visible = [el for el in items if el.is_displayed() and (el.text or "").strip()]
        if not visible:
            return None
        labels = [(el.text or "").strip() for el in visible]
        best = best_forza_match(labels, ubic)
        if not best:
            return None
        for el in visible:
            txt = (el.text or "").strip()
            if txt == best or best in txt:
                return el
        ranked = sorted(
            visible,
            key=lambda el: score_forza_label(el.text or "", ubic),
            reverse=True,
        )
        if score_forza_label(ranked[0].text or "", ubic) >= 6:
            return ranked[0]
        return None

    def _step_fragil(self) -> None:
        # .side: click css=.ion-color-success (toggle frágil)
        _log("  2) Frágil = sí")
        near = self.driver.find_elements(
            By.XPATH,
            "//*[contains(.,'Frágil') or contains(.,'Fragil')]"
            "/ancestor::*[.//ion-toggle][1]//ion-toggle",
        )
        candidates = near or self.driver.find_elements(
            By.CSS_SELECTOR,
            "app-cotizador-corporativo ion-toggle.ion-color-success, "
            "app-cotizador-corporativo ion-toggle, ion-toggle.ion-color-success",
        )
        for tg in candidates:
            try:
                if not tg.is_displayed():
                    continue
                checked = (tg.get_attribute("aria-checked") or "").lower()
                is_on = checked in ("true",) or "toggle-checked" in (
                    tg.get_attribute("class") or ""
                )
                if not is_on:
                    self._js_click(tg)
                self._pause(0.25)
                return
            except Exception:
                continue
        self._set_toggle_near(["¿Es Frágil?", "Es Frágil?", "Frágil", "Fragil"], on=True)
        self._pause(0.25)

    def _step_descripcion(self, pedido: Pedido) -> None:
        # .side: ion-textarea-0 = producto
        producto = (pedido.producto or "Producto").strip()
        _log(f"  3) Descripción: {producto[:60]}")
        filled = self._fill_by_label(
            [
                "Descripción general del envío",
                "Descripcion general del envio",
                "Descripción",
                "Descripcion",
            ],
            producto,
            required=False,
            scope="app-cotizador-corporativo",
        )
        if not filled:
            for sel in (
                "app-cotizador-corporativo textarea.native-textarea",
                "app-cotizador-corporativo textarea",
                "textarea[name^='ion-textarea']",
                "textarea",
            ):
                areas = [
                    el
                    for el in self.driver.find_elements(By.CSS_SELECTOR, sel)
                    if el.is_displayed()
                ]
                if areas:
                    self._set_input_value(areas[0], producto)
                    return

    def _step_calcular(self, use_cod: bool = True) -> None:
        # .side: ion-button CALCULAR (texto a menudo en shadow → XPath/JS)
        _log("  4) CALCULAR")
        self._pause(0.5)
        try:
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
        except Exception:
            pass
        self._pause(0.3)

        btn = self._find_calcular_button()
        if btn is None:
            self._pause(1.0)
            btn = self._find_calcular_button()
        if btn is None:
            try:
                self.driver.save_screenshot("debug_forza_calcular.png")
            except Exception:
                pass
            sel = self._selected_poblado_text()
            raise RuntimeError(
                "No se encontró el botón CALCULAR (revisa poblado / Frágil / descripción)"
                + (f" | poblado='{sel}'" if sel else "")
            )

        for _ in range(8):
            cls = btn.get_attribute("class") or ""
            aria = (btn.get_attribute("aria-disabled") or "").lower()
            disabled_attr = btn.get_attribute("disabled")
            disabled = (
                aria == "true"
                or "button-disabled" in cls
                or "ion-disabled" in cls
                or disabled_attr is not None
            )
            if not disabled:
                break
            self._pause(0.4)
            btn = self._find_calcular_button() or btn

        # Un solo clic (evitar disparar CALCULAR varias veces)
        self._click_ion_button(btn)

        # Esperar a que carguen las tarjetas de servicio antes de elegir COD
        _log("     esperando tarifas / Seleccionar…")
        self._wait_servicios_listos(use_cod=use_cod, timeout=20)

    def _wait_servicios_listos(self, use_cod: bool, timeout: float = 20) -> None:
        """Tras CALCULAR: espera a que exista el servicio correcto y su botón Seleccionar."""
        last_reason = ""

        def ready(d: Any) -> bool:
            nonlocal last_reason
            try:
                result = d.execute_script(
                    """
                    const wantCod = !!arguments[0];
                    const fold = (s) => (s||'').toLowerCase()
                      .normalize('NFD').replace(/[\u0300-\u036f]/g,'');
                    const body = fold(document.body ? document.body.innerText : '');
                    const buttons = Array.from(
                      document.querySelectorAll('app-cotizador-corporativo ion-button, ion-button')
                    ).filter(b => {
                      const t = fold(b.textContent || b.innerText || '');
                      return t.includes('seleccionar') || !!b.querySelector("ion-icon[name='send']");
                    });
                    const hasCod = body.includes('servicio c.o.d') || body.includes('servicio cod') || /c\.o\.d/.test(body);
                    const hasStd = body.includes('servicio estandar') || body.includes('servicio estándar');
                    const ok = wantCod ? (hasCod && buttons.length >= 1) : (hasStd && buttons.length >= 1);
                    return {ok, reason: ok ? 'servicio correcto visible' : (hasCod || hasStd ? 'botones/titulo incompletos' : 'esperando tarifas'), n: buttons.length};
                    """,
                    use_cod,
                ) or {}
                last_reason = str(result.get("reason") or "")
                return bool(result.get("ok"))
            except Exception as exc:
                last_reason = str(exc)
                return False

        try:
            WebDriverWait(self.driver, timeout, poll_frequency=0.15).until(ready)
            _log("     servicios listos")
            return
        except Exception:
            self._debug_failure("calcular")
            raise RuntimeError(
                "Se pulsó CALCULAR pero no aparecieron servicios listos para "
                f"{'C.O.D.' if use_cod else 'Estándar'}"
                + (f" ({last_reason})" if last_reason else "")
            )

    def _find_calcular_button(self) -> Any:
        """Localiza CALCULAR aunque el texto viva en shadow DOM."""
        for xp in (
            "//app-cotizador-corporativo//ion-button[contains(.,'CALCULAR')]",
            "//app-cotizador-corporativo//ion-button[contains(.,'Calcular')]",
            "//ion-button[contains(.,'CALCULAR') or contains(.,'Calcular')]",
            "//button[contains(.,'CALCULAR') or contains(.,'Calcular')]",
            "//*[self::ion-button or self::button]"
            "[contains(translate(normalize-space(.),'calcular','CALCULAR'),'CALCULAR')]",
        ):
            for el in self.driver.find_elements(By.XPATH, xp):
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue

        for el in self.driver.find_elements(
            By.CSS_SELECTOR, "app-cotizador-corporativo ion-button, ion-button"
        ):
            try:
                if not el.is_displayed():
                    continue
                txt = _fold(self._element_label(el))
                if "calcular" in txt:
                    return el
            except Exception:
                continue

        try:
            el = self.driver.execute_script(
                """
                const nodes = Array.from(document.querySelectorAll('ion-button, button'));
                for (const n of nodes) {
                  const t = ((n.innerText || n.textContent || '') + ' ' +
                    (n.shadowRoot ? (n.shadowRoot.textContent || '') : '')).toLowerCase();
                  if (t.includes('calcular') && n.offsetParent !== null) return n;
                }
                return null;
                """
            )
            if el is not None:
                return el
        except Exception:
            pass
        return None

    def _element_label(self, el: Any) -> str:
        parts = [
            el.text or "",
            el.get_attribute("innerText") or "",
            el.get_attribute("textContent") or "",
            el.get_attribute("aria-label") or "",
        ]
        try:
            shadow = self.driver.execute_script(
                "return arguments[0].shadowRoot ? arguments[0].shadowRoot.textContent : '';",
                el,
            )
            parts.append(shadow or "")
        except Exception:
            pass
        return " ".join(parts)

    def _step_seleccionar_servicio(self, use_cod: bool) -> None:
        """Tras CALCULAR: SELECCIONAR en tarjeta Servicio C.O.D. o Estándar."""
        label = "C.O.D" if use_cod else "Estándar"
        _log(f"  5) Seleccionar servicio {label}")
        self._pause(0.8)

        btn = self._find_seleccionar_by_titulo(use_cod)
        if btn is None:
            btn = self._find_seleccionar_servicio_js(use_cod)
        if btn is None and use_cod:
            btn = self._find_seleccionar_col_side(col_index=2)
        if btn is None and not use_cod:
            btn = self._find_seleccionar_col_side(col_index=1)

        if btn is None:
            raise RuntimeError(f"No se encontró SELECCIONAR para {label}")

        local = _fold_key(self._tarjeta_local_text(btn))
        _log(f"     tarjeta local: {local[:100]!r}")
        if use_cod and "cod" not in local and "contraentrega" not in local:
            btn2 = self._find_seleccionar_by_titulo(True)
            if btn2 is None:
                raise RuntimeError(
                    f"SELECCIONAR no es C.O.D. (tarjeta={local[:100]!r})"
                )
            btn = btn2
            local = _fold_key(self._tarjeta_local_text(btn))
            if "cod" not in local:
                raise RuntimeError(
                    f"SELECCIONAR sigue sin ser C.O.D. (tarjeta={local[:100]!r})"
                )

        _log(f"     click SELECCIONAR → {label} (1 vez)")
        self._click_ion_button(btn)
        self._pause(1.5)
        try:
            self.wait.until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "app-envio-corporativo, "
                        "ion-radio-group[formcontrolname='opcionDeEntrega']",
                    )
                )
            )
        except Exception:
            if self.driver.find_elements(By.CSS_SELECTOR, "app-envio-corporativo"):
                return
            _log("     reintento único SELECCIONAR…")
            btn2 = self._find_seleccionar_by_titulo(use_cod)
            if btn2 is not None:
                self._click_ion_button(btn2)
                self._pause(1.2)

    def _tarjeta_local_text(self, btn: Any) -> str:
        """Texto de la ion-col / card del botón SELECCIONAR."""
        try:
            return (
                self.driver.execute_script(
                    """
                    const b = arguments[0];
                    const col = b.closest('ion-col');
                    if (col) return col.innerText || '';
                    const card = b.closest('.card, ion-card, [class*="card"]');
                    if (card) return card.innerText || '';
                    const fold = (s) => (s||'').toLowerCase();
                    const isSel = (x) => fold(x.textContent||'').includes('seleccionar')
                      || !!x.querySelector("ion-icon[name='send']");
                    let n = b.parentElement, best = b.textContent || '';
                    while (n) {
                      const sels = Array.from(n.querySelectorAll('ion-button')).filter(isSel);
                      if (sels.length === 1) return n.innerText || best;
                      if (sels.length > 1) break;
                      n = n.parentElement;
                    }
                    return best;
                    """,
                    btn,
                )
                or ""
            )
        except Exception:
            return self._element_label(btn) or ""

    def _find_seleccionar_by_titulo(self, use_cod: bool) -> Any | None:
        """UI: 'Servicio C.O.D.' / 'Servicio Estándar' + SELECCIONAR (icon send)."""
        if use_cod:
            titles = (
                "Servicio C.O.D.",
                "Servicio C.O.D",
                "Servicio COD",
                "C.O.D.",
                "C.O.D",
            )
        else:
            titles = (
                "Servicio Estándar",
                "Servicio Estandar",
                "Estándar",
                "Estandar",
            )
        for title in titles:
            xpaths = (
                f"//app-cotizador-corporativo//ion-col[contains(.,'{title}')]"
                f"//ion-button[.//ion-icon[@name='send'] or "
                f"contains(translate(.,'SELECCIONAR','seleccionar'),'seleccionar')]",
                f"//ion-col[contains(.,'{title}')]//ion-button[.//ion-icon[@name='send']]",
                f"//ion-col[contains(.,'{title}')]//ion-button"
                f"[contains(translate(.,'SELECCIONAR','seleccionar'),'seleccionar')]",
                f"//*[contains(@class,'card')][contains(.,'{title}')]"
                f"//ion-button[.//ion-icon[@name='send']]",
            )
            for xp in xpaths:
                for el in self.driver.find_elements(By.XPATH, xp):
                    try:
                        local = _fold_key(self._tarjeta_local_text(el))
                        if use_cod and "cod" in local:
                            return el
                        if not use_cod and "estandar" in local:
                            return el
                    except Exception:
                        continue
        try:
            return self.driver.execute_script(
                """
                const wantCod = !!arguments[0];
                const fold = (s) => (s||'').toLowerCase()
                  .normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');
                for (const col of document.querySelectorAll(
                  'app-cotizador-corporativo ion-col, ion-col'
                )) {
                  const t = fold(col.innerText || '');
                  const isCod = t.includes('servicio c.o.d') || t.includes('servicio cod');
                  const isStd = t.includes('servicio estandar') || t.includes('servicio estándar');
                  if (wantCod && !isCod) continue;
                  if (!wantCod && !isStd) continue;
                  const btn = Array.from(col.querySelectorAll('ion-button')).find(b => {
                    const bt = fold(b.textContent || '');
                    return bt.includes('seleccionar')
                      || !!b.querySelector("ion-icon[name='send']");
                  });
                  if (btn) return btn;
                }
                return null;
                """,
                use_cod,
            )
        except Exception:
            return None

    def _find_seleccionar_col_side(self, col_index: int) -> Any | None:
        """Fallback forza.side: ion-row tarifas → ion-col[N]//ion-button."""
        # .side: .../ion-row[3]/ion-col[2]/div/div[3]/div/ion-button  (COD)
        xpaths = (
            f"//app-cotizador-corporativo//ion-row[3]/ion-col[{col_index}]"
            f"//ion-button",
            f"//app-cotizador-corporativo//ion-row[last()]//ion-col[{col_index}]"
            f"//ion-button[contains(translate(.,'SELECCIONAR','seleccionar'),'seleccionar') "
            f"or .//ion-icon[@name='send']]",
            f"//app-cotizador-corporativo//ion-col[{col_index}]"
            f"//ion-button[.//ion-icon[@name='send']]",
        )
        for xp in xpaths:
            for el in self.driver.find_elements(By.XPATH, xp):
                try:
                    txt = _fold(self._element_label(el) + " " + (el.get_attribute("textContent") or ""))
                    if "seleccionar" in txt or el.find_elements(
                        By.CSS_SELECTOR, "ion-icon[name='send']"
                    ):
                        if el.is_displayed() or True:
                            return el
                except Exception:
                    continue
        return None

    def _find_seleccionar_servicio_js(self, use_cod: bool) -> Any | None:
        """Seleccionar cuyo bloque local (1 botón) menciona COD o Estándar."""
        try:
            return self.driver.execute_script(
                """
                const wantCod = !!arguments[0];
                const fold = (s) => (s||'').toLowerCase()
                  .normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');
                const key = (s) => fold(s).replace(/[^a-z0-9]+/g,'');
                const isSel = (b) => fold(b.textContent||'').includes('seleccionar')
                  || !!b.querySelector("ion-icon[name='send']");

                const localRoot = (btn) => {
                  let n = btn.parentElement, last = btn;
                  while (n) {
                    const sels = Array.from(n.querySelectorAll('ion-button')).filter(isSel);
                    if (sels.length === 1) return n;
                    if (sels.length > 1) return last;
                    last = n;
                    n = n.parentElement;
                  }
                  return btn;
                };

                let best = null, bestScore = -1;
                for (const b of document.querySelectorAll(
                  'app-cotizador-corporativo ion-button, ion-button'
                )) {
                  if (!isSel(b)) continue;
                  const root = localRoot(b);
                  const k = key(root.innerText || '');
                  const hasCod = k.includes('cod') || k.includes('contraentrega');
                  const hasStd = k.includes('estandar') || k.includes('standard');
                  let score = 0;
                  if (wantCod) {
                    if (hasCod) score += 100;
                    if (hasStd && !hasCod) score -= 200;
                    // penalizar si el bloque local es enorme (mezcla)
                    if ((root.innerText||'').length > 500) score -= 20;
                  } else {
                    if (hasStd) score += 100;
                    if (hasCod && !hasStd) score -= 200;
                  }
                  if (score > bestScore) { bestScore = score; best = b; }
                }
                return bestScore >= 80 ? best : null;
                """,
                use_cod,
            )
        except Exception:
            return None

    def _find_seleccionar_button(
        self,
        prefer: tuple[str, ...],
        avoid: tuple[str, ...],
    ) -> Any | None:
        """Legacy: score por keys en tarjeta local."""
        candidates: list[tuple[int, Any]] = []
        for el in self.driver.find_elements(
            By.CSS_SELECTOR, "app-cotizador-corporativo ion-button, ion-button"
        ):
            try:
                label = _fold(
                    (el.get_attribute("textContent") or "")
                    + " "
                    + self._element_label(el)
                )
                if "seleccionar" not in label and not el.find_elements(
                    By.CSS_SELECTOR, "ion-icon[name='send']"
                ):
                    continue
                ctx_key = _fold_key(self._tarjeta_local_text(el))
                if any(a and a in ctx_key for a in avoid) and not any(
                    p and p in ctx_key for p in prefer
                ):
                    continue
                score = 10
                for p in prefer:
                    if p and p in ctx_key:
                        score += 40
                for a in avoid:
                    if a and a in ctx_key and not any(p and p in ctx_key for p in prefer):
                        score -= 50
                candidates.append((score, el))
            except Exception:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        if prefer and candidates[0][0] < 40:
            return None
        return candidates[0][1]

    def _click_ion_button(self, btn: Any, *, once: bool = False) -> None:
        """Clic en ion-button. once=True evita re-submit (Mostrar Resumen / Mis envíos)."""
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", btn
        )
        self._pause(0.12)
        try:
            self.driver.execute_script(
                """
                const b = arguments[0];
                const once = !!arguments[1];
                if (once && b.__otClicked) return;
                if (once) b.__otClicked = true;
                const inner = b.shadowRoot && b.shadowRoot.querySelector(
                  'button.button-native, button, .button-native'
                );
                if (inner) {
                  inner.click();
                  if (once) {
                    try { inner.disabled = true; } catch (e) {}
                  }
                } else {
                  b.click();
                }
                if (once) {
                  try {
                    b.setAttribute('aria-disabled', 'true');
                    b.classList.add('button-disabled');
                  } catch (e) {}
                }
                """,
                btn,
                once,
            )
        except Exception:
            try:
                btn.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", btn)

    # ------------------------------------------------------------------
    # DETALLES (app-envio-corporativo)
    # Solo Destinatario + referencia (Remitente ya viene precargado).
    # ------------------------------------------------------------------

    def _step_detalles(self, pedido: Pedido, numero_pedido: int | None = None) -> None:
        _log("  6) Destinatario (detalles)")
        try:
            self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "app-envio-corporativo, app-envio")
                )
            )
        except Exception as exc:
            raise RuntimeError("No apareció la pantalla Destinatario") from exc
        self._pause(0.5)

        producto = (pedido.producto or "Producto").strip()
        nombre = (pedido.nombre or "").strip()
        phone = clean_phone(str(pedido.telefono or ""))
        direccion = (pedido.direccion or "").strip()
        indicaciones = build_observations(pedido)
        if numero_pedido is not None and int(numero_pedido) > 0:
            ref_num = str(int(numero_pedido))
        else:
            ref_num = str(int(pedido.fila) - 1) if getattr(pedido, "fila", 0) else "1"

        _log("     opcionDeEntrega = Casa")
        if not self._select_opcion_entrega("Casa"):
            raise RuntimeError(
                "No se pudo marcar Casa en Destinatario (opcionDeEntrega)"
            )

        # Ubicación Destinatario (antes de la referencia; no mezclar campos)
        self._ensure_destinatario_ubicacion(pedido)

        fields = [
            ("qenvia", producto, "Quien recibe"),
            ("nombre", nombre, "Nombre de contacto"),
            ("telefono", phone, "Telefono"),
            ("direccion", direccion, "Direccion destinatario"),
            ("indicacion", indicaciones, "Indicaciones"),
        ]
        side_names = {
            "qenvia": "ion-input-15",
            "nombre": "ion-input-16",
            "telefono": "ion-input-17",
            "direccion": "ion-input-20",
            "indicacion": "ion-input-21",
        }
        for control, value, label in fields:
            _log(f"  -> {label} [{control}]: {str(value)[:50]}")
            ok = self._fill_by_formcontrol(control, value)
            if not ok:
                ok = self._fill_by_input_name(side_names.get(control, ""), value)
            if not ok:
                _log(f"    aviso: no se relleno {control}")
            self._pause(0.08)

        # Referencia = # del listado (campo específico; NUNCA el depto/poblado)
        _log(f"  -> Numero de pedido [referencia]: {ref_num}")
        if not self._fill_numero_referencia(ref_num):
            _log("    aviso: no se relleno numero de referencia")
        self._fix_ubicacion_si_contaminada(pedido)
        _log("     Destinatario: campos listos")

    def _fill_numero_referencia(self, ref_num: str) -> bool:
        """Rellena solo 'Número de referencia' (no depto/municipio/poblado)."""
        # 1) formcontrolname=referencia dentro de app-envio (evitar otros)
        for sel in (
            "app-envio-corporativo app-input[formcontrolname='referencia'] input.native-input",
            "app-envio-corporativo app-input[formcontrolname='referencia'] input",
            "app-input[formcontrolname='referencia'] input.native-input",
            "app-input[formcontrolname='referencia'] input",
        ):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if not el.is_displayed():
                        continue
                    # Rechazar si el bloque es depto/municipio/poblado
                    block = el.find_element(
                        By.XPATH, "./ancestor::app-input[1]|./ancestor::app-select[1]"
                    )
                    bt = _fold(block.text or block.get_attribute("innerText") or "")
                    if any(
                        w in bt
                        for w in (
                            "departamento",
                            "municipio",
                            "poblado",
                            "quien recibe",
                            "quién recibe",
                            "direccion",
                            "dirección",
                            "telefono",
                            "teléfono",
                        )
                    ):
                        continue
                    self._set_input_value(el, ref_num)
                    return True
                except Exception:
                    continue

        # 2) Por label / placeholder
        try:
            el = self.driver.execute_script(
                """
                const fold = (s) => (s||'').toLowerCase()
                  .normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');
                const root = document.querySelector('app-envio-corporativo') || document;
                for (const block of root.querySelectorAll('app-input, .mb-4, ion-item')) {
                  const t = fold(block.innerText || '');
                  const ph = fold((block.querySelector('input')||{}).placeholder || '');
                  if (!(t.includes('numero de referencia') || t.includes('número de referencia')
                        || ph.includes('numero de referencia') || ph.includes('referencia de envio')
                        || ph.includes('referencia de envío'))) continue;
                  if (t.includes('departamento') || t.includes('municipio') || t.includes('poblado'))
                    continue;
                  const inp = block.querySelector('input.native-input, input');
                  if (inp) return inp;
                }
                return null;
                """
            )
            if el is not None:
                self._set_input_value(el, ref_num)
                return True
        except Exception:
            pass

        # 3) formcontrolname global (último)
        return self._fill_by_formcontrol("referencia", ref_num)

    def _fix_ubicacion_si_contaminada(self, pedido: Pedido) -> None:
        """Si depto/poblado quedó con un número corto (ej. '7'), limpiar y reelegir."""
        try:
            bad_val = self.driver.execute_script(
                """
                const root = document.querySelector('app-envio-corporativo');
                if (!root) return null;
                const fold = (s) => (s||'').toLowerCase();
                for (const b of root.querySelectorAll('app-select, app-input')) {
                  const t = fold(b.innerText || '');
                  if (!(t.includes('departamento') || t.includes('municipio')
                        || t.includes('poblado'))) continue;
                  if (t.includes('remitente')) continue;
                  const inp = b.querySelector('input.native-input, input');
                  if (!inp) continue;
                  const val = (inp.value || '').trim();
                  if (val && (/^\\d{1,3}$/.test(val) || val.length < 2)) return val;
                }
                return null;
                """
            )
        except Exception:
            bad_val = None
        if not bad_val:
            return
        _log(f"     ubicacion contaminada ({bad_val!r}): se corrige")
        try:
            self.driver.execute_script(
                """
                const root = document.querySelector('app-envio-corporativo');
                if (!root) return;
                const fold = (s) => (s||'').toLowerCase();
                for (const b of root.querySelectorAll('app-select, app-input')) {
                  const t = fold(b.innerText || '');
                  if (!(t.includes('departamento') || t.includes('municipio')
                        || t.includes('poblado'))) continue;
                  if (t.includes('remitente')) continue;
                  const inp = b.querySelector('input.native-input, input');
                  if (!inp) continue;
                  const val = (inp.value || '').trim();
                  if (val && (/^\\d{1,3}$/.test(val) || val.length < 2)) {
                    inp.value = '';
                    inp.dispatchEvent(new Event('input', {bubbles:true}));
                    inp.dispatchEvent(new Event('change', {bubbles:true}));
                  }
                }
                """
            )
        except Exception:
            pass
        self._ensure_destinatario_ubicacion(pedido, force=True)

    def _ensure_destinatario_ubicacion(
        self, pedido: Pedido, force: bool = False
    ) -> None:
        """Si Destinatario no tiene depto/municipio/poblado válido, rellena."""
        try:
            need = self.driver.execute_script(
                """
                const force = !!arguments[0];
                const root = document.querySelector('app-envio-corporativo');
                if (!root) return false;
                const fold = (s) => (s||'').toLowerCase();
                for (const b of root.querySelectorAll('app-select, app-input, ion-item')) {
                  const t = fold(b.innerText || '');
                  if (!(t.includes('departamento') || t.includes('municipio')
                        || t.includes('poblado'))) continue;
                  if (t.includes('remitente')) continue;
                  const inp = b.querySelector('input.native-input, input');
                  const val = ((inp && inp.value) || '').trim();
                  if (force) return true;
                  if (!val) return true;
                  if (/^\\d{1,3}$/.test(val) || val.length < 2) return true;
                }
                return false;
                """,
                force,
            )
        except Exception:
            need = force
        if not need:
            _log("     ubicacion destinatario: ok / ya tiene valor")
            return

        _log("     ubicacion destinatario: se intenta rellenar")
        opened = False
        for sel in (
            "app-envio-corporativo app-select ion-input input",
            "app-envio-corporativo app-select ion-input",
            "app-envio-corporativo app-select",
        ):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if not el.is_displayed():
                        continue
                    if self._is_inside_remitente(el):
                        continue
                    # No abrir si el label es referencia
                    parent_txt = _fold(
                        self.driver.execute_script(
                            "const n=arguments[0].closest('app-select,app-input,.mb-4');"
                            "return n ? (n.innerText||'') : '';",
                            el,
                        )
                        or ""
                    )
                    if "referencia" in parent_txt and "poblado" not in parent_txt:
                        continue
                    self._js_click(el)
                    opened = True
                    self._pause(0.5)
                    break
                except Exception:
                    continue
            if opened:
                break
        if not opened:
            _log("     aviso: no se abrio selector ubicacion destinatario")
            return
        try:
            ubic = forza_location_from_pedido(
                direccion=str(pedido.direccion or ""),
                referencia=str(pedido.referencia or ""),
                departamento=str(pedido.departamento or ""),
                municipio=str(pedido.municipio or ""),
                colonia=str(getattr(pedido, "colonia", "") or ""),
            )
            self._buscar_y_elegir_poblado(ubic)
        except Exception as exc:
            _log(f"     aviso ubicacion destinatario: {exc}")

    def _select_opcion_entrega(self, opcion: str = "Casa") -> bool:
        """Marca Casa/Oficina en ion-radio-group[formcontrolname=opcionDeEntrega]."""
        want = _fold(opcion)
        # Clic directo por JS (más fiable que XPath + shadow)
        try:
            ok = self.driver.execute_script(
                """
                const want = (arguments[0] || 'casa').toLowerCase();
                const group = document.querySelector(
                  "ion-radio-group[formcontrolname='opcionDeEntrega']"
                );
                if (!group) return false;
                const cols = Array.from(group.querySelectorAll('ion-col'));
                for (const col of cols) {
                  const t = (col.innerText || '').toLowerCase();
                  if (!t.includes(want)) continue;
                  const radio = col.querySelector('ion-radio');
                  if (!radio) continue;
                  radio.click();
                  const inp = radio.shadowRoot && radio.shadowRoot.querySelector('input[type=radio]');
                  if (inp) { inp.click(); inp.checked = true; }
                  radio.setAttribute('aria-checked', 'true');
                  return true;
                }
                const radios = Array.from(group.querySelectorAll('ion-radio'));
                if (!radios.length) return false;
                const idx = want.includes('oficina') ? Math.min(1, radios.length-1) : 0;
                radios[idx].click();
                return true;
                """,
                opcion,
            )
            if ok:
                self._pause(0.25)
                return True
        except Exception:
            pass

        groups = self.driver.find_elements(
            By.CSS_SELECTOR,
            "ion-radio-group[formcontrolname='opcionDeEntrega']",
        )
        for g in groups:
            try:
                radios = [
                    r
                    for r in g.find_elements(By.CSS_SELECTOR, "ion-radio")
                    if r.is_displayed()
                ]
                if not radios:
                    continue
                target = (
                    radios[0]
                    if want == "casa"
                    else (radios[1] if len(radios) > 1 else radios[0])
                )
                self._js_click(target)
                self._pause(0.25)
                return True
            except Exception:
                continue
        return False

    def _destinatario_root(self) -> Any | None:
        """Raíz del bloque Destinatario (form con Casa/Oficina)."""
        for g in self.driver.find_elements(
            By.CSS_SELECTOR, "ion-radio-group[formcontrolname='opcionDeEntrega']"
        ):
            try:
                return g.find_element(By.XPATH, "./ancestor::form[1]")
            except Exception:
                try:
                    return g.find_element(By.XPATH, "./ancestor::ion-col[1]")
                except Exception:
                    continue
        cols = self.driver.find_elements(
            By.XPATH,
            "//app-envio-corporativo//ion-grid/ion-row[1]/ion-col",
        )
        if len(cols) >= 2:
            return cols[1]
        return None

    def _is_inside_remitente(self, el: Any) -> bool:
        """True si el input está en Remitente (form sin opcionDeEntrega)."""
        try:
            return bool(
                self.driver.execute_script(
                    """
                    const el = arguments[0];
                    const form = el.closest('form');
                    if (form) {
                      if (form.querySelector("ion-radio-group[formcontrolname='opcionDeEntrega']")) {
                        return false; // Destinatario
                      }
                      const t = (form.innerText || '').toLowerCase();
                      if (t.includes('remitente') || t.includes('quién envía')
                          || t.includes('quien envia') || t.includes('dirección en remitente')
                          || t.includes('direccion en remitente')) {
                        return true;
                      }
                    }
                    const col = el.closest('ion-col');
                    if (col) {
                      const t = (col.innerText || '').toLowerCase();
                      if ((t.includes('remitente') || t.includes('quién envía')
                           || t.includes('quien envia'))
                          && !t.includes('quién recibe') && !t.includes('quien recibe')
                          && !t.includes('opciones de entrega')) {
                        return true;
                      }
                    }
                    return false;
                    """,
                    el,
                )
            )
        except Exception:
            return False

    def _fill_by_formcontrol(self, name: str, value: str) -> bool:
        """Rellena app-input[formcontrolname=...] solo en Destinatario (+ referencia)."""
        selectors = (
            f"app-input[formcontrolname='{name}'] input.native-input",
            f"app-input[formcontrolname='{name}'] ion-input input",
            f"app-input[formcontrolname='{name}'] input",
            f"app-input[formcontrolname='{name}'] textarea",
        )
        roots: list[Any] = []
        if name == "referencia":
            roots = [self.driver]
        else:
            dest = self._destinatario_root()
            roots = [dest] if dest is not None else [self.driver]

        for root in roots:
            for sel in selectors:
                try:
                    els = root.find_elements(By.CSS_SELECTOR, sel)
                except Exception:
                    continue
                for el in els:
                    try:
                        if not el.is_displayed():
                            continue
                        if name != "referencia" and self._is_inside_remitente(el):
                            continue
                        self._set_input_value(el, value)
                        return True
                    except Exception:
                        continue
        return False

    def _fill_by_input_name(self, name: str, value: str) -> bool:
        if not name:
            return False
        for el in self.driver.find_elements(By.CSS_SELECTOR, f"input[name='{name}']"):
            try:
                if not el.is_displayed():
                    continue
                if self._is_inside_remitente(el):
                    continue
                self._set_input_value(el, value)
                return True
            except Exception:
                continue
        return False

    def _set_input_value(self, el: Any, value: str) -> None:
        """Escribe valor vía JS (sin send_keys: en Ionic puede colgarse)."""
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el
            )
        except Exception:
            pass
        self._pause(0.05)
        self.driver.execute_script(
            """
            const el = arguments[0], val = arguments[1] || '';
            try { el.focus(); } catch (e) {}
            el.value = val;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            try {
              el.dispatchEvent(new InputEvent('input', {
                bubbles: true, data: val, inputType: 'insertText'
              }));
            } catch (e) {}
            const ion = el.closest('ion-input') || el.closest('ion-textarea');
            if (ion) {
              try { ion.value = val; } catch (e) {}
              ion.dispatchEvent(new CustomEvent('ionInput', {
                detail: { value: val }, bubbles: true
              }));
              ion.dispatchEvent(new CustomEvent('ionChange', {
                detail: { value: val }, bubbles: true
              }));
            }
            try { el.blur(); } catch (e) {}
            """,
            el,
            value or "",
        )
        self._pause(0.06)

    def _fill_app_input_by_index(self, app: str, index: int, value: str) -> bool:
        inputs = self._list_app_inputs(app)
        if 0 <= index < len(inputs):
            self._set_input_value(inputs[index], value)
            return True
        return False

    def _list_app_inputs(self, app: str) -> list[Any]:
        out: list[Any] = []
        for block in self.driver.find_elements(By.CSS_SELECTOR, f"{app} app-input"):
            try:
                if not block.is_displayed():
                    continue
                fields = block.find_elements(
                    By.CSS_SELECTOR,
                    "ion-input input, input.native-input, input, textarea",
                )
                visibles = [f for f in fields if f.is_displayed()]
                if visibles:
                    out.append(visibles[0])
            except Exception:
                continue
        if not out:
            for el in self.driver.find_elements(
                By.CSS_SELECTOR,
                f"{app} ion-input input, {app} input.native-input, {app} textarea",
            ):
                try:
                    if el.is_displayed():
                        out.append(el)
                except Exception:
                    continue
        return out

    def _step_siguiente_detalles(self) -> None:
        """Siguiente en Destinatario (NUNCA Mostrar Resumen)."""
        _log(" 13) Siguiente (detalles)")
        btn = self._find_button_by_text(
            ["Siguiente", "SIGUIENTE"],
            scope="app-envio-corporativo",
            require_primary=True,
            require_arrow=True,
        )
        if btn is None:
            btn = self._find_button_by_text(
                ["Siguiente", "SIGUIENTE"],
                scope="app-envio-corporativo",
                require_primary=False,
                require_arrow=False,
            )
        if btn is None:
            # Fallback: ion-button[2] del footer de detalles
            for xp in (
                "//app-envio-corporativo//ion-button[.//ion-icon[contains(@name,'arrow-forward')]]",
                "//app-envio-corporativo//ion-button[contains(.,'Siguiente')]",
            ):
                for el in self.driver.find_elements(By.XPATH, xp):
                    txt = _fold(self._element_label(el))
                    if "regresar" in txt or "mostrar resumen" in txt:
                        continue
                    btn = el
                    break
                if btn is not None:
                    break
        if btn is None:
            raise RuntimeError("No se encontró botón Siguiente en Destinatario")
        self._wait_button_enabled(btn, timeout=6)
        self._click_ion_button(btn)
        self._wait_page_marker(("contra entrega", "seguro", "Mostrar Resumen", "COD"), timeout=6)

    def _step_cod_monto(self, pedido: Pedido, use_cod: bool) -> None:
        """COD & Seguro: precio en monto COD; si pagado, COD apagado (sin flickers)."""
        precio = re.sub(r"[^\d.]", "", str(pedido.precio or "").strip()) or "0"
        _log(f" 14) COD / servicios (monto={precio}, cod={use_cod})")
        try:
            self.wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "app-servicios-corporativo")
                )
            )
        except Exception:
            pass
        self._pause(0.7)

        # COD: solo clic si el estado actual difiere (evita on/off al llegar)
        self._set_servicios_toggle(
            ["contra entrega", "c.o.d", "cod", "cobro contra", "servicio cod"],
            on=use_cod,
            exclude=["seguro"],
        )

        if use_cod:
            filled = self._fill_monto_cod(precio)
            if not filled:
                _log("    aviso: no se pudo escribir Monto COD")
        else:
            _log("     COD desmarcado (pedido pagado); no se escribe monto")

        # Seguro adicional siempre off (no confundir con toggle COD)
        self._set_servicios_toggle(
            ["seguro adicional", "seguro"],
            on=False,
            exclude=["cod", "c.o.d", "contra entrega", "cobro"],
        )
        self._pause(0.35)

    def _fill_monto_cod(self, precio: str) -> bool:
        """Escribe el precio del producto en Monto COD (ion-input-25 / labels)."""
        if self._fill_by_input_name("ion-input-25", precio):
            return True
        filled = self._fill_by_label(
            [
                "Monto COD",
                "Ingresar monto COD",
                "Monto a cobrar",
                "Valor a cobrar",
                "Cantidad a cobrar",
                "Monto",
            ],
            precio,
            required=False,
            scope="app-servicios-corporativo",
        )
        if filled:
            return True
        # Placeholder exacto
        for el in self.driver.find_elements(
            By.CSS_SELECTOR,
            "app-servicios-corporativo input.native-input, "
            "app-servicios-corporativo input",
        ):
            try:
                if not el.is_displayed():
                    continue
                ph = _fold(el.get_attribute("placeholder") or "")
                if "monto" in ph and "cod" in ph:
                    self._set_input_value(el, precio)
                    return True
            except Exception:
                continue
        # .side legacy / primer app-input del bloque
        if self._fill_by_input_name("ion-input-28", precio):
            return True
        return self._fill_app_input_by_index("app-servicios-corporativo", 0, precio)

    def _ion_toggle_is_on(self, tg: Any) -> bool:
        try:
            return bool(
                self.driver.execute_script(
                    """
                    const t = arguments[0];
                    const aria = (t.getAttribute('aria-checked') || '').toLowerCase();
                    if (aria === 'true') return true;
                    if (aria === 'false') return false;
                    if (t.classList && t.classList.contains('toggle-checked')) return true;
                    if (t.checked === true) return true;
                    if (t.checked === false) return false;
                    const inp = t.shadowRoot && t.shadowRoot.querySelector('input[type=checkbox]');
                    if (inp) return !!inp.checked;
                    return false;
                    """,
                    tg,
                )
            )
        except Exception:
            checked = (tg.get_attribute("aria-checked") or "").lower()
            if checked == "true":
                return True
            if checked == "false":
                return False
            return "toggle-checked" in (tg.get_attribute("class") or "")

    def _set_servicios_toggle(
        self,
        labels: list[str],
        on: bool,
        exclude: list[str] | None = None,
    ) -> bool:
        """Enciende/apaga un ion-toggle de app-servicios-corporativo sin doble clic."""
        want = [_fold(x) for x in labels if x]
        ban = [_fold(x) for x in (exclude or []) if x]
        toggles = self.driver.find_elements(
            By.CSS_SELECTOR, "app-servicios-corporativo ion-toggle"
        )
        if not toggles:
            toggles = self.driver.find_elements(By.CSS_SELECTOR, "ion-toggle")

        best = None
        best_score = -1
        for tg in toggles:
            try:
                if not tg.is_displayed():
                    continue
                txt = _fold(
                    self.driver.execute_script(
                        """
                        let n = arguments[0], best = '';
                        for (let i = 0; i < 6 && n; i++) {
                          const t = (n.innerText || '').replace(/\\s+/g, ' ').trim();
                          if (t && t.length < 180 && (best === '' || t.length < best.length)) {
                            best = t;
                          }
                          n = n.parentElement;
                        }
                        return best;
                        """,
                        tg,
                    )
                    or ""
                )
                if any(b and b in txt for b in ban) and not any(
                    w and w in txt for w in want
                ):
                    continue
                score = 0
                for w in want:
                    if w and w in txt:
                        score += 10 + len(w)
                if score > best_score:
                    best_score = score
                    best = tg
            except Exception:
                continue

        if best is None or best_score <= 0:
            # Fallback acotado al helper viejo solo dentro de servicios
            self._set_toggle_near(
                [x for x in labels if x][:3],
                on=on,
            )
            return False

        is_on = self._ion_toggle_is_on(best)
        _log(f"     toggle ({labels[0]}): ahora={'ON' if is_on else 'OFF'} → {'ON' if on else 'OFF'}")
        if is_on == on:
            return True
        self._js_click(best)
        self._pause(0.35)
        # Un solo reintento si el estado no cambió (nunca un segundo flip “por si acaso”)
        if self._ion_toggle_is_on(best) != on:
            self._js_click(best)
            self._pause(0.25)
        return self._ion_toggle_is_on(best) == on

    def _step_siguiente_servicios(self) -> None:
        """COD & Seguro → Mostrar Resumen (primary) UNA sola vez → pantalla final."""
        _log(" 15) Mostrar Resumen (COD / servicios)")
        if self._resumen_confirmado:
            _log("     ya confirmado — se omite")
            return

        clicked = self._click_mostrar_resumen("app-servicios-corporativo")
        if not clicked:
            clicked = self._click_mostrar_resumen("")
        if not clicked:
            # Fallback legacy (solo si aún no se confirmó)
            if not self._click_siguiente("app-servicios-corporativo"):
                raise RuntimeError(
                    "No se encontró botón Mostrar Resumen / Siguiente en COD"
                )
            # Siguiente en COD no es lo mismo que confirmar guía; no marcar aún
        else:
            self._resumen_confirmado = True

        # Esperar pantalla de pago/facturación (NO usar texto del menú "Mis Envíos")
        if not self._wait_pantalla_cierre(timeout=12):
            _log("     aviso: pantalla de cierre lenta (sin re-pulsar Mostrar Resumen)")
            self._pause(1.5)

    def _step_resumen_pago(self) -> None:
        """Tras Mostrar Resumen: esperar Mis envíos (botón). No re-enviar la guía."""
        _log(" 16) Resumen → esperar cierre (sin re-enviar)")
        self._pause(0.4)
        if self._find_mis_envios_button() is not None:
            _log("     botón Mis envíos visible")
            return
        if self._wait_pantalla_cierre(timeout=8):
            return
        # Solo si NUNCA se confirmó, intentar una vez
        if not self._resumen_confirmado:
            _log("     Mostrar Resumen pendiente — un intento")
            if self._click_mostrar_resumen("app-pago-servicio-corporativo") or self._click_mostrar_resumen(
                ""
            ):
                self._resumen_confirmado = True
                self._wait_pantalla_cierre(timeout=8)
        else:
            _log("     guía ya enviada — no se pulsa Mostrar Resumen de nuevo")
        self._dismiss_overlays()

    def _click_mostrar_resumen(self, scope: str = "") -> bool:
        """Clic en ion-button primary 'Mostrar Resumen' (+ flecha). Máximo 1 por pedido."""
        if self._resumen_confirmado:
            _log("     Mostrar Resumen: omitido (ya confirmado)")
            return True
        try:
            self.wait.until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "ion-button.ion-color-primary, ion-button[color='primary']",
                    )
                )
            )
        except Exception:
            pass
        self._pause(0.3)

        btn = self._find_button_by_text(
            ["Mostrar Resumen", "Mostrar resumen", "MOSTRAR RESUMEN"],
            scope=scope,
            require_primary=True,
            require_arrow=True,
        )
        if btn is None and scope:
            btn = self._find_button_by_text(
                ["Mostrar Resumen", "Mostrar resumen"],
                scope="",
                require_primary=True,
                require_arrow=False,
            )
        if btn is None:
            # JS textContent
            try:
                btn = self.driver.execute_script(
                    """
                    const fold = (s) => (s||'').toLowerCase()
                      .normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');
                    for (const b of document.querySelectorAll('ion-button')) {
                      if (b.__otClicked) continue;
                      const t = fold(b.textContent || '');
                      if (t.includes('mostrar resumen')) return b;
                    }
                    return null;
                    """
                )
            except Exception:
                btn = None
        if btn is None:
            return False
        self._wait_button_enabled(btn, timeout=10)
        self._click_ion_button(btn, once=True)
        self._resumen_confirmado = True
        _log("     click: Mostrar Resumen (1x)")
        return True

    def _wait_pantalla_cierre(self, timeout: float = 10.0) -> bool:
        """Espera app de pago/facturación o botón Mis envíos (ignora menú lateral)."""
        try:
            WebDriverWait(self.driver, timeout, poll_frequency=0.2).until(
                lambda _d: self._find_mis_envios_button() is not None
                or bool(
                    self.driver.execute_script(
                        """
                        return !!(
                          document.querySelector('app-pago-facturacion')
                          || document.querySelector('app-pago-servicio-corporativo')
                        );
                        """
                    )
                )
            )
            return True
        except Exception:
            return False

    def _find_button_by_text(
        self,
        texts: list[str],
        scope: str = "",
        require_primary: bool = False,
        require_arrow: bool = False,
    ) -> Any | None:
        want = [_fold(t) for t in texts]
        root = f"{scope} " if scope else ""
        selectors = (
            f"{root}ion-button.ion-color-primary",
            f"{root}ion-button[color='primary']",
            f"{root}ion-button",
            "ion-button",
        )
        scored: list[tuple[int, Any]] = []
        seen: set[int] = set()
        for sel in selectors:
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    eid = id(el)
                    if eid in seen:
                        continue
                    seen.add(eid)
                    txt = _fold(
                        (el.get_attribute("textContent") or "")
                        + " "
                        + self._element_label(el)
                    )
                    if not any(w and w in txt for w in want):
                        continue
                    score = 50
                    cls = el.get_attribute("class") or ""
                    if "ion-color-primary" in cls or el.get_attribute("color") == "primary":
                        score += 20
                    elif require_primary:
                        continue
                    has_arrow = bool(
                        el.find_elements(
                            By.CSS_SELECTOR,
                            "ion-icon[name='arrow-forward-outline'], "
                            "ion-icon[name*='arrow-forward']",
                        )
                    )
                    if has_arrow:
                        score += 15
                    elif require_arrow:
                        score -= 5
                    try:
                        if el.is_displayed():
                            score += 10
                    except Exception:
                        pass
                    scored.append((score, el))
                except Exception:
                    continue
        if not scored:
            for t in texts:
                for xp in (
                    f"//ion-button[contains(normalize-space(.),'{t}')]",
                    f"//{scope}//ion-button[contains(.,'{t}')]" if scope else "",
                ):
                    if not xp:
                        continue
                    for el in self.driver.find_elements(By.XPATH, xp):
                        scored.append((40, el))
        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def _click_siguiente(self, scope: str = "") -> bool:
        """Clic en ion-button 'Siguiente' (no Mostrar Resumen)."""
        try:
            self.wait.until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "ion-button.ion-color-primary, ion-button[color='primary'], ion-button",
                    )
                )
            )
        except Exception:
            pass
        self._pause(0.25)

        btn = self._find_siguiente_button(scope)
        if btn is None and scope:
            btn = self._find_siguiente_button("")
        if btn is None:
            btn = self._find_siguiente_button_js(scope)
        if btn is None:
            return False
        # No confundir con Mostrar Resumen
        try:
            if "mostrar resumen" in _fold(self._element_label(btn)):
                return False
        except Exception:
            pass
        self._wait_button_enabled(btn, timeout=6)
        self._click_ion_button(btn)
        self._pause(0.3)
        return True

    def _find_siguiente_button(self, scope: str = "") -> Any | None:
        """
        Botón real de Forza:
          <ion-button color="primary"> Siguiente
            <ion-icon name="arrow-forward-outline">
        """
        # 1) XPath directo (texto light DOM + icono)
        if scope:
            xp_roots = [f"//{scope}", ""]
        else:
            xp_roots = [
                "",
                "//app-servicios-corporativo",
                "//app-pago-servicio-corporativo",
                "//app-envio-corporativo",
                "//app-pago-facturacion",
            ]

        xpaths = []
        for root in xp_roots:
            prefix = root if root else ""
            xpaths.extend(
                [
                    f"{prefix}//ion-button[.//ion-icon[@name='arrow-forward-outline']]",
                    f"{prefix}//ion-button[.//ion-icon[contains(@name,'arrow-forward')]]",
                    f"{prefix}//ion-button[contains(normalize-space(.),'Siguiente')]",
                    f"{prefix}//ion-button[contains(normalize-space(.),'SIGUIENTE')]",
                    f"{prefix}//ion-button[contains(normalize-space(.),'siguiente')]",
                    f"{prefix}//ion-button[@color='primary' and .//ion-icon[contains(@name,'arrow')]]",
                ]
            )

        for xp in xpaths:
            if not xp.startswith("//") and not xp.startswith("/"):
                continue
            # Normalizar: "" + "//ion..." ya es "//ion..."
            for el in self.driver.find_elements(By.XPATH, xp):
                try:
                    txt = _fold(self._element_label(el))
                    if any(
                        a in txt
                        for a in ("regresar", "cancelar", "atras", "atrás", "anterior")
                    ):
                        continue
                    # Preferir visibles; si ninguno visible, devolver el primero válido
                    if el.is_displayed():
                        return el
                except Exception:
                    continue

        # 2) CSS + score (incluye no-visibles como último recurso)
        root = f"{scope} " if scope else ""
        selectors = (
            f"{root}ion-button.ion-color-primary",
            f"{root}ion-button[color='primary']",
            f"{root}ion-button",
            "ion-button.ion-color-primary",
            "ion-button[color='primary']",
            "ion-button",
        )
        scored: list[tuple[int, Any]] = []
        seen: set[int] = set()
        for sel in selectors:
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    eid = id(el)
                    if eid in seen:
                        continue
                    seen.add(eid)
                    txt = _fold(
                        (el.get_attribute("textContent") or "")
                        + " "
                        + (el.get_attribute("innerText") or "")
                        + " "
                        + self._element_label(el)
                    )
                    if any(
                        a in txt
                        for a in (
                            "regresar",
                            "cancelar",
                            "atras",
                            "atrás",
                            "volver",
                            "anterior",
                        )
                    ):
                        continue
                    has_arrow = bool(
                        el.find_elements(
                            By.CSS_SELECTOR,
                            "ion-icon[name='arrow-forward-outline'], "
                            "ion-icon[name='arrow-forward'], "
                            "ion-icon[name*='arrow']",
                        )
                    )
                    score = 0
                    if "siguiente" in txt:
                        score += 50
                    elif "continuar" in txt:
                        score += 30
                    if has_arrow:
                        score += 40
                    cls = el.get_attribute("class") or ""
                    if "ion-color-primary" in cls or el.get_attribute("color") == "primary":
                        score += 20
                    try:
                        if el.is_displayed():
                            score += 10
                    except Exception:
                        pass
                    # Primary + flecha aunque el texto no se lea (slots Ionic)
                    if has_arrow and (
                        "ion-color-primary" in cls or el.get_attribute("color") == "primary"
                    ):
                        score = max(score, 60)
                    if score > 0:
                        scored.append((score, el))
                except Exception:
                    continue

        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def _find_siguiente_button_js(self, scope: str = "") -> Any | None:
        """Fallback: localiza Siguiente por JS (textContent real del light DOM)."""
        try:
            el = self.driver.execute_script(
                """
                const scope = arguments[0] || '';
                const roots = [];
                if (scope) {
                  const s = document.querySelector(scope);
                  if (s) roots.push(s);
                }
                roots.push(document);
                const pick = (root) => {
                  const list = Array.from(root.querySelectorAll('ion-button'));
                  let best = null, bestScore = -1;
                  for (const b of list) {
                    const style = window.getComputedStyle(b);
                    if (style && (style.display === 'none' || style.visibility === 'hidden')) continue;
                    const txt = (b.textContent || '').toLowerCase()
                      .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
                    if (txt.includes('regresar') || txt.includes('cancelar')) continue;
                    const icon = b.querySelector(
                      "ion-icon[name='arrow-forward-outline'], ion-icon[name*='arrow-forward']"
                    );
                    let score = 0;
                    if (txt.includes('siguiente')) score += 50;
                    if (icon) score += 40;
                    if (b.getAttribute('color') === 'primary'
                        || (b.className || '').includes('ion-color-primary')) score += 20;
                    if (score > bestScore) { bestScore = score; best = b; }
                  }
                  return bestScore >= 40 ? best : null;
                };
                for (const r of roots) {
                  const hit = pick(r);
                  if (hit) return hit;
                }
                // Último recurso: cualquier primary con flecha
                for (const b of document.querySelectorAll('ion-button')) {
                  const icon = b.querySelector("ion-icon[name*='arrow-forward']");
                  if (icon && ((b.className||'').includes('ion-color-primary')
                               || b.getAttribute('color')==='primary')) {
                    return b;
                  }
                }
                return null;
                """,
                scope or "",
            )
            return el
        except Exception:
            return None

    def _find_forward_button(self, scope: str) -> Any | None:
        """Alias: botón Siguiente / continuar hacia adelante."""
        return self._find_siguiente_button(scope)

    def _step_facturacion(self) -> None:
        """Último paso: Mis envíos (secondary) UNA vez. No re-pulsar Mostrar Resumen."""
        _log(" 17) Mis envíos (cierre)")
        self._pause(0.5)

        # Esperar botón real (el menú lateral también dice Mis Envíos)
        self._wait_pantalla_cierre(timeout=10)

        clicked = False
        for attempt in range(6):
            clicked = self._click_mis_envios()
            if clicked:
                break
            _log(f"     Mis envíos no listo (intento {attempt+1}/6)…")
            # NUNCA volver a Mostrar Resumen aquí (crea guías extra)
            self._dismiss_overlays()
            self._pause(0.9)

        if not clicked:
            clicked = self._click_any_secondary_cierre()

        if not clicked:
            try:
                self.driver.save_screenshot("debug_forza_mis_envios.png")
            except Exception:
                pass
            raise RuntimeError("No se encontró el botón Mis envíos (cierre)")

        self._pause(0.8)
        # Alert "¿Desea finalizar el proceso?" — aceptar una sola vez
        self._dismiss_overlays()
        self._pause(0.4)

    def _find_mis_envios_button(self) -> Any | None:
        """Localiza el ion-button 'Mis envíos' (no el ítem del menú)."""
        try:
            return self.driver.execute_script(
                """
                const fold = (s) => (s||'').toLowerCase()
                  .normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');
                const labelOf = (b) => fold(
                  (b.textContent || '') + ' ' + (b.innerText || '') + ' ' +
                  Array.from(b.childNodes).map(n => n.textContent || '').join(' ')
                );
                let best = null, bestScore = -1;
                for (const b of document.querySelectorAll('ion-button')) {
                  if (b.__otClicked) continue;
                  const t = labelOf(b);
                  if (t.includes('regresar') || t.includes('cancelar')
                      || t.includes('mostrar resumen')) continue;
                  let score = 0;
                  if (t.includes('mis envios') || t.includes('mis envio')) score += 100;
                  else if (t.includes('envios') && t.includes('mis')) score += 80;
                  else continue;
                  const secondary = (b.className||'').includes('ion-color-secondary')
                    || b.getAttribute('color') === 'secondary';
                  if (secondary) score += 25;
                  if (b.closest('app-pago-facturacion')) score += 20;
                  if (b.closest('app-pago-servicio-corporativo')) score += 10;
                  // Excluir menú
                  if (b.closest('ion-menu')) score -= 100;
                  try {
                    const st = window.getComputedStyle(b);
                    if (st && st.display === 'none') continue;
                  } catch (e) {}
                  if (score > bestScore) { bestScore = score; best = b; }
                }
                return bestScore >= 80 ? best : null;
                """
            )
        except Exception:
            return None

    def _page_has_mis_envios_text(self) -> bool:
        """True solo si existe el botón de cierre (no el texto del menú)."""
        return self._find_mis_envios_button() is not None

    def _click_any_secondary_cierre(self) -> bool:
        """Fallback: secondary en app-pago-facturacion / pago-servicio."""
        for sel in (
            "app-pago-facturacion ion-button.ion-color-secondary",
            "app-pago-facturacion ion-button[color='secondary']",
            "app-pago-servicio-corporativo ion-button.ion-color-secondary",
        ):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    txt = _fold(
                        (el.get_attribute("textContent") or "")
                        + " "
                        + self._element_label(el)
                    )
                    if any(
                        x in txt
                        for x in ("regresar", "cancelar", "mostrar resumen")
                    ):
                        continue
                    if "mis env" not in txt and "envio" not in txt:
                        continue
                    self._click_ion_button(el, once=True)
                    _log(f"     click secondary fallback ({txt[:40]!r})")
                    return True
                except Exception:
                    continue
        return False

    def _click_mis_envios(self) -> bool:
        """Clic en ion-button color=secondary 'Mis envíos' (una sola vez)."""
        btn = self._find_mis_envios_button()

        # Fallback XPath (solo con texto Mis envíos, no cualquier secondary)
        if btn is None:
            for xp in (
                "//app-pago-facturacion//ion-button[contains(.,'Mis env')]",
                "//app-pago-servicio-corporativo//ion-button[contains(.,'Mis env')]",
                "//ion-button[contains(normalize-space(.),'Mis envíos')]",
                "//ion-button[contains(normalize-space(.),'Mis envios')]",
            ):
                for el in self.driver.find_elements(By.XPATH, xp):
                    try:
                        if el.get_attribute("__otClicked"):
                            continue
                        txt = _fold(
                            (el.get_attribute("textContent") or "")
                            + " "
                            + self._element_label(el)
                        )
                        if "regresar" in txt or "cancelar" in txt:
                            continue
                        if "mis env" in txt:
                            btn = el
                            break
                    except Exception:
                        continue
                if btn is not None:
                    break

        if btn is None:
            return False

        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", btn
            )
        except Exception:
            pass
        self._pause(0.2)
        self._wait_button_enabled(btn, timeout=4)
        self._click_ion_button(btn, once=True)
        _log("     click: Mis envíos (1x)")
        return True

    def _wait_button_enabled(self, btn: Any, timeout: float = 6.0) -> None:
        """Espera eficientemente a que un ion-button deje de estar disabled."""
        def enabled(_driver: Any) -> bool:
            try:
                cls = btn.get_attribute("class") or ""
                aria = (btn.get_attribute("aria-disabled") or "").lower()
                disabled_attr = btn.get_attribute("disabled")
                return not (
                    aria == "true"
                    or "button-disabled" in cls
                    or "ion-disabled" in cls
                    or disabled_attr is not None
                )
            except Exception:
                return False
        try:
            WebDriverWait(self.driver, timeout, poll_frequency=0.12).until(enabled)
        except Exception:
            # Mantener compatibilidad: el caller decide si el click posterior falla.
            pass

    def _step_confirmar(self) -> None:
        """Compat: resumen + facturación (flujo completo post-COD)."""
        self._step_siguiente_servicios()
        self._step_resumen_pago()
        self._step_facturacion()

    # ------------------------------------------------------------------
    # Helpers UI
    # ------------------------------------------------------------------

    def _dismiss_overlays(self) -> None:
        """Cierra alerts (p. ej. ¿Desea finalizar el proceso?)."""
        for label in ("Aceptar", "ACEPTAR", "OK", "Sí", "Si"):
            if self._click_by_text(
                [label], tags=("ion-button", "button", "button.alert-button")
            ):
                self._pause(0.3)
                return
        for sel in (
            "ion-alert button.alert-button-role-confirm",
            "ion-alert .alert-button-role-confirm",
            "ion-alert button",
            ".alert-button",
        ):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        self._js_click(el)
                        self._pause(0.3)
                        return
                except Exception:
                    continue

    def _click_radio_or_label(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return False
        xpaths = (
            f"//ion-radio[contains(.,'{t}')]",
            f"//ion-label[contains(.,'{t}')]",
            f"//*[contains(.,'{t}') and (self::span or self::div or self::label)]",
        )
        for xp in xpaths:
            if self._click_xpath(xp):
                self._pause(0.15)
                return True
        return False

    def _fill_by_label(
        self,
        labels: list[str],
        value: str,
        required: bool = True,
        scope: str = "app-envio-corporativo",
    ) -> bool:
        """Rellena el input del app-input que contiene la etiqueta (scoped)."""
        for lab in labels:
            # Preferir app-input concreto que menciona el label
            xpaths = (
                f"//{scope}//app-input[.//*[contains(normalize-space(.),'{lab}')]]"
                f"//input[contains(@class,'native-input') or true()]",
                f"//{scope}//app-input[contains(.,'{lab}')]//input",
                f"//{scope}//app-input[contains(.,'{lab}')]//textarea",
                f"//{scope}//label[contains(.,'{lab}')]/ancestor::app-input[1]//input",
                f"//{scope}//*[self::ion-label or self::label]"
                f"[contains(normalize-space(.),'{lab}')]"
                f"/ancestor::app-input[1]//input",
            )
            for xp in xpaths:
                for el in self.driver.find_elements(By.XPATH, xp):
                    try:
                        if not el.is_displayed():
                            continue
                        self._set_input_value(el, value)
                        return True
                    except Exception:
                        continue
        if required:
            _log(f"    aviso: no se encontró campo para {labels[0]!r}")
        return False

    def _set_toggle_near(self, labels: list[str], on: bool) -> None:
        for lab in labels:
            toggles = self.driver.find_elements(
                By.XPATH,
                f"//*[contains(.,'{lab}')]/ancestor::*[.//ion-toggle][1]//ion-toggle"
                f" | //*[contains(.,'{lab}')]/following::ion-toggle[1]",
            )
            for tg in toggles:
                try:
                    if not tg.is_displayed():
                        continue
                    checked = (tg.get_attribute("aria-checked") or "").lower()
                    is_on = checked in ("true",)
                    if "toggle-checked" in (tg.get_attribute("class") or ""):
                        is_on = True
                    if on != is_on:
                        self._js_click(tg)
                        self._pause(0.25)
                    return
                except Exception:
                    continue

    def _click_by_text(
        self,
        texts: list[str],
        tags: tuple[str, ...] = ("ion-button", "button"),
        exact: bool = False,
    ) -> bool:
        for tag in tags:
            if tag == "*":
                for t in texts:
                    xp = (
                        f"//*[normalize-space()='{t}']"
                        if exact
                        else f"//*[contains(.,'{t}')]"
                    )
                    if self._click_xpath(xp):
                        return True
                continue
            folded = [_fold(t) for t in texts]
            for el in self.driver.find_elements(By.CSS_SELECTOR, tag):
                try:
                    if not el.is_displayed():
                        continue
                    txt = _fold(el.text or el.get_attribute("innerText") or "")
                    if any(f == txt or f in txt for f in folded):
                        self._js_click(el)
                        return True
                except Exception:
                    continue
        return False

    def _click_xpath(self, xpath: str) -> bool:
        for el in self.driver.find_elements(By.XPATH, xpath):
            try:
                if el.is_displayed():
                    self._js_click(el)
                    return True
            except Exception:
                continue
        return False

    def _click_then_type(self, el: Any, value: str) -> None:
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el
        )
        self._pause(0.06)
        try:
            el.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", el)
        self._pause(0.04)
        try:
            el.send_keys(Keys.CONTROL, "a")
            el.send_keys(Keys.DELETE)
            el.send_keys(value or "")
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
                value or "",
            )

    def _js_click(self, el: Any) -> None:
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el
        )
        self._pause(0.06)
        try:
            el.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", el)


def main() -> None:
    """Prueba manual: un pedido de ejemplo (dry-run por defecto)."""
    import argparse

    p = argparse.ArgumentParser(description="Bot Forza Delivery (desde forza.side)")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--confirm", action="store_true", help="Confirmar guía (sin dry-run)")
    args = p.parse_args()

    bot = ForzaBot(headless=args.headless, dry_run=not args.confirm)
    try:
        bot.login()
        bot.go_crear_guias()
        demo = Pedido(
            nombre="Prueba Forza",
            telefono="70000000",
            direccion="Parque central",
            referencia="Centro",
            producto="Producto demo",
            precio="32",
            peso="1",
            fecha_registro="",
            fecha_entrega="",
            notas="Contactar al cliente para coordinar la entrega",
            departamento="Morazán",
            municipio="Corinto",
            payment_type="Efectivo",
            fila=2,
            colonia="Corinto",
        )
        bot.procesar_pedido(demo, 1, 1)
        _log("Listo.")
        if not args.headless:
            time.sleep(3)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
