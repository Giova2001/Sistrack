# -*- coding: utf-8 -*-
"""Ejecuta subida Selenium con callbacks de progreso y stop en error."""
from __future__ import annotations

import threading
from typing import Any, Callable

from cargar_pedidos_sistrack import (
    DEFAULT_OBSERVATIONS,
    Pedido,
    SistrackBot,
    build_observations,
)


ProgressCb = Callable[[int, str, str], None]  # index, status, error


class UploadRunner:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused_at: int | None = None
        self.running = False
        self.last_error = ""

    @property
    def paused_at(self) -> int | None:
        return self._paused_at

    def record_to_pedido(self, rec: dict[str, Any], fila: int) -> Pedido:
        nombre = (rec.get("nombre") or "").strip()
        obs = (rec.get("observaciones") or DEFAULT_OBSERVATIONS).strip()
        if rec.get("grabado") == "Si" and rec.get("mensaje_grabado"):
            obs = f"{obs} | Grabado: {rec['mensaje_grabado']}".strip(" |")
        if rec.get("pagado") == "Si" and "pagado" not in obs.lower():
            obs = f"{obs} | PAGADO".strip(" |")
        return Pedido(
            nombre=nombre,
            telefono=str(rec.get("telefono") or ""),
            direccion=str(rec.get("direccion") or ""),
            referencia=str(rec.get("punto_referencia") or "Sin referencia"),
            producto=str(rec.get("producto") or "Producto"),
            precio=str(rec.get("precio") or "0"),
            peso=str(rec.get("peso") or "0.1"),
            fecha_registro="",
            fecha_entrega=str(rec.get("fecha_entrega") or ""),
            notas=obs,
            departamento=str(rec.get("departamento") or "San Salvador"),
            municipio=str(rec.get("municipio") or ""),
            payment_type=str(rec.get("payment_type") or "Efectivo"),
            fila=fila,
        )

    def start(
        self,
        records: list[dict[str, Any]],
        start_index: int,
        on_progress: ProgressCb,
        on_done: Callable[[], None],
        email: str | None = None,
        password: str | None = None,
    ) -> None:
        if self.running:
            raise RuntimeError("Ya hay una subida en curso")
        self._stop.clear()
        self.running = True
        self.last_error = ""

        def worker() -> None:
            bot = None
            try:
                bot = SistrackBot(headless=False, dry_run=False)
                bot.login(email=email, password=password)
                bot.go_crear_orden()
                total = len(records)
                for i in range(start_index, total):
                    if self._stop.is_set():
                        break
                    rec = records[i]
                    # Saltar ya exitosos
                    if rec.get("upload_status") == "success":
                        continue
                    pedido = self.record_to_pedido(rec, i + 2)
                    try:
                        bot.procesar_pedido(pedido, i + 1, total)
                        on_progress(i, "success", "")
                    except Exception as e:
                        self.last_error = str(e)
                        self._paused_at = i
                        on_progress(i, "error", str(e))
                        break
                else:
                    self._paused_at = None
            except Exception as e:
                self.last_error = str(e)
                self._paused_at = start_index
                on_progress(start_index, "error", str(e))
            finally:
                self.running = False
                try:
                    if bot:
                        bot.quit()
                except Exception:
                    pass
                on_done()

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
