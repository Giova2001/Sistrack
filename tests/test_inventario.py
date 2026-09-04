# -*- coding: utf-8 -*-
"""Tests de la lógica de inventario (portada del módulo Laravel)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import web.inventario as inv


def _reset(tmp_path: Path) -> None:
    inv.INVENTARIO_PATH = tmp_path / "inventario.json"
    if inv.INVENTARIO_PATH.exists():
        inv.INVENTARIO_PATH.unlink()


def test_entrada_salida_y_stock_insuficiente(tmp_path: Path):
    _reset(tmp_path)
    cat = inv.crear_categoria("Relojes")
    prod = inv.crear_producto({
        "nombre": "Casio MTP",
        "id_categoria": cat["id"],
        "tipo_inventario": "producto_base",
        "precio_unitario": 25,
        "stock_actual": 10,
        "stock_minimo": 3,
    })
    assert prod["stock_actual"] == 10
    assert prod["stock_disponible"] == 10

    inv.registrar_entrada(None, prod["id"], 5, costo_unitario=8, motivo="Compra")
    snap = inv.snapshot()
    p = next(x for x in snap["productos"] if x["id"] == prod["id"])
    assert p["stock_actual"] == 15

    inv.registrar_salida(prod["id"], 4, motivo="Venta")
    snap = inv.snapshot()
    p = next(x for x in snap["productos"] if x["id"] == prod["id"])
    assert p["stock_actual"] == 11

    try:
        inv.registrar_salida(prod["id"], 999)
        assert False, "debia fallar por stock insuficiente"
    except inv.InventarioError as exc:
        assert "insuficiente" in str(exc).lower()


def test_ajuste_y_no_borrar_con_stock(tmp_path: Path):
    _reset(tmp_path)
    cat = inv.crear_categoria("Lentes")
    prod = inv.crear_producto({
        "nombre": "Ray-Ban",
        "id_categoria": cat["id"],
        "stock_actual": 2,
    })
    inv.ajustar_stock(None, prod["id"], 7, motivo="Conteo físico")
    p = inv.kardex(prod["id"])["producto"]
    assert p["stock_actual"] == 7
    tipos = [m["tipo_movimiento"] for m in inv.kardex(prod["id"])["movimientos"]]
    assert "Entrada" in tipos
    assert "Ajuste" in tipos

    try:
        inv.eliminar_producto(prod["id"])
        assert False, "debia impedir borrar con stock"
    except inv.InventarioError as exc:
        assert "stock" in str(exc).lower()


def test_soft_delete_con_kardex_y_categoria_protegida(tmp_path: Path):
    _reset(tmp_path)
    cat = inv.crear_categoria("Kits")
    prod = inv.crear_producto({
        "nombre": "Kit 2x1",
        "id_categoria": cat["id"],
        "stock_actual": 1,
    })
    inv.registrar_salida(prod["id"], 1)
    res = inv.eliminar_producto(prod["id"])
    assert res["soft_delete"] is True
    p = next(x for x in inv.snapshot()["productos"] if x["id"] == prod["id"])
    assert p["activo"] is False

    try:
        inv.eliminar_categoria(cat["id"])
        assert False, "debia impedir borrar categoria con productos"
    except inv.InventarioError:
        pass


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        test_entrada_salida_y_stock_insuficiente(p)
        test_ajuste_y_no_borrar_con_stock(p)
        test_soft_delete_con_kardex_y_categoria_protegida(p)
        print("ok")
