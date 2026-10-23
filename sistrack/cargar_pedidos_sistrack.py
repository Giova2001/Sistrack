
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
import unicodedata
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

BASE_URL = os.getenv('EXPRESS2_URL', 'https://xpress.facilitoerp.com').rstrip('/')
DEFAULT_NOTES = 'Contactar al cliente para coordinar la entrega'

def norm(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value or '').lower()) if not unicodedata.combining(c)).strip()

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

class Express2Bot:
    def __init__(self, headless=False, timeout=25):
        options = webdriver.ChromeOptions()
        if headless: options.add_argument('--headless=new')
        options.add_argument('--start-maximized')
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, timeout)

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

    def open_recipient(self):
        if self.driver.find_elements(By.ID, 'legacy-recipient-name') and self.driver.find_element(By.ID, 'legacy-recipient-name').is_displayed():
            return
        # El .side abre el formulario mediante .ml-1; usarlo solo si es necesario.
        self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '.ml-1'))).click()
        self.find((By.ID, 'legacy-recipient-name'))

    def select_option(self, select_el, label):
        select = Select(select_el)
        target = norm(label)
        for option in select.options:
            if norm(option.text) == target:
                select.select_by_visible_text(option.text)
                return option.text
        raise ValueError(f'No existe la opción {label!r} en {[o.text for o in select.options]}')

    def select_location(self, department, municipality):
        # Selectores tomados del flujo Selenium IDE. Pueden requerir ajuste si cambia la UI.
        dept_sel = self.find((By.CSS_SELECTOR, '.mb-8:nth-child(2) > .rounded-lg > .flex:nth-child(3) .h-9'))
        self.select_option(dept_sel, department)
