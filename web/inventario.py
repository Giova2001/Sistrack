# -*- coding: utf-8 -*-
"""Inventario local (JSON) — misma lógica de negocio que el módulo Laravel.

Reglas portadas de InventarioMovimientoService / InventarioController:
- stock_disponible = stock_actual - stock_reservado
- Entrada: cantidad > 0, incrementa stock_actual y deja kardex
- Salida: no deja stock_disponible negativo (respeta reservado)
- Ajuste: stock_nuevo >= stock_reservado; kardex solo si hay diferencia
- No borrar producto con stock; si tiene kardex, se marca inactivo
- No borrar categoría con productos asignados
- Stock inicial al crear producto se registra como Entrada
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from web.store import DATA_DIR, _IO_LOCK, _atomic_write_text

INVENTARIO_PATH = DATA_DIR / "inventario.json"
_INV_LOCK = threading.RLock()

TIPOS_INVENTARIO = {
    "producto_base": "Producto base",
    "consumible": "Consumible",
}

TIPO_DEFAULT = "producto_base"


class InventarioError(ValueError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty_store() -> dict[str, Any]:
    return {
        "next_producto_id": 1,
        "next_categoria_id": 1,
        "next_movimiento_id": 1,
        "categorias": [],
        "productos": [],
        "movimientos": [],
        "updated_at": None,
    }


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    out = _empty_store()
    out.update({k: v for k, v in data.items() if k in out})
    out["categorias"] = list(out.get("categorias") or [])
    out["productos"] = list(out.get("productos") or [])
    out["movimientos"] = list(out.get("movimientos") or [])
    for p in out["productos"]:
        _hydrate_producto(p, out["categorias"])
    return out


def _hydrate_producto(p: dict[str, Any], categorias: list[dict]) -> dict[str, Any]:
    actual = float(p.get("stock_actual") or 0)
    reservado = float(p.get("stock_reservado") or 0)
    minimo = float(p.get("stock_minimo") or 0)
    p["stock_actual"] = round(actual, 2)
    p["stock_reservado"] = round(reservado, 2)
    p["stock_minimo"] = round(minimo, 2)
    p["stock_disponible"] = round(actual - reservado, 2)
    p["bajo_minimo"] = minimo > 0 and (actual - reservado) < minimo
    cat = next((c for c in categorias if c.get("id") == p.get("id_categoria")), None)
    p["categoria_nombre"] = cat["nombre"] if cat else ""
    p["tipo_inventario_label"] = TIPOS_INVENTARIO.get(
        p.get("tipo_inventario") or TIPO_DEFAULT, p.get("tipo_inventario") or ""
    )
    return p


def load_inventario() -> dict[str, Any]:
    with _INV_LOCK, _IO_LOCK:
        if INVENTARIO_PATH.exists():
            try:
                data = json.loads(INVENTARIO_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return _normalize(data)
            except Exception:
                pass
        store = _empty_store()
        _seed_defaults(store)
        _persist(store)
        return _normalize(store)


def _persist(store: dict[str, Any]) -> None:
    payload = {
        "next_producto_id": int(store.get("next_producto_id") or 1),
        "next_categoria_id": int(store.get("next_categoria_id") or 1),
        "next_movimiento_id": int(store.get("next_movimiento_id") or 1),
        "categorias": store.get("categorias") or [],
        "productos": [
            {
                k: v
                for k, v in p.items()
                if k
                not in (
                    "stock_disponible",
                    "bajo_minimo",
                    "categoria_nombre",
                    "tipo_inventario_label",
                )
            }
            for p in (store.get("productos") or [])
        ],
        "movimientos": store.get("movimientos") or [],
        "updated_at": _now(),
    }
    _atomic_write_text(
        INVENTARIO_PATH,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def _seed_defaults(store: dict[str, Any]) -> None:
    store["categorias"] = [
        {
            "id": 1,
            "nombre": "General",
            "descripcion": "Categoría inicial",
            "activo": True,
        }
    ]
    store["next_categoria_id"] = 2


def _producto(store: dict[str, Any], producto_id: int) -> dict[str, Any]:
    p = next((x for x in store["productos"] if int(x["id"]) == int(producto_id)), None)
    if not p:
        raise InventarioError("El producto no existe.")
    return p


def _categoria(store: dict[str, Any], categoria_id: int) -> dict[str, Any]:
    c = next((x for x in store["categorias"] if int(x["id"]) == int(categoria_id)), None)
    if not c:
        raise InventarioError("La categoría no existe.")
    return c


def _append_movimiento(
    store: dict[str, Any],
    *,
    producto: dict[str, Any],
    tipo: str,
    cantidad: float,
    stock_anterior: float,
    stock_nuevo: float,
    referencia: str | None,
    motivo: str | None,
    usuario: str | None,
) -> dict[str, Any]:
    mid = int(store["next_movimiento_id"])
    store["next_movimiento_id"] = mid + 1
    mov = {
        "id": mid,
        "id_producto": producto["id"],
        "producto_nombre": producto.get("nombre") or "",
        "tipo_movimiento": tipo,
        "cantidad": round(float(cantidad), 2),
        "stock_anterior": round(float(stock_anterior), 2),
        "stock_nuevo": round(float(stock_nuevo), 2),
        "referencia": (referencia or "").strip() or None,
        "motivo": (motivo or "").strip() or None,
        "usuario": (usuario or "local").strip() or "local",
        "fecha_movimiento": _now(),
    }
    store["movimientos"].insert(0, mov)
    return mov


def _construir_motivo_entrada(
    motivo: str | None, costo_unitario: float | None, cantidad: float
) -> str:
    partes = [motivo.strip() if motivo and motivo.strip() else "Entrada de inventario"]
    if costo_unitario is not None:
        partes.append(f"Costo unitario: {costo_unitario:.2f}")
        partes.append(f"Costo total: {round(costo_unitario * cantidad, 2):.2f}")
    return " | ".join(partes)


def snapshot() -> dict[str, Any]:
    store = load_inventario()
    productos = [_hydrate_producto(dict(p), store["categorias"]) for p in store["productos"]]
    activos = [p for p in productos if p.get("activo", True)]
    bajo = [p for p in activos if p.get("bajo_minimo")]
    cats = []
    for c in store["categorias"]:
        count = sum(1 for p in productos if p.get("id_categoria") == c["id"])
        cats.append({**c, "productos_count": count})
    return {
        "tipos_inventario": TIPOS_INVENTARIO,
        "categorias": cats,
        "productos": productos,
        "movimientos": store["movimientos"][:100],
        "resumen": {
            "productos_activos": len(activos),
            "stock_total": round(sum(float(p["stock_actual"]) for p in activos), 2),
            "bajo_minimo": len(bajo),
            "categorias": len(cats),
        },
        "updated_at": store.get("updated_at"),
    }


def listar_productos(
    buscar: str = "",
    categoria_id: int | None = None,
    tipo: str = "",
    estado: str = "",
    stock: str = "",
) -> list[dict[str, Any]]:
    store = load_inventario()
    items = [_hydrate_producto(dict(p), store["categorias"]) for p in store["productos"]]
    q = (buscar or "").strip().lower()
    if q:
        items = [
            p
            for p in items
            if q in str(p.get("nombre") or "").lower()
            or q in str(p.get("descripcion") or "").lower()
            or q == str(p.get("id"))
        ]
    if categoria_id:
        items = [p for p in items if p.get("id_categoria") == int(categoria_id)]
    if tipo:
        items = [p for p in items if p.get("tipo_inventario") == tipo]
    if estado == "activo":
        items = [p for p in items if p.get("activo", True)]
    elif estado == "inactivo":
        items = [p for p in items if not p.get("activo", True)]
    if stock == "con_stock":
        items = [p for p in items if float(p["stock_actual"]) > 0]
    elif stock == "sin_stock":
        items = [p for p in items if float(p["stock_actual"]) <= 0]
    elif stock == "bajo_minimo":
        items = [p for p in items if p.get("bajo_minimo")]
    items.sort(key=lambda p: str(p.get("nombre") or "").lower())
    return items


def crear_producto(payload: dict[str, Any]) -> dict[str, Any]:
    nombre = str(payload.get("nombre") or "").strip()
    if not nombre:
        raise InventarioError("El nombre es obligatorio.")
    tipo = str(payload.get("tipo_inventario") or TIPO_DEFAULT)
    if tipo not in TIPOS_INVENTARIO:
        raise InventarioError("Tipo de inventario inválido.")
    with _INV_LOCK:
        store = load_inventario()
        cat_id = int(payload.get("id_categoria") or 0)
        _categoria(store, cat_id)
        pid = int(store["next_producto_id"])
        store["next_producto_id"] = pid + 1
        stock_inicial = max(0.0, float(payload.get("stock_actual") or 0))
        producto = {
            "id": pid,
            "nombre": nombre[:100],
            "tipo_inventario": tipo,
            "descripcion": str(payload.get("descripcion") or "").strip() or None,
            "id_categoria": cat_id,
            "precio_unitario": round(max(0.0, float(payload.get("precio_unitario") or 0)), 2),
            "precio_paquete": round(max(0.0, float(payload.get("precio_paquete") or 0)), 2),
            "unidades_por_paquete": round(max(0.0, float(payload.get("unidades_por_paquete") or 0)), 2),
            "stock_actual": 0.0,
            "stock_reservado": 0.0,
            "stock_minimo": round(max(0.0, float(payload.get("stock_minimo") or 0)), 2),
            "activo": True,
            "fecha_creacion": _now(),
            "fecha_actualizacion": _now(),
        }
        store["productos"].append(producto)
        if stock_inicial > 0:
            registrar_entrada(
                store,
                pid,
                stock_inicial,
                referencia="STOCK-INICIAL",
                motivo="Stock inicial al crear producto",
                usuario=payload.get("usuario"),
            )
        _persist(store)
        return _hydrate_producto(dict(_producto(store, pid)), store["categorias"])


def actualizar_producto(producto_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    with _INV_LOCK:
        store = load_inventario()
        p = _producto(store, producto_id)
        if "nombre" in payload:
            nombre = str(payload.get("nombre") or "").strip()
            if not nombre:
                raise InventarioError("El nombre es obligatorio.")
            p["nombre"] = nombre[:100]
        if "tipo_inventario" in payload:
            tipo = str(payload.get("tipo_inventario") or TIPO_DEFAULT)
            if tipo not in TIPOS_INVENTARIO:
                raise InventarioError("Tipo de inventario inválido.")
            p["tipo_inventario"] = tipo
        if "descripcion" in payload:
            p["descripcion"] = str(payload.get("descripcion") or "").strip() or None
        if "id_categoria" in payload:
            cat_id = int(payload.get("id_categoria") or 0)
            _categoria(store, cat_id)
            p["id_categoria"] = cat_id
        for key in ("precio_unitario", "precio_paquete", "unidades_por_paquete", "stock_minimo"):
            if key in payload and payload[key] is not None:
                p[key] = round(max(0.0, float(payload[key] or 0)), 2)
        if "activo" in payload:
            p["activo"] = bool(payload["activo"])
        p["fecha_actualizacion"] = _now()
        if payload.get("stock_actual") is not None and str(payload.get("stock_actual")) != "":
            ajustar_stock(
                store,
                producto_id,
                float(payload["stock_actual"]),
                motivo=payload.get("motivo")
                or "Actualización de stock desde formulario de producto",
                usuario=payload.get("usuario"),
            )
        _persist(store)
        return _hydrate_producto(dict(_producto(store, producto_id)), store["categorias"])


def eliminar_producto(producto_id: int) -> dict[str, Any]:
    with _INV_LOCK:
        store = load_inventario()
        p = _producto(store, producto_id)
        if float(p.get("stock_actual") or 0) > 0:
            raise InventarioError(
                f"No se puede eliminar el producto porque aún tiene {p['stock_actual']} "
                "unidades en stock. Registra una salida o un ajuste para vaciarlo primero."
            )
        tiene_kardex = any(
            int(m.get("id_producto")) == int(producto_id) for m in store["movimientos"]
        )
        if tiene_kardex:
            p["activo"] = False
            p["fecha_actualizacion"] = _now()
            _persist(store)
            return {
                "ok": True,
                "soft_delete": True,
                "message": "El producto tiene historial de movimientos, por lo que fue marcado como INACTIVO.",
            }
        store["productos"] = [x for x in store["productos"] if int(x["id"]) != int(producto_id)]
        _persist(store)
        return {"ok": True, "soft_delete": False, "message": "Producto eliminado."}


def registrar_entrada(
    store: dict[str, Any] | None,
    producto_id: int,
    cantidad: float,
    *,
    costo_unitario: float | None = None,
    referencia: str | None = None,
    motivo: str | None = None,
    usuario: str | None = None,
) -> dict[str, Any]:
    if cantidad <= 0:
        raise InventarioError("La cantidad debe ser mayor a cero.")
    if costo_unitario is not None and costo_unitario < 0:
        raise InventarioError("El costo unitario no puede ser negativo.")

    own = store is None
    if own:
        _INV_LOCK.acquire()
        store = load_inventario()
    try:
        p = _producto(store, producto_id)
        if not p.get("activo", True):
            raise InventarioError("El producto no existe o está inactivo.")
        anterior = float(p.get("stock_actual") or 0)
        nuevo = round(anterior + cantidad, 2)
        p["stock_actual"] = nuevo
        p["fecha_actualizacion"] = _now()
        mov = _append_movimiento(
            store,
            producto=p,
            tipo="Entrada",
            cantidad=cantidad,
            stock_anterior=anterior,
            stock_nuevo=nuevo,
            referencia=referencia,
            motivo=_construir_motivo_entrada(motivo, costo_unitario, cantidad),
            usuario=usuario,
        )
        if own:
            _persist(store)
        return mov
    finally:
        if own:
            _INV_LOCK.release()


def registrar_salida(
    producto_id: int,
    cantidad: float,
    *,
    referencia: str | None = None,
    motivo: str | None = None,
    usuario: str | None = None,
) -> dict[str, Any]:
    if cantidad <= 0:
        raise InventarioError("La cantidad a descontar debe ser mayor a cero.")
    with _INV_LOCK:
        store = load_inventario()
        p = _producto(store, producto_id)
        if not p.get("activo", True):
            raise InventarioError("El producto no existe o está inactivo.")
        anterior = float(p.get("stock_actual") or 0)
        reservado = float(p.get("stock_reservado") or 0)
        nuevo = round(anterior - cantidad, 2)
        if nuevo < reservado - 0.0001:
            disponible = round(anterior - reservado, 2)
            raise InventarioError(
                f"Stock insuficiente. El stock disponible es {disponible} "
                f"(Stock actual: {anterior} - Reservado: {reservado})."
            )
        p["stock_actual"] = nuevo
        p["fecha_actualizacion"] = _now()
        mov = _append_movimiento(
            store,
            producto=p,
            tipo="Salida",
            cantidad=cantidad,
            stock_anterior=anterior,
            stock_nuevo=nuevo,
            referencia=referencia,
            motivo=motivo or "Salida general de inventario",
            usuario=usuario,
        )
        _persist(store)
        return mov


def ajustar_stock(
    store: dict[str, Any] | None,
    producto_id: int,
    stock_nuevo: float,
    *,
    motivo: str | None = None,
    usuario: str | None = None,
) -> dict[str, Any] | None:
    if stock_nuevo < 0:
        raise InventarioError("El stock no puede ser negativo.")
    own = store is None
    if own:
        _INV_LOCK.acquire()
        store = load_inventario()
    try:
        p = _producto(store, producto_id)
        anterior = float(p.get("stock_actual") or 0)
        reservado = float(p.get("stock_reservado") or 0)
        nuevo = round(float(stock_nuevo), 2)
        if nuevo < reservado - 0.0001:
            raise InventarioError(
                f"El stock actual no puede ser menor que la cantidad reservada ({reservado:.2f})."
            )
        diferencia = round(nuevo - anterior, 2)
        if abs(diferencia) < 0.0001:
            return None
        p["stock_actual"] = nuevo
        p["fecha_actualizacion"] = _now()
        mov = _append_movimiento(
            store,
            producto=p,
            tipo="Ajuste",
            cantidad=abs(diferencia),
            stock_anterior=anterior,
            stock_nuevo=nuevo,
            referencia="FORM-PRODUCTO",
            motivo=motivo
            or (
                "Ajuste de inventario: incremento desde formulario de producto"
                if diferencia > 0
                else "Ajuste de inventario: reducción desde formulario de producto"
            ),
            usuario=usuario,
        )
        if own:
            _persist(store)
        return mov
    finally:
        if own:
            _INV_LOCK.release()


def kardex(producto_id: int) -> dict[str, Any]:
    store = load_inventario()
    p = _hydrate_producto(dict(_producto(store, producto_id)), store["categorias"])
    movs = [m for m in store["movimientos"] if int(m.get("id_producto")) == int(producto_id)]
    return {"producto": p, "movimientos": movs}


def crear_categoria(nombre: str, descripcion: str | None = None) -> dict[str, Any]:
    nombre = (nombre or "").strip()
    if not nombre:
        raise InventarioError("El nombre de la categoría es obligatorio.")
    with _INV_LOCK:
        store = load_inventario()
        if any(c["nombre"].lower() == nombre.lower() for c in store["categorias"]):
            raise InventarioError("Ya existe una categoría con ese nombre.")
        cid = int(store["next_categoria_id"])
        store["next_categoria_id"] = cid + 1
        cat = {
            "id": cid,
            "nombre": nombre[:100],
            "descripcion": (descripcion or "").strip() or None,
            "activo": True,
        }
        store["categorias"].append(cat)
        _persist(store)
        return {**cat, "productos_count": 0}


def actualizar_categoria(categoria_id: int, nombre: str, descripcion: str | None = None) -> dict[str, Any]:
    nombre = (nombre or "").strip()
    if not nombre:
        raise InventarioError("El nombre de la categoría es obligatorio.")
    with _INV_LOCK:
        store = load_inventario()
        cat = _categoria(store, categoria_id)
        if any(
            c["nombre"].lower() == nombre.lower() and int(c["id"]) != int(categoria_id)
            for c in store["categorias"]
        ):
            raise InventarioError("Ya existe una categoría con ese nombre.")
        cat["nombre"] = nombre[:100]
        cat["descripcion"] = (descripcion or "").strip() or None
        _persist(store)
        count = sum(1 for p in store["productos"] if p.get("id_categoria") == cat["id"])
        return {**cat, "productos_count": count}


def eliminar_categoria(categoria_id: int) -> dict[str, Any]:
    with _INV_LOCK:
        store = load_inventario()
        _categoria(store, categoria_id)
        count = sum(1 for p in store["productos"] if int(p.get("id_categoria") or 0) == int(categoria_id))
        if count > 0:
            raise InventarioError(
                "No se puede eliminar la categoría porque tiene productos asignados. "
                "Primero reasigne o elimine esos productos."
            )
        store["categorias"] = [c for c in store["categorias"] if int(c["id"]) != int(categoria_id)]
        _persist(store)
        return {"ok": True, "message": "Categoría eliminada correctamente."}
