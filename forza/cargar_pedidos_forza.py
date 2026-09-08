# -*- coding: utf-8 -*-
"""
Carga de guías en Forza Delivery Express (portal corporativo).

Flujo real de la UI (capturas / Crear Guías):

  Login corporativo (El Salvador)
  → Crear Guías  (o SOLICITAR SERVICIO)
  → Pestaña ENVÍO
       1. Destino = Manual
       2. Poblado, municipio, departamento
       3. Tipo paquete = Caja
       4. Frágil = sí (por defecto)
       5. Descripción = nombre del producto
       6. CALCULAR
       7. SELECCIONAR  Estándar  ó  C.O.D.
  → Pestaña DETALLES (Destinatario)
       8. Casa / Oficina
       9. ¿Quién recibe?
      10. Nombre de contacto
      11. Teléfono
      12. Dirección en destinatario
      13. Indicaciones para entrega
      14. Número de referencia
      15. SIGUIENTE
  → Pestaña COD & SEGURO
      16. COD on/off + monto (si aplica)
      17. Seguro adicional = off
      18. Confirmar / finalizar
  → Volver a Crear Guías

Nota: peso y dimensiones se dejan en los valores por defecto de Forza (no se tocan).
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

FORZA_CODIGO = os.getenv("FORZA_CODIGO", "").strip()
FORZA_USUARIO = os.getenv("FORZA_USUARIO", "").strip()
FORZA_PASSWORD = os.getenv("FORZA_PASSWORD", "")

# Valores por defecto según capturas de la UI
DEFAULT_PESO_LBS = "1"
DEFAULT_LARGO_CM = "10"
DEFAULT_ANCHO_CM = "20"
DEFAULT_ALTO_CM = "20"


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFD", text or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _pedido_es_cod(pedido: Pedido) -> bool:
    """True si Forza debe cobrar (Servicio C.O.D.)."""
    pagado = str(getattr(pedido, "pagado", "") or "").strip().lower()
    if pagado in ("si", "sí", "yes", "true", "1"):
        return False
    pay = _fold(str(pedido.payment_type or ""))
    notas = _fold(str(pedido.notas or ""))
    if "transfer" in pay or "pagado" in notas:
        return False
    if "efectivo" in pay or "cod" in pay or "contra" in pay:
        return True
    # Por defecto: cobro contra entrega si hay precio > 0
    try:
        return float(re.sub(r"[^\d.]", "", str(pedido.precio or "0")) or "0") > 0
    except ValueError:
        return True


class ForzaBot:
    def __init__(self, headless: bool = False, dry_run: bool = False):
        self.dry_run = dry_run
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )
        self.wait = WebDriverWait(self.driver, 30)

    def quit(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Login / navegación
    # ------------------------------------------------------------------

    def login(
        self,
        codigo: str | None = None,
        usuario: str | None = None,
        password: str | None = None,
    ) -> None:
        code = (codigo or FORZA_CODIGO or "").strip()
        user = (usuario or FORZA_USUARIO or "").strip()
        pwd = (password if password is not None else FORZA_PASSWORD) or ""
        if not code or not user or not pwd:
            raise RuntimeError(
                "Faltan credenciales de Forza (código, usuario y contraseña)"
            )
        self.driver.get(LOGIN_URL)
        time.sleep(1.0)
        self._select_country_el_salvador()
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "form")))
        time.sleep(0.4)

        inputs = self._visible_inputs()
        if len(inputs) < 3:
            raise RuntimeError("No se encontraron los 3 campos de login de Forza")
        self._click_then_type(inputs[0], code)
        self._click_then_type(inputs[1], user)
        self._click_then_type(inputs[2], pwd)

        if not self._click_by_text(
            ["INICIAR SESIÓN", "INICIAR SESION", "Iniciar sesión"],
            tags=("ion-button", "button"),
        ):
            raise RuntimeError("No se encontró el botón Iniciar sesión")

        self.wait.until(
            EC.any_of(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(.,'Crear Guías') or contains(.,'Mis Envíos')]")
                ),
                EC.presence_of_element_located((By.CSS_SELECTOR, "ion-menu, app-tabs")),
            )
        )
        time.sleep(1.0)

    def go_crear_guias(self) -> None:
        """Desde Mis Envíos / menú → Crear Guías."""
        self._dismiss_overlays()
        self._open_menu_if_needed()

        if self._click_xpath(
            "//ion-item[contains(.,'Crear Guías') or contains(.,'Crear Guias')]"
            " | //*[normalize-space()='Crear Guías' or normalize-space()='Crear Guias']"
        ):
            time.sleep(1.0)
        elif self._click_by_text(
            ["SOLICITAR SERVICIO", "Solicitar servicio"],
            tags=("ion-button", "button", "a"),
        ):
            time.sleep(1.0)
        else:
            self.driver.get(f"{BASE_URL}/tabs/cotizador-corporativo")
            time.sleep(1.0)

        # Asegurar pestaña Envío
        self._click_by_text(["Envío", "ENVIO"], tags=("*",), exact=False)
        time.sleep(0.6)
        self.wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//*[contains(.,'Poblado') or contains(.,'Destino') or contains(.,'Paquete')]",
                )
            )
        )

    def procesar_pedido(self, pedido: Pedido, index: int, total: int) -> None:
        print(
            f"[Forza {index}/{total}] Fila {pedido.fila}: {pedido.nombre} | "
            f"{pedido.departamento}/{pedido.municipio or '?'} | ${pedido.precio}"
        )
        self.go_crear_guias()

        use_cod = _pedido_es_cod(pedido)
        print(f"  Servicio: {'C.O.D.' if use_cod else 'Estándar'}")

        # --- ENVÍO ---
        self._step_destino_manual(pedido)          # 1-2
        self._step_paquete(pedido)                 # 3-4 (sin peso/dimensiones)
        self._step_descripcion(pedido)             # 5
        self._dismiss_overlays()
        self._step_calcular()                      # 6
        self._step_seleccionar_servicio(use_cod)   # 7

        # --- DETALLES ---
        self._step_detalles_destinatario(pedido, index)  # 8-14
        self._step_siguiente_detalles()                  # 15

        # --- COD & SEGURO ---
        self._step_cod_y_seguro(pedido, use_cod)   # 16-17

        if self.dry_run:
            print("  Dry-run: formulario llenado, no se confirma.")
            self.go_crear_guias()
            return

        self._step_confirmar()                     # 18
        time.sleep(1.2)
        self.go_crear_guias()
        print(f"  OK Forza -> {(pedido.producto or '')[:60]}")

    # ------------------------------------------------------------------
    # Pasos ENVÍO
    # ------------------------------------------------------------------

    def _step_destino_manual(self, pedido: Pedido) -> None:
        print("  1) Destino = Manual")
        self._click_radio_or_label("Manual")
        time.sleep(0.3)

        ubic = forza_location_from_pedido(
            direccion=str(pedido.direccion or ""),
            referencia=str(pedido.referencia or ""),
            departamento=str(pedido.departamento or ""),
            municipio=str(pedido.municipio or ""),
            colonia=str(getattr(pedido, "colonia", "") or ""),
        )
        print(f"  2) Poblado: {ubic.label}")
        self._abrir_selector_poblado()
        self._buscar_y_elegir_poblado(ubic)
        time.sleep(0.5)

    def _abrir_selector_poblado(self) -> None:
        for sel in (
            "app-select ion-icon[name='caret-down-outline']",
            ".form-control ion-icon",
            "app-select ion-input",
            "app-select",
        ):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        self._js_click(el)
                        time.sleep(0.5)
                        if self.driver.find_elements(
                            By.CSS_SELECTOR, "ion-modal, input.searchbar-input"
                        ):
                            return
                except Exception:
                    continue
        # click por texto del placeholder
        if self._click_xpath(
            "//*[contains(.,'Selecciona un poblado') or contains(.,'Poblado, municipio')]"
        ):
            time.sleep(0.5)
            return
        raise RuntimeError("No se pudo abrir el selector de poblado")

    def _buscar_y_elegir_poblado(self, ubic: ForzaUbicacion) -> None:
        self.wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "ion-modal, input.searchbar-input, input[type='search']")
            )
        )
        search = self.wait.until(
            EC.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    "ion-modal input.searchbar-input, ion-searchbar input, input[type='search']",
                )
            )
        )
        item = None
        last = ""
        chosen_label = ""
        for query in ubic.search_queries():
            last = query
            self._click_then_type(search, query)
            time.sleep(0.9)
            item = self._pick_location_item(ubic)
            if item is not None:
                chosen_label = (item.text or "").strip()
                break
        if item is None:
            raise RuntimeError(
                f"No hay coincidencia de poblado Forza para '{ubic.label}' "
                f"(última búsqueda: '{last}')"
            )
        try:
            h2 = item.find_elements(By.CSS_SELECTOR, "h2, ion-label")
            self._js_click(h2[0] if h2 else item)
        except Exception:
            self._js_click(item)
        time.sleep(0.7)
        self._assert_poblado_seleccionado(ubic, chosen_label)

    def _selected_poblado_text(self) -> str:
        for sel in (
            "app-select ion-input input",
            "app-select input",
            ".form-control ion-input input",
            "app-select",
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

    def _assert_poblado_seleccionado(self, ubic: ForzaUbicacion, fallback_label: str = "") -> None:
        selected = self._selected_poblado_text() or fallback_label
        score = score_forza_label(selected, ubic)
        print(f"     seleccionado: {selected[:80]} (score={score})")
        if score < 6:
            raise RuntimeError(
                f"Poblado incorrecto o no aplicado: '{selected}' "
                f"(se esperaba algo como '{ubic.label}')"
            )

    def _pick_location_item(self, ubic: ForzaUbicacion) -> Any:
        items = self.driver.find_elements(
            By.CSS_SELECTOR, "ion-modal ion-item, ion-modal ion-list ion-item"
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
        # Por score directo
        ranked = sorted(
            visible,
            key=lambda el: score_forza_label(el.text or "", ubic),
            reverse=True,
        )
        if score_forza_label(ranked[0].text or "", ubic) >= 6:
            return ranked[0]
        return None

    def _step_paquete(self, pedido: Pedido) -> None:
        """Caja + Frágil. Peso/dimensiones: se omiten (defaults de Forza)."""
        print("  3) Tipo paquete = Caja (sin tocar peso/dimensiones)")
        self._click_radio_or_label("Caja")
        time.sleep(0.2)
        print("  4) Frágil = sí")
        self._set_toggle_near(
            ["¿Es Frágil?", "Es Frágil?", "Frágil", "Fragil"],
            on=True,
        )
        time.sleep(0.2)

    def _step_descripcion(self, pedido: Pedido) -> None:
        producto = (pedido.producto or "Producto").strip()
        # Solo el nombre del producto (sin prefijo "1 paquete")
        desc = producto
        print(f"  5) Descripción: {desc[:60]}")
        filled = self._fill_by_label(
            [
                "Descripción general del envío",
                "Descripcion general del envio",
                "Descripción",
                "Descripcion",
            ],
            desc,
        )
        if not filled:
            areas = [
                el
                for el in self.driver.find_elements(By.CSS_SELECTOR, "textarea")
                if el.is_displayed()
            ]
            if areas:
                self._click_then_type(areas[0], desc)

    def _step_calcular(self) -> None:
        print("  6) CALCULAR")
        # Asegurar que el poblado quedó aplicado antes de calcular
        time.sleep(0.3)
        clicked = self._click_by_text(
            ["CALCULAR", "Calcular"], tags=("ion-button", "button")
        )
        if not clicked:
            try:
                self.driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);"
                )
            except Exception:
                pass
            time.sleep(0.4)
            clicked = self._click_by_text(
                ["CALCULAR", "Calcular"], tags=("ion-button", "button", "*")
            )
        if not clicked:
            for xp in (
                "//ion-button[contains(translate(.,'calcular','CALCULAR'),'CALCULAR')]",
                "//button[contains(translate(.,'calcular','CALCULAR'),'CALCULAR')]",
                "//*[self::ion-button or self::button]"
                "[contains(translate(normalize-space(.),'calcular','CALCULAR'),'CALCULAR')]",
            ):
                if self._click_xpath(xp):
                    clicked = True
                    break
        if not clicked:
            sel = self._selected_poblado_text()
            raise RuntimeError(
                "No se encontró el botón CALCULAR"
                + (f" (poblado actual: '{sel}')" if sel else " (revisa poblado seleccionado)")
            )
        time.sleep(1.6)
        self.wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//*[contains(.,'SELECCIONAR') or contains(.,'Servicio Estándar') "
                    "or contains(.,'Servicio C.O.D') or contains(.,'Servicio COD')]",
                )
            )
        )

    def _step_seleccionar_servicio(self, use_cod: bool) -> None:
        label = "C.O.D" if use_cod else "Estándar"
        print(f"  8) SELECCIONAR servicio {label}")
        # Buscar tarjeta por título y su botón SELECCIONAR
        cards = self.driver.find_elements(
            By.XPATH,
            "//*[contains(@class,'card') or self::ion-card or self::div]"
            "[.//text()[contains(.,'Servicio')]]",
        )
        target_words = ("c.o.d", "cod") if use_cod else ("estandar", "estándar", "estandar")
        for card in cards:
            try:
                txt = _fold(card.text or "")
                if not txt or "seleccionar" not in txt:
                    continue
                if any(w in txt for w in target_words):
                    btn = card.find_elements(
                        By.XPATH,
                        ".//ion-button[contains(.,'SELECCIONAR')] | "
                        ".//button[contains(.,'SELECCIONAR')] | "
                        ".//*[contains(.,'SELECCIONAR')]",
                    )
                    if btn:
                        self._js_click(btn[0])
                        time.sleep(1.2)
                        return
            except Exception:
                continue

        # Fallback: 1er SELECCIONAR = estándar, 2º = COD
        buttons = [
            el
            for el in self.driver.find_elements(
                By.XPATH,
                "//ion-button[contains(.,'SELECCIONAR')] | //button[contains(.,'SELECCIONAR')]",
            )
            if el.is_displayed()
        ]
        if not buttons:
            raise RuntimeError("No hay botones SELECCIONAR de servicio")
        idx = 1 if use_cod and len(buttons) > 1 else 0
        self._js_click(buttons[idx])
        time.sleep(1.2)

    # ------------------------------------------------------------------
    # Pasos DETALLES
    # ------------------------------------------------------------------

    def _step_detalles_destinatario(self, pedido: Pedido, index: int) -> None:
        # Asegurar pestaña Detalles
        self._click_by_text(["Detalles", "DETALLES"], tags=("*",), exact=False)
        time.sleep(0.8)
        self.wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//*[contains(.,'Destinatario') or contains(.,'Quién recibe')]")
            )
        )

        phone = clean_phone(pedido.telefono)
        producto = (pedido.producto or "Producto").strip()[:80]
        nombre = f"{index} {pedido.nombre}".strip()
        direccion = (pedido.direccion or "").strip()
        obs = build_observations(pedido) or (
            "contactar al cliente para coordinar la entrega"
        )

        print("  9) Entrega = Casa")
        self._click_radio_or_label("Casa")

        print(f"  10) Quién recibe: {producto[:50]}")
        self._fill_by_label(
            ["Quién recibe", "Quien recibe", "¿Quién recibe?"],
            producto,
            required=False,
        )

        print(f"  11) Nombre contacto: {nombre}")
        self._fill_destinatario_nombre(nombre)

        print(f"  12) Teléfono: {phone}")
        self._fill_destinatario_telefono(phone)

        print(f"  13) Dirección destinatario")
        self._fill_by_label(
            ["Dirección en destinatario", "Direccion en destinatario", "dirección completa"],
            direccion or "Sin dirección",
        )

        print(f"  14) Indicaciones entrega")
        self._fill_by_label(
            [
                "Indicaciones para entrega",
                "contactar al cliente para coordinar la entrega",
            ],
            obs,
            required=False,
        )

        print(f"  15) Número de referencia: {index}")
        self._fill_by_label(
            ["Número de referencia", "Numero de referencia"],
            str(index),
            required=False,
        )

    def _fill_destinatario_nombre(self, nombre: str) -> None:
        # Hay dos "Nombre de contacto" (remitente y destinatario); usar el de la columna derecha
        labels = self.driver.find_elements(
            By.XPATH,
            "//*[contains(.,'Nombre de contacto')]",
        )
        # Buscar inputs cerca del bloque Destinatario
        dest = self.driver.find_elements(
            By.XPATH,
            "//*[contains(.,'Destinatario')]/following::input[not(@type='hidden')]",
        )
        visibles = [el for el in dest if el.is_displayed()]
        if len(visibles) >= 2:
            # suele ser: quien recibe, nombre, telefono...
            # Intentar el input cuyo label cercano diga Nombre
            for el in visibles[:6]:
                try:
                    parent_txt = _fold(
                        el.find_element(By.XPATH, "./ancestor::*[self::app-input or self::div][1]").text
                    )
                except Exception:
                    parent_txt = ""
                if "nombre" in parent_txt and "quien" not in parent_txt and "recibe" not in parent_txt:
                    self._click_then_type(el, nombre)
                    return
            # fallback: segundo input del bloque
            if len(visibles) >= 2:
                self._click_then_type(visibles[1], nombre)
                return
        self._fill_by_label(["Nombre de contacto"], nombre)

    def _fill_destinatario_telefono(self, phone: str) -> None:
        dest = self.driver.find_elements(
            By.XPATH,
            "//*[contains(.,'Destinatario')]/following::input[not(@type='hidden')]",
        )
        visibles = [el for el in dest if el.is_displayed()]
        for el in visibles:
            try:
                parent_txt = _fold(
                    el.find_element(By.XPATH, "./ancestor::*[self::app-input or self::div][1]").text
                )
            except Exception:
                parent_txt = ""
            if "telefono" in parent_txt or "teléfono" in parent_txt:
                self._click_then_type(el, phone)
                return
        # fallback: input tipo tel
        tels = [
            el
            for el in self.driver.find_elements(
                By.CSS_SELECTOR, "input[type='tel'], input[inputmode='tel']"
            )
            if el.is_displayed()
        ]
        if tels:
            self._click_then_type(tels[-1], phone)
            return
        self._fill_by_label(["Teléfono", "Telefono"], phone)

    def _step_siguiente_detalles(self) -> None:
        print("  16) SIGUIENTE")
        if not self._click_by_text(
            ["SIGUIENTE", "Siguiente"], tags=("ion-button", "button", "a")
        ):
            raise RuntimeError("No se encontró SIGUIENTE en Detalles")
        time.sleep(1.2)

    # ------------------------------------------------------------------
    # Pasos COD & SEGURO
    # ------------------------------------------------------------------

    def _step_cod_y_seguro(self, pedido: Pedido, use_cod: bool) -> None:
        self._click_by_text(
            ["COD & Seguro", "COD", "Seguro"], tags=("*",), exact=False
        )
        time.sleep(0.8)

        if use_cod:
            print("  17) COD = ON + monto")
            self._set_toggle_near(
                ["Collect On Delivery", "servicio Collect On Delivery", "COD"],
                on=True,
            )
            time.sleep(0.4)
            self._click_radio_or_label("Mis favoritos")
            monto = re.sub(r"[^\d.]", "", str(pedido.precio or "0")) or "0"
            self._fill_by_label(
                ["Monto a cobrar", "Monto"],
                monto,
                required=False,
            )
        else:
            print("  17) COD = OFF (Estándar)")
            self._set_toggle_near(
                ["Collect On Delivery", "servicio Collect On Delivery", "COD"],
                on=False,
            )

        print("  18) Seguro adicional = OFF")
        self._set_toggle_near(
            ["seguro adicional", "Desea seguro adicional"],
            on=False,
        )
        time.sleep(0.3)

    def _step_confirmar(self) -> None:
        print("  19) Confirmar guía")
        labels = [
            "CONFIRMAR",
            "FINALIZAR",
            "CREAR GUÍA",
            "CREAR GUIA",
            "PAGAR",
            "ACEPTAR",
            "SIGUIENTE",
        ]
        if not self._click_by_text(labels, tags=("ion-button", "button")):
            # botón secondary / naranja típico
            btns = [
                el
                for el in self.driver.find_elements(By.CSS_SELECTOR, "ion-button")
                if el.is_displayed()
            ]
            skip = ("regresar", "cancelar", "atrás", "atras")
            usable = [
                b for b in btns if not any(s in _fold(b.text or "") for s in skip)
            ]
            if not usable:
                raise RuntimeError("No se encontró botón de confirmar en Forza")
            self._js_click(usable[-1])
        time.sleep(1.2)
        self._click_by_text(["OK", "ACEPTAR", "CERRAR"], tags=("ion-button", "button"))

    # ------------------------------------------------------------------
    # Helpers UI
    # ------------------------------------------------------------------

    def _select_country_el_salvador(self) -> None:
        for xpath in (
            "//span[contains(.,'El Salvador')]",
            "//*[contains(@class,'d-flex') and contains(.,'El Salvador')]",
        ):
            if self._click_xpath(xpath):
                time.sleep(0.5)
                return

    def _open_menu_if_needed(self) -> None:
        items = self.driver.find_elements(
            By.XPATH,
            "//ion-item[contains(.,'Crear Guías') or contains(.,'Crear Guias')]",
        )
        if any(el.is_displayed() for el in items):
            return
        for sel in ("ion-menu-button", "ion-buttons ion-menu-button", "[menuToggle]"):
            for b in self.driver.find_elements(By.CSS_SELECTOR, sel):
                if b.is_displayed():
                    self._js_click(b)
                    time.sleep(0.5)
                    return

    def _visible_inputs(self) -> list:
        els = self.driver.find_elements(
            By.CSS_SELECTOR,
            "input.native-input, input[type='text'], input[type='password'], "
            "input[type='number'], input[type='tel'], textarea.native-textarea, textarea",
        )
        return [el for el in els if el.is_displayed()]

    def _click_radio_or_label(self, text: str) -> bool:
        t = text.strip()
        xpaths = (
            f"//ion-radio[contains(.,'{t}')]",
            f"//ion-item[contains(.,'{t}')]//ion-radio",
            f"//ion-label[contains(.,'{t}')]",
            f"//*[normalize-space()='{t}']",
            f"//*[contains(.,'{t}') and (self::span or self::div or self::label)]",
        )
        for xp in xpaths:
            if self._click_xpath(xp):
                time.sleep(0.2)
                return True
        return False

    def _fill_by_label(
        self, labels: list[str], value: str, required: bool = True
    ) -> bool:
        for lab in labels:
            # input dentro de contenedor que menciona el label
            xpaths = (
                f"//*[contains(.,'{lab}')]/following::input[1]",
                f"//*[contains(.,'{lab}')]/ancestor::app-input[1]//input",
                f"//*[contains(.,'{lab}')]/ancestor::div[1]//input",
                f"//*[contains(.,'{lab}')]/following::textarea[1]",
                f"//*[contains(.,'{lab}')]/ancestor::app-input[1]//textarea",
                f"//label[contains(.,'{lab}')]/following::input[1]",
            )
            for xp in xpaths:
                els = self.driver.find_elements(By.XPATH, xp)
                for el in els:
                    try:
                        if not el.is_displayed():
                            continue
                        self._click_then_type(el, value)
                        return True
                    except Exception:
                        continue
        if required:
            # último recurso: no fallar duro en opcionales
            print(f"    aviso: no se encontró campo para {labels[0]!r}")
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
                    is_on = checked in ("true", "true ")
                    # también class / checked property
                    cls = tg.get_attribute("class") or ""
                    if "toggle-checked" in cls:
                        is_on = True
                    if on != is_on:
                        self._js_click(tg)
                        time.sleep(0.3)
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
            els = self.driver.find_elements(By.CSS_SELECTOR, tag) if tag != "*" else []
            if tag == "*":
                for t in texts:
                    if exact:
                        xp = f"//*[normalize-space()='{t}']"
                    else:
                        xp = f"//*[contains(.,'{t}')]"
                    if self._click_xpath(xp):
                        return True
                continue
            folded = [_fold(t) for t in texts]
            for el in els:
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
        time.sleep(0.08)
        try:
            el.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", el)
        time.sleep(0.05)
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
        time.sleep(0.08)
        try:
            el.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", el)

    def _dismiss_overlays(self) -> None:
        """Cierra alerts de Forza (p. ej. ¿Desea finalizar el proceso?)."""
        # Preferir Aceptar/OK; Cancelar dejaría el flujo a medias
        for label in ("Aceptar", "ACEPTAR", "OK", "Sí", "Si"):
            if self._click_by_text(
                [label], tags=("ion-button", "button", "button.alert-button")
            ):
                time.sleep(0.35)
                return
        # Fallback: cualquier botón visible del alert
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
                        time.sleep(0.35)
                        return
                except Exception:
                    continue
