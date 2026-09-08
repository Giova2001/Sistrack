# -*- coding: utf-8 -*-
"""Tests basicos de parser e ubicaciones."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sistrack.ubicaciones import infer_location, locations_for_ui
from web.parser import parse_order_text
from web.store import default_delivery_date, parse_natural_delivery_date


def test_quetzaltepeque():
    dept, muni = infer_location("Quetzaltepeque, La Libertad", "")
    assert dept == "La Libertad"
    assert "QUEZALTEPEQUE" in muni


def test_san_pedro_masahuat():
    dept, muni = infer_location(
        "Calle hacia playa, San Pedro Masahuat, La Paz", "Frente a la bomba"
    )
    assert dept == "La Paz"
    assert "MASAHUAT" in muni


def test_locations_ui_has_14_depts():
    data = locations_for_ui()
    assert len(data["departments"]) == 14
    assert "ZACATECOLUCA" in data["by_department"]["La Paz"]


def test_parse_single_line_order():
    s = (
        "1. Edwin Milton Hernandez 75881415 Calle hacia playa Las Hojas, "
        "El Achiotal- Frente a la bomba de agua, San Pedro Masahuat, La Paz "
        "-Promocion lente aviador negro y Wood negro Total $20 "
        "Contactar al cliente para coordinar a la entrega"
    )
    recs = parse_order_text(s, default_delivery="2026-08-12")
    assert len(recs) == 1
    r = recs[0]
    assert "Edwin" in r["nombre"]
    assert r["telefono"] == "75881415"
    assert r["departamento"] == "La Paz"
    assert "MASAHUAT" in r["municipio"]
    assert "Achiotal" in r["direccion"]
    assert "bomba" in r["punto_referencia"].lower()
    assert "Promocion" in r["producto"] or "promocion" in r["producto"].lower()
    assert r["precio"] == "20"
    # El telefono no debe contaminar otros campos
    assert "7588" not in r["nombre"]
    assert "7588" not in r["direccion"]
    assert "7588" not in (r["producto"] or "")


def test_phone_glued_to_name():
    s = (
        "1. Juan Perez78561234\n"
        "Residencial Las Flores pasaje 3, Santa Tecla, La Libertad\n"
        "Reloj Seiko\n"
        "Total $55"
    )
    r = parse_order_text(s)[0]
    assert r["nombre"] == "Juan Perez"
    assert r["telefono"] == "78561234"
    assert "7856" not in r["nombre"]
    assert "Santa Tecla" in r["municipio"] or "SANTA TECLA" in r["municipio"]


def test_phone_inside_address():
    s = (
        "Ana Martinez\n"
        "Colonia Centro 7012 3456 Soyapango\n"
        "Promocion 2x1\n"
        "$30"
    )
    r = parse_order_text(s)[0]
    assert r["nombre"] == "Ana Martinez"
    assert r["telefono"] == "70123456"
    assert "Colonia Centro" in r["direccion"]
    assert "7012" not in r["direccion"]
    assert "SOYAPANGO" in r["municipio"]


def test_phone_with_country_code_not_in_name():
    s = (
        "Carlos Rivera 503 7890-1234 Calle Principal #12 Mejicanos "
        "San Salvador wood classic $40"
    )
    r = parse_order_text(s)[0]
    assert "Carlos" in r["nombre"]
    assert "503" not in r["nombre"]
    assert r["telefono"] == "78901234"
    assert "Calle Principal" in r["direccion"]


def test_second_phone_as_emergency():
    s = (
        "Pedro Gomez 78561234 70123456 Colonia San Benito San Salvador casio $50"
    )
    r = parse_order_text(s)[0]
    assert r["telefono"] == "78561234"
    assert r["numero_de_emergencia"] == "70123456"
    assert "7012" not in r["direccion"]
    assert "Colonia San Benito" in r["direccion"]


def test_delivery_sunday_rule():
    # Viernes + 2 = domingo -> lunes
    assert default_delivery_date(date(2026, 8, 14)) == "2026-08-17"


def test_natural_manana():
    d = parse_natural_delivery_date("entrega manana", date(2026, 8, 10))
    assert d == "2026-08-11"


def test_forza_decipher_el_arenal():
    from forza.ubicaciones_forza import load_forza_catalog, best_catalog_match_from_text
    from web.parser import parse_order_text

    load_forza_catalog.cache_clear()
    hit = best_catalog_match_from_text(
        "Colonia El Arenal pasaje 2, Ciudad Delgado, San Salvador"
    )
    assert hit is not None
    assert "ARENAL" in hit.colonia.upper()
    assert "Delgado" in hit.municipio

    s = (
        "Maria Lopez 78561234 Colonia El Arenal casa 5, Ciudad Delgado, "
        "San Salvador - Reloj $25 Contactar al cliente"
    )
    r = parse_order_text(s, platform="forza")[0]
    assert "ARENAL" in (r.get("colonia") or "").upper()
    assert "Delgado" in (r.get("municipio") or "")
    assert "ARENAL" in (r.get("forza_label") or "").upper()


def test_forza_decipher_ashapuco():
    from forza.ubicaciones_forza import load_forza_catalog, best_catalog_match_from_text

    load_forza_catalog.cache_clear()
    hit = best_catalog_match_from_text("Canton Ashapuco casa 10, Ahuachapan")
    assert hit is not None
    assert "ASHAPUCO" in hit.colonia.upper()


if __name__ == "__main__":
    test_quetzaltepeque()
    test_san_pedro_masahuat()
    test_locations_ui_has_14_depts()
    test_parse_single_line_order()
    test_phone_glued_to_name()
    test_phone_inside_address()
    test_phone_with_country_code_not_in_name()
    test_second_phone_as_emergency()
    test_delivery_sunday_rule()
    test_natural_manana()
    test_forza_decipher_el_arenal()
    test_forza_decipher_ashapuco()
    print("OK all tests")
