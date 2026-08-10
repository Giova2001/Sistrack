# -*- coding: utf-8 -*-
"""Ejecuta subida Selenium con progreso, stop y opciones headless/dry-run."""
from __future__ import annotations

import threading
from typing import Any, Callable

from cargar_pedidos_sistrack import (
    DEFAULT_OBSERVATIONS,
    Pedido,
    SistrackBot,
    infer_payment,
)


ProgressCb = Callable[[int, str, str, dict], None]  # index, status, error, meta


class UploadRunner:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused_at: int | None = None
        self.running = False
        self.last_error = ""
        self.current_index: int | None = None
        self.total: int = 0
        self.done_count: int = 0

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
        emerg = str(rec.get("numero_de_emergencia") or "").strip()
        payment = str(rec.get("payment_type") or "").strip()
        if not payment or payment == "Efectivo":
            payment = infer_payment(obs + (" PAGADO" if rec.get("pagado") == "Si" else ""))
        if rec.get("pagado") == "Si" and "transfer" in (obs.lower() + " " + payment.lower()):
            payment = "Transferencia"
        elif rec.get("pagado") == "Si":
            # Pagado suele ir con transferencia / web
            if "efectivo" not in payment.lower():
                payment = payment or "Transferencia"
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
            payment_type=payment or "Efectivo",
            fila=fila,
            emergencia=emerg,
        )

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "paused_at": self._paused_at,
            "last_error": self.last_error,
            "current_index": self.current_index,
            "total": self.total,
            "done_count": self.done_count,
        }

    def start(
        self,
        records: list[dict[str, Any]],
        start_index: int,
        on_progress: ProgressCb,
        on_done: Callable[[], None],
        email: str | None = None,
        password: str | None = None,
        headless: bool = False,
        dry_run: bool = False,
    ) -> None:
        if self.running:
            raise RuntimeError("Ya hay una subida en curso")
        self._stop.clear()
        self.running = True
        self.last_error = ""
        self.total = len(records)
        self.done_count = sum(1 for r in records if r.get("upload_status") == "success")
        self.current_index = start_index

        def worker() -> None:
            bot = None
            try:
                bot = SistrackBot(headless=headless, dry_run=dry_run)
                bot.login(email=email, password=password)
                bot.go_crear_orden()
                total = len(records)
                for i in range(start_index, total):
                    if self._stop.is_set():
                        self._paused_at = i
                        break
                    rec = records[i]
                    self.current_index = i
                    if rec.get("upload_status") == "success":
                        continue
                    pedido = self.record_to_pedido(rec, i + 2)
                    meta = {"current_index": i, "total": total, "done_count": self.done_count}
                    try:
                        bot.procesar_pedido(pedido, i + 1, total)
                        self.done_count += 1
                        meta["done_count"] = self.done_count
                        on_progress(i, "success", "", meta)
                    except Exception as e:
                        self.last_error = str(e)
                        self._paused_at = i
                        on_progress(i, "error", str(e), meta)
                        break
                else:
                    self._paused_at = None
            except Exception as e:
                self.last_error = str(e)
                self._paused_at = start_index
                on_progress(
                    start_index,
                    "error",
                    str(e),
                    {"current_index": start_index, "total": self.total, "done_count": self.done_count},
                )
            finally:
                self.running = False
                self.current_index = None
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
