# -*- coding: utf-8 -*-
"""Cobros / pagos locales — misma lógica que CobroController y PedidoSaldoService.

Reglas portadas:
- saldo_pendiente = max(0, total_pedido - total_pagado)
- total_pagado = suma de cobros (o el total si el pedido ya marcó pagado=Si)
- Estado: Pendiente / Pago Parcial / Pagado
- Monto > 0 y no mayor al saldo pendiente
- Métodos: Efectivo, Transferencia, Tarjeta, Otro
- Referencia de cobro #CB-0001
"""
from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from web import store as pedido_store
from web.store import (
    DATA_DIR,
    _IO_LOCK,
    _atomic_write_text,
    _parse_precio,
    load_day,
    save_day,
)

COBROS_PATH = DATA_DIR / "cobros.json"
_COB_LOCK = threading.RLock()

METODOS_DEFAULT = [
    {"id": 1, "nombre": "Efectivo"},
    {"id": 2, "nombre": "Transferencia"},
    {"id": 3, "nombre": "Tarjeta"},
    {"id": 4, "nombre": "Otro"},
]


class CobroError(ValueError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty_store() -> dict[str, Any]:
    return {
        "next_cobro_id": 1,
        "metodos": list(METODOS_DEFAULT),
        "cobros": [],
        "updated_at": None,
    }


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    out = _empty_store()
    out.update({k: v for k, v in data.items() if k in out})
    out["metodos"] = list(out.get("metodos") or METODOS_DEFAULT)
    out["cobros"] = list(out.get("cobros") or [])
    if not out["metodos"]:
        out["metodos"] = list(METODOS_DEFAULT)
    return out


def load_cobros() -> dict[str, Any]:
    with _COB_LOCK, _IO_LOCK:
        if COBROS_PATH.exists():
            try:
                data = json.loads(COBROS_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return _normalize(data)
            except Exception:
                pass
        store = _empty_store()
        _persist(store)
        return store


def _persist(store: dict[str, Any]) -> None:
    payload = {
        "next_cobro_id": int(store.get("next_cobro_id") or 1),
        "metodos": store.get("metodos") or list(METODOS_DEFAULT),
        "cobros": store.get("cobros") or [],
        "updated_at": _now(),
    }
    _atomic_write_text(COBROS_PATH, json.dumps(payload, ensure_ascii=False, indent=2))


def pedido_key(fecha: str, zona: str, index: int) -> str:
    return f"{fecha}|{zona}|{index}"


def _parse_day_file(path: Path) -> tuple[str, str] | None:
    m = re.match(r"pedidos_(\d{4}-\d{2}-\d{2})(_SS)?$", path.stem)
    if not m:
        return None
    return m.group(1), "ss" if m.group(2) else "dept"


def _es_pagado_flag(raw: Any) -> bool:
    return str(raw or "").strip().lower() in ("si", "sí", "yes", "true", "1")


def _estado(total_pedido: float, total_pagado: float) -> tuple[str, str]:
    if total_pagado <= 0:
        return "Pendiente", "danger"
    if total_pagado + 0.009 < total_pedido:
        return "Pago Parcial", "warn"
    return "Pagado", "ok"


def _metodo_nombre(store: dict[str, Any], metodo_id: int) -> str:
    m = next((x for x in store["metodos"] if int(x["id"]) == int(metodo_id)), None)
    return m["nombre"] if m else "—"


def _codigo_pedido(fecha: str, zona: str, index: int) -> str:
    suf = "SS" if zona == "ss" else "DEPT"
    return f"PED-{fecha}-{suf}-{index + 1}"


def _cobros_de(store: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [c for c in store["cobros"] if c.get("pedido_key") == key]


def _listar_pedidos_fuente() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with _IO_LOCK:
        paths = sorted(pedido_store.DATA_DIR.glob("pedidos_*.json"))
    for path in paths:
        parsed = _parse_day_file(path)
        if not parsed:
            continue
        fecha, zona = parsed
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        records = list(data.get("records") or [])
        for i, rec in enumerate(records):
            items.append({
                "pedido_key": pedido_key(fecha, zona, i),
                "fecha": fecha,
                "zona": zona,
                "index": i,
                "codigo": _codigo_pedido(fecha, zona, i),
                "cliente": str(rec.get("nombre") or "").strip() or "—",
                "telefono": str(rec.get("telefono") or "").strip(),
                "producto": str(rec.get("producto") or "").strip(),
                "total_pedido": round(_parse_precio(rec.get("precio")), 2),
                "pagado_flag": _es_pagado_flag(rec.get("pagado")),
                "payment_type": str(rec.get("payment_type") or "").strip(),
            })
    return items


def _saldo_desde_fuente(store: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    cobros = _cobros_de(store, src["pedido_key"])
    pagado_cobros = round(sum(float(c.get("monto") or 0) for c in cobros), 2)
    total = float(src["total_pedido"] or 0)
    if cobros:
        total_pagado = pagado_cobros
    elif src["pagado_flag"]:
        total_pagado = total
    else:
        total_pagado = 0.0
    saldo = round(max(0.0, total - total_pagado), 2)
    estado, clase = _estado(total, total_pagado)
    return {
        **src,
        "total_pagado": total_pagado,
        "saldo_pendiente": saldo,
        "estado": estado,
        "clase_estado": clase,
        "cobros_count": len(cobros),
    }


def _hidratar_cobro(store: dict[str, Any], cobro: dict[str, Any], saldos_by_key: dict[str, dict]) -> dict[str, Any]:
    saldo = saldos_by_key.get(cobro.get("pedido_key") or "")
    estado, clase = ("Pendiente", "danger")
    total = 0.0
    if saldo:
        estado, clase = saldo["estado"], saldo["clase_estado"]
        total = saldo["total_pedido"]
    return {
        **cobro,
        "ref": "#CB-" + str(int(cobro["id"])).zfill(4),
        "metodo": _metodo_nombre(store, int(cobro.get("id_metodo_pago") or 0)),
        "cliente": cobro.get("cliente") or (saldo or {}).get("cliente") or "—",
        "pedido": cobro.get("codigo") or (saldo or {}).get("codigo") or "—",
        "total_pedido": total or float(cobro.get("total_pedido") or 0),
        "estado": estado,
        "clase_estado": clase,
    }


def snapshot(buscar: str = "", estado: str = "", fecha: str = "") -> dict[str, Any]:
    store = load_cobros()
    fuentes = _listar_pedidos_fuente()
    saldos_all = [_saldo_desde_fuente(store, src) for src in fuentes]
    saldos_by_key = {s["pedido_key"]: s for s in saldos_all}
    cobros = [_hidratar_cobro(store, dict(c), saldos_by_key) for c in store["cobros"]]
    saldos = list(saldos_all)

    if buscar:
        q = buscar.strip().lower()
        cobros = [
            c
            for c in cobros
            if q in str(c.get("cliente") or "").lower()
            or q in str(c.get("pedido") or "").lower()
            or q in str(c.get("ref") or "").lower()
        ]
        saldos = [
            s
            for s in saldos
            if q in str(s.get("cliente") or "").lower()
            or q in str(s.get("codigo") or "").lower()
        ]
    if estado:
        cobros = [c for c in cobros if c.get("estado") == estado]
        saldos_filtrados = [s for s in saldos if s.get("estado") == estado]
    else:
        saldos_filtrados = saldos
    if fecha:
        cobros = [c for c in cobros if str(c.get("fecha_cobro") or "").startswith(fecha)]
        saldos_filtrados = [s for s in saldos_filtrados if s.get("fecha") == fecha]

    pendientes = [s for s in saldos_all if float(s["saldo_pendiente"]) > 0.009]
    pendientes.sort(key=lambda s: -float(s["saldo_pendiente"]))
    cobros.sort(key=lambda c: str(c.get("fecha_cobro") or ""), reverse=True)

    today = date.today()
    prefix = today.isoformat()[:7]
    cobros_mes = [
        c for c in store["cobros"] if str(c.get("fecha_cobro") or "").startswith(prefix)
    ]
    return {
        "metodos": store["metodos"],
        "cobros": cobros,
        "saldos": saldos_filtrados,
        "saldos_pendientes": pendientes,
        "resumen": {
            "cobros_count": len(store["cobros"]),
            "cobros_mes": round(sum(float(c.get("monto") or 0) for c in cobros_mes), 2),
            "saldo_pendiente": round(sum(float(s["saldo_pendiente"]) for s in saldos_all), 2),
            "pedidos_pendientes": len(pendientes),
            "pedidos_pagados": sum(1 for s in saldos_all if s["estado"] == "Pagado"),
        },
        "updated_at": store.get("updated_at"),
    }


def _actualizar_flag_pedido(saldo: dict[str, Any]) -> None:
    day = load_day(saldo["fecha"], saldo["zona"])
    recs = list(day.get("records") or [])
    idx = int(saldo["index"])
    if not (0 <= idx < len(recs)):
        return
    rec = dict(recs[idx])
    if float(saldo["saldo_pendiente"]) <= 0.009:
        rec["pagado"] = "Si"
    elif float(saldo["total_pagado"]) > 0:
        rec["pagado"] = "Parcial"
    else:
        rec["pagado"] = "No"
    recs[idx] = rec
    save_day(saldo["fecha"], recs, saldo["zona"])


def registrar_cobro(payload: dict[str, Any]) -> dict[str, Any]:
    key = str(payload.get("pedido_key") or "").strip()
    monto = float(payload.get("monto") or 0)
    metodo_id = int(payload.get("id_metodo_pago") or 0)
    fecha_cobro = str(payload.get("fecha_cobro") or date.today().isoformat())[:10]
    if monto < 0.01:
        raise CobroError("El monto debe ser mayor a cero.")
    if not key:
        raise CobroError("Selecciona un pedido.")

    with _COB_LOCK:
        store = load_cobros()
        if not any(int(m["id"]) == metodo_id for m in store["metodos"]):
            raise CobroError("Método de pago inválido.")
        fuentes = _listar_pedidos_fuente()
        src = next((x for x in fuentes if x["pedido_key"] == key), None)
        if not src:
            raise CobroError("El pedido no existe.")
        saldo = _saldo_desde_fuente(store, src)
        if monto > float(saldo["saldo_pendiente"]) + 0.009:
            raise CobroError(
                f"El monto supera el saldo pendiente (${saldo['saldo_pendiente']:.2f})."
            )
        cid = int(store["next_cobro_id"])
        store["next_cobro_id"] = cid + 1
        cobro = {
            "id": cid,
            "pedido_key": key,
            "fecha": src["fecha"],
            "zona": src["zona"],
            "index": src["index"],
            "codigo": src["codigo"],
            "cliente": src["cliente"],
            "id_metodo_pago": metodo_id,
            "monto": round(monto, 2),
            "referencia_pago": str(payload.get("referencia_pago") or payload.get("observaciones") or "").strip() or None,
            "observaciones": str(payload.get("observaciones") or "").strip() or None,
            "usuario": str(payload.get("usuario") or "local").strip() or "local",
            "fecha_cobro": fecha_cobro,
            "created_at": _now(),
        }
        store["cobros"].insert(0, cobro)
        _persist(store)
        saldo = _saldo_desde_fuente(store, src)
        _actualizar_flag_pedido(saldo)
        saldos_by_key = {saldo["pedido_key"]: saldo}
        return _hidratar_cobro(store, cobro, saldos_by_key)


def detalle_cobro(cobro_id: int) -> dict[str, Any]:
    store = load_cobros()
    cobro = next((c for c in store["cobros"] if int(c["id"]) == int(cobro_id)), None)
    if not cobro:
        raise CobroError("El cobro no existe.")
    fuentes = _listar_pedidos_fuente()
    src = next((x for x in fuentes if x["pedido_key"] == cobro.get("pedido_key")), None)
    saldo = _saldo_desde_fuente(store, src) if src else None
    return _hidratar_cobro(store, dict(cobro), {cobro.get("pedido_key"): saldo} if saldo else {})
