# -*- coding: utf-8 -*-
"""Tests de cobros/pagos (portados de CobroController y PedidoSaldoService)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import web.cobros as cob
import web.store as store


def _reset(tmp_path: Path) -> None:
    store.DATA_DIR = tmp_path
    cob.COBROS_PATH = tmp_path / "cobros.json"
    if cob.COBROS_PATH.exists():
        cob.COBROS_PATH.unlink()
    store.save_day(
        "2026-09-02",
        [
            {
                "nombre": "Ana López",
                "precio": "100",
                "pagado": "No",
                "producto": "Reloj",
                "payment_type": "Efectivo",
            },
            {
                "nombre": "Luis Pérez",
                "precio": "50",
                "pagado": "Si",
                "producto": "Lentes",
            },
        ],
        "dept",
    )


def test_saldo_inicial_y_pedido_ya_pagado(tmp_path: Path):
    _reset(tmp_path)
    snap = cob.snapshot()
    ana = next(s for s in snap["saldos"] if s["cliente"] == "Ana López")
    luis = next(s for s in snap["saldos"] if s["cliente"] == "Luis Pérez")
    assert ana["estado"] == "Pendiente"
    assert ana["saldo_pendiente"] == 100
    assert luis["estado"] == "Pagado"
    assert luis["saldo_pendiente"] == 0


def test_abono_parcial_y_no_supera_saldo(tmp_path: Path):
    _reset(tmp_path)
    snap = cob.snapshot()
    ana = next(s for s in snap["saldos"] if s["cliente"] == "Ana López")
    cobro = cob.registrar_cobro({
        "pedido_key": ana["pedido_key"],
        "monto": 40,
        "id_metodo_pago": 1,
        "fecha_cobro": "2026-09-02",
        "observaciones": "Abono",
    })
    assert cobro["ref"] == "#CB-0001"
    assert cobro["estado"] == "Pago Parcial"

    snap = cob.snapshot()
    ana = next(s for s in snap["saldos"] if s["cliente"] == "Ana López")
    assert ana["total_pagado"] == 40
    assert ana["saldo_pendiente"] == 60
    rec = store.load_day("2026-09-02", "dept")["records"][0]
    assert rec["pagado"] == "Parcial"

    try:
        cob.registrar_cobro({
            "pedido_key": ana["pedido_key"],
            "monto": 999,
            "id_metodo_pago": 2,
        })
        assert False, "debia rechazar un cobro mayor al saldo"
    except cob.CobroError as exc:
        assert "saldo" in str(exc).lower()


def test_liquidar_pedido(tmp_path: Path):
    _reset(tmp_path)
    snap = cob.snapshot()
    ana = next(s for s in snap["saldos"] if s["cliente"] == "Ana López")
    cob.registrar_cobro({
        "pedido_key": ana["pedido_key"],
        "monto": 100,
        "id_metodo_pago": 3,
        "fecha_cobro": "2026-09-03",
    })
    snap = cob.snapshot()
    ana = next(s for s in snap["saldos"] if s["cliente"] == "Ana López")
    assert ana["estado"] == "Pagado"
    assert ana["saldo_pendiente"] == 0
    rec = store.load_day("2026-09-02", "dept")["records"][0]
    assert rec["pagado"] == "Si"
