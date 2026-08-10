# -*- coding: utf-8 -*-
"""Tests basicos de parser e ubicaciones."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ubicaciones import infer_location, locations_for_ui
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


def test_delivery_sunday_rule():
    # Viernes + 2 = domingo -> lunes
    assert default_delivery_date(date(2026, 8, 14)) == "2026-08-17"


def test_natural_manana():
    d = parse_natural_delivery_date("entrega manana", date(2026, 8, 10))
    assert d == "2026-08-11"


if __name__ == "__main__":
    test_quetzaltepeque()
    test_san_pedro_masahuat()
    test_locations_ui_has_14_depts()
    test_parse_single_line_order()
    test_delivery_sunday_rule()
    test_natural_manana()
    print("OK all tests")
