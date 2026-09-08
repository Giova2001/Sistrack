# -*- coding: utf-8 -*-
"""
Carga de guías en Forza Delivery Express (portal corporativo).
Flujo basado en Selenium IDE: forza.side
  https://portal.forzadelivery.com/login-corporate
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

from cargar_pedidos_sistrack import Pedido, build_observations, clean_phone

BASE_URL = "https://portal.forzadelivery.com/login-corporate"
LOGIN_URL = f"{BASE_URL}/login-corporate"

FORZA_CODIGO = os.getenv("FORZA_CODIGO", "").strip()
FORZA_USUARIO = os.getenv("FORZA_USUARIO", "").strip()
FORZA_PASSWORD = os.getenv("FORZA_PASSWORD", "")

DEFAULT_WEIGHT = 1


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFD", text or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


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
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "form")))
        time.sleep(0.8)
        inputs = self._visible_native_inputs()
        if len(inputs) < 3:
            raise RuntimeError("No se encontraron los 3 campos de login de Forza")
        self._set_native(inputs[0], code)
        self._set_native(inputs[1], user)
        self._set_native(inputs[2], pwd)
        self._click_ion_button(
            ["INICIAR SESIÓN", "INICIAR SESION", "Iniciar sesión"]
        )
        self.wait.until(
            EC.any_of(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(.,'Crear Guías') or contains(.,'Crear Guias')]")
                ),
                EC.presence_of_element_located((By.CSS_SELECTOR, "ion-menu, app-tabs")),
            )
        )
        time.sleep(1.2)

    def go_crear_guias(self) -> None:
        self._dismiss_overlays()
        self._open_menu_if_needed()
        clicked = self._click_xpath(
            "//ion-item[contains(.,'Crear Guías') or contains(.,'Crear Guias')]"
        )
        if not clicked:
            self.driver.get(f"{BASE_URL}/tabs/cotizador-corporativo")
        self.wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "app-cotizador-corporativo, ion-content")
            )
        )
        time.sleep(0.8)

    def procesar_pedido(self, pedido: Pedido, index: int, total: int) -> None:
        print(
            f"[Forza {index}/{total}] Fila {pedido.fila}: {pedido.nombre} | "
            f"{pedido.departamento}/{pedido.municipio or '?'} | ${pedido.precio}"
        )
        if not self._on_cotizador():
            self.go_crear_guias()

        self._seleccionar_destino(pedido)
        self._set_peso(DEFAULT_WEIGHT)
        self._llenar_contenido(pedido)
        self._continuar_hasta("app-envio-corporativo")
        self._seleccionar_tipo_pago(pedido)
        self._llenar_destinatario(pedido, index)
        self._continuar_hasta("app-servicios-corporativo")
        self._llenar_monto(pedido)
        self._continuar_hasta("app-pago-servicio-corporativo", "app-pago-facturacion")
        if self._page_has("app-pago-servicio-corporativo"):
            self._continuar_hasta("app-pago-facturacion")
        if self.dry_run:
            print("  Dry-run: guía llenada, no se confirma el pago.")
            self.go_crear_guias()
            return
        self._confirmar_guia()
        time.sleep(1.2)
        self.go_crear_guias()
        print(f"  OK Forza -> {pedido.producto[:60]}")

    def _on_cotizador(self) -> bool:
        return bool(self.driver.find_elements(By.CSS_SELECTOR, "app-cotizador-corporativo"))

    def _page_has(self, css: str) -> bool:
        return bool(self.driver.find_elements(By.CSS_SELECTOR, css))

    def _seleccionar_destino(self, pedido: Pedido) -> None:
        self._dismiss_overlays()
        opened = False
        for sel in (
            "app-select ion-input input",
            "app-select input.native-input",
            "app-cotizador-corporativo app-select",
        ):
            els = self.driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if not el.is_displayed():
                    continue
                self._js_click(el)
                opened = True
                break
            if opened:
                break
        if not opened:
            raise RuntimeError("No se pudo abrir el selector de municipio en Forza")

        self.wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ion-modal, ion-searchbar"))
        )
        time.sleep(0.4)
        search = self.wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "ion-modal input.searchbar-input, ion-searchbar input")
            )
        )
        query = (pedido.municipio or pedido.departamento or "").strip()
        self._set_native(search, query)
        search.send_keys(Keys.ENTER)
        time.sleep(0.8)

        item = self._pick_location_item(pedido)
        if item is None:
            raise RuntimeError(
                f"No hay coincidencia de destino Forza para "
                f"{pedido.municipio} / {pedido.departamento}"
            )
        self._js_click(item)
        time.sleep(0.6)

    def _pick_location_item(self, pedido: Pedido) -> Any:
        muni = _fold(pedido.municipio or "")
        dept = _fold(pedido.departamento or "")
        items = self.driver.find_elements(
            By.CSS_SELECTOR, "ion-modal ion-item, ion-modal ion-list ion-item"
        )
        visible = [el for el in items if el.is_displayed() and (el.text or "").strip()]
        if not visible:
            return None

        def score(text: str) -> int:
            t = _fold(text)
            s = 0
            if muni and muni in t:
                s += 3
            if dept and dept in t:
                s += 2
            if muni and t.startswith(muni):
                s += 1
            return s

        ranked = sorted(visible, key=lambda el: score(el.text or ""), reverse=True)
        best = ranked[0]
        if score(best.text or "") <= 0:
            # último recurso: primer resultado de la búsqueda
            return visible[0]
        return best

    def _set_peso(self, kilos: float) -> None:
        try:
            self.driver.execute_script(
                """
                const range = document.querySelector('app-cotizador-corporativo ion-range, ion-range');
                if (!range) return;
                const val = arguments[0];
                range.value = val;
                range.dispatchEvent(new CustomEvent('ionInput', {bubbles:true, detail:{value: val}}));
                range.dispatchEvent(new CustomEvent('ionChange', {bubbles:true, detail:{value: val}}));
                """,
                kilos,
            )
            time.sleep(0.2)
        except Exception:
            pass

    def _llenar_contenido(self, pedido: Pedido) -> None:
        obs = build_observations(pedido)
        text = f"{pedido.producto} {obs}".strip()
        areas = self.driver.find_elements(
            By.CSS_SELECTOR, "app-cotizador-corporativo textarea, textarea.native-textarea"
        )
        visible = [el for el in areas if el.is_displayed()]
        if not visible:
            raise RuntimeError("No se encontró el campo de contenido/observaciones en Forza")
        self._set_native(visible[0], text)

    def _seleccionar_tipo_pago(self, pedido: Pedido) -> None:
        paid = str(pedido.payment_type or "").lower()
        notas = (pedido.notas or "").lower()
        is_paid = "transfer" in paid or "pagado" in notas
        labels = (
            ["prepago", "pagado", "ya pagado", "sin cobro"]
            if is_paid
            else ["contra entrega", "cobro", "efectivo", "cod"]
        )
        for lab in labels:
            if self._click_xpath(
                f"//ion-radio[contains(translate(., 'ÁÉÍÓÚáéíóú', 'AEIOUaeiou'), '{lab}')]"
                f" | //ion-item[contains(translate(., 'ÁÉÍÓÚáéíóú', 'AEIOUaeiou'), '{lab}')]//ion-radio"
            ):
                time.sleep(0.3)
                return
        radios = [
            el
            for el in self.driver.find_elements(By.CSS_SELECTOR, "ion-radio")
            if el.is_displayed()
        ]
        if radios:
            # En el .side se elige el segundo radio (cobro típico)
            idx = 0 if is_paid else min(1, len(radios) - 1)
            self._js_click(radios[idx])
            time.sleep(0.3)

    def _llenar_destinatario(self, pedido: Pedido, index: int) -> None:
        self.wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "app-envio-corporativo"))
        )
        time.sleep(0.4)
        inputs = [
            el
            for el in self.driver.find_elements(
                By.CSS_SELECTOR, "app-envio-corporativo input.native-input"
            )
            if el.is_displayed()
        ]
        if len(inputs) < 5:
            raise RuntimeError("Formulario de destinatario Forza incompleto")

        phone = clean_phone(pedido.telefono)
        self._set_native(inputs[0], (pedido.producto or "Producto")[:80])
        self._set_native(inputs[1], pedido.nombre)
        self._set_native(inputs[2], phone)
        if len(inputs) >= 7:
            self._set_native(inputs[5], pedido.direccion)
            self._set_native(inputs[6], pedido.referencia or "Sin referencia")
        elif len(inputs) >= 6:
            self._set_native(inputs[4], pedido.direccion)
            self._set_native(inputs[5], pedido.referencia or "Sin referencia")
        else:
            self._set_native(inputs[min(3, len(inputs) - 1)], pedido.direccion)

        ident = f"Pedido {index} - {phone or pedido.nombre}"
        extra = [
            el
            for el in self.driver.find_elements(
                By.CSS_SELECTOR, "app-envio-corporativo ion-row:nth-child(2) input.native-input"
            )
            if el.is_displayed()
        ]
        if extra:
            self._set_native(extra[0], ident)
        elif len(inputs) >= 7:
            self._set_native(inputs[-1], ident)

    def _llenar_monto(self, pedido: Pedido) -> None:
        precio = re.sub(r"[^\d.]", "", str(pedido.precio or "0")) or "0"
        inputs = [
            el
            for el in self.driver.find_elements(
                By.CSS_SELECTOR, "app-servicios-corporativo input.native-input"
            )
            if el.is_displayed()
        ]
        if inputs:
            self._set_native(inputs[0], precio)

    def _continuar_hasta(self, *css_targets: str, clicks: int = 4) -> None:
        for _ in range(clicks):
            if any(self._page_has(css) for css in css_targets):
                return
            self._click_continue()
            time.sleep(0.9)
        if not any(self._page_has(css) for css in css_targets):
            raise RuntimeError(
                "Forza no avanzó al siguiente paso (" + ", ".join(css_targets) + ")"
            )

    def _click_continue(self) -> None:
        labels = [
            "SIGUIENTE",
            "CONTINUAR",
            "CREAR GUÍA",
            "CREAR GUIA",
            "GUARDAR",
            "ACEPTAR",
            "CONFIRMAR",
        ]
        if self._click_ion_button(labels):
            return
        buttons = [
            el
            for el in self.driver.find_elements(By.CSS_SELECTOR, "ion-button")
            if el.is_displayed()
        ]
        skip = ("cancelar", "atrás", "atras", "volver", "usar otra")
        usable = []
        for el in buttons:
            txt = _fold(el.text or "")
            if any(s in txt for s in skip):
                continue
            usable.append(el)
        if not usable:
            raise RuntimeError("No hay botón de continuar en Forza")
        self._js_click(usable[-1])

    def _confirmar_guia(self) -> None:
        if self._page_has("app-pago-facturacion"):
            if not self._click_ion_button(
                ["CONFIRMAR", "PAGAR", "FINALIZAR", "ACEPTAR", "CREAR"]
            ):
                btns = [
                    el
                    for el in self.driver.find_elements(
                        By.CSS_SELECTOR,
                        "app-pago-facturacion ion-button, ion-button.ion-color-secondary",
                    )
                    if el.is_displayed()
                ]
                if not btns:
                    raise RuntimeError("No se encontró el botón de confirmar guía Forza")
                self._js_click(btns[-1])
        time.sleep(1.0)
        # alertas Ionic de éxito
        self._click_ion_button(["OK", "ACEPTAR", "CERRAR"])

    def _visible_native_inputs(self) -> list:
        els = self.driver.find_elements(
            By.CSS_SELECTOR, "input.native-input, input[type='text'], input[type='password']"
        )
        return [el for el in els if el.is_displayed()]

    def _set_native(self, el: Any, value: str) -> None:
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el
        )
        time.sleep(0.12)
        try:
            el.click()
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
        time.sleep(0.1)
        try:
            el.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", el)

    def _click_xpath(self, xpath: str) -> bool:
        els = self.driver.find_elements(By.XPATH, xpath)
        for el in els:
            try:
                if el.is_displayed():
                    self._js_click(el)
                    return True
            except Exception:
                continue
        return False

    def _click_ion_button(self, labels: list[str]) -> bool:
        buttons = self.driver.find_elements(By.CSS_SELECTOR, "ion-button")
        folded = [_fold(x) for x in labels]
        for el in buttons:
            try:
                if not el.is_displayed():
                    continue
                txt = _fold(el.text or el.get_attribute("innerText") or "")
                if any(lab in txt for lab in folded):
                    self._js_click(el)
                    return True
            except Exception:
                continue
        return False

    def _open_menu_if_needed(self) -> None:
        items = self.driver.find_elements(
            By.XPATH, "//ion-item[contains(.,'Crear Guías') or contains(.,'Crear Guias')]"
        )
        if any(el.is_displayed() for el in items):
            return
        for sel in ("ion-menu-button", "ion-buttons ion-menu-button", "[aria-label='menu']"):
            btns = self.driver.find_elements(By.CSS_SELECTOR, sel)
            for b in btns:
                if b.is_displayed():
                    self._js_click(b)
                    time.sleep(0.5)
                    return

    def _dismiss_overlays(self) -> None:
        for sel in ("ion-alert button", ".alert-button"):
            for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        self._js_click(el)
                except Exception:
                    pass
