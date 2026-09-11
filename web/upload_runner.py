# -*- coding: utf-8 -*-
"""Ejecuta subida Selenium con progreso, stop y opciones headless/dry-run."""
from __future__ import annotations

import re
import threading
from typing import Any, Callable

from sistrack.cargar_pedidos_sistrack import (
    DEFAULT_OBSERVATIONS,
    Pedido,
    SistrackBot,
    infer_payment,
)


ProgressCb = Callable[[int, str, str, dict], None]  # index, status, error, meta

_TRAILING_GRABADO = re.compile(r"(?i)\s*[|—\-]*\s*Grabado\s*:.*$")
_TRAILING_EMERGENCIA = re.compile(r"(?i)\s*\|\s*Emergencia\s*:.*$")
_TRAILING_PAGADO = re.compile(r"(?i)\s*\|\s*PAGADO\s*$")


def forza_field_text(text: str, *, max_len: int | None = 50) -> str:
    """Texto limpio para Forza (sin grabado/emergencia/PAGADO; opcional tope 50)."""
    s = re.sub(r"\s+", " ", (text or "").strip())
    s = _TRAILING_GRABADO.sub("", s).strip(" |—-")
    s = _TRAILING_EMERGENCIA.sub("", s).strip(" |")
    s = _TRAILING_PAGADO.sub("", s).strip(" |")
    # Quitar "Grabado: …" embebido al final del producto
    s = re.sub(r"(?i)\s*[—\-]\s*Grabado\s*:.*$", "", s).strip()
    if max_len is not None and max_len > 0 and len(s) > max_len:
        s = s[:max_len].rstrip(" |,.-")
    return s


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

    def record_to_pedido(
        self, rec: dict[str, Any], fila: int, platform: str = "sistrack"
    ) -> Pedido:
        nombre = (rec.get("nombre") or "").strip()
        obs_raw = (rec.get("observaciones") or "").strip()
        producto = str(rec.get("producto") or "Producto").strip() or "Producto"
        use_forza = str(platform or "sistrack").strip().lower() == "forza"

        if use_forza:
            # Forza (~50 chars): no agregar grabado ni PAGADO a indicaciones
            obs = forza_field_text(obs_raw, max_len=50) or DEFAULT_OBSERVATIONS
            # Descripción / producto: solo contenido (sin grabado)
            producto = forza_field_text(producto, max_len=None) or "Producto"
        else:
            obs = obs_raw or DEFAULT_OBSERVATIONS
            if rec.get("grabado") == "Si" and rec.get("mensaje_grabado"):
                obs = f"{obs} | Grabado: {rec['mensaje_grabado']}".strip(" |")
            if rec.get("pagado") == "Si" and "pagado" not in obs.lower():
                obs = f"{obs} | PAGADO".strip(" |")

        emerg = str(rec.get("numero_de_emergencia") or "").strip()
        payment = str(rec.get("payment_type") or "").strip()
        if not payment or payment == "Efectivo":
            payment = infer_payment(
                obs + (" PAGADO" if rec.get("pagado") == "Si" else "")
            )
        if rec.get("pagado") == "Si" and "transfer" in (
            obs.lower() + " " + payment.lower()
        ):
            payment = "Transferencia"
        elif rec.get("pagado") == "Si":
            if "efectivo" not in payment.lower():
                payment = payment or "Transferencia"
        return Pedido(
            nombre=nombre,
            telefono=str(rec.get("telefono") or ""),
            direccion=str(rec.get("direccion") or ""),
            referencia=str(rec.get("punto_referencia") or "Sin referencia"),
            producto=producto,
            precio=str(rec.get("precio") or "0"),
            peso=str(rec.get("peso") or "0.1"),
            fecha_registro="",
            fecha_entrega=str(rec.get("fecha_entrega") or ""),
            notas=obs,
            departamento=str(rec.get("departamento") or "San Salvador"),
            municipio=str(rec.get("municipio") or ""),
            payment_type=payment or "Efectivo",
            fila=fila,
            # Forza: no usar emergencia en campos limitados
            emergencia="" if use_forza else emerg,
            colonia=str(rec.get("colonia") or "").strip(),
            pagado=str(rec.get("pagado") or "").strip(),
            forza_label=str(rec.get("forza_label") or "").strip(),
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
        platform: str = "sistrack",
        forza_codigo: str | None = None,
        forza_usuario: str | None = None,
        forza_password: str | None = None,
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
            use_forza = str(platform or "sistrack").strip().lower() == "forza"
            try:
                if use_forza:
                    from forza.cargar_pedidos_forza import ForzaBot

                    bot = ForzaBot(headless=headless, dry_run=dry_run)
                    bot.login(
                        codigo=forza_codigo,
                        usuario=forza_usuario,
                        password=forza_password,
                    )
                    bot.go_crear_guias()
                else:
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
                    # Descifrar ubicación según plataforma (poblado Forza, etc.)
                    from web.parser import enrich_record_for_platform

                    rec = enrich_record_for_platform(
                        dict(rec), "forza" if use_forza else "sistrack"
                    )
                    records[i].update(
                        {
                            k: rec[k]
                            for k in (
                                "colonia",
                                "municipio",
                                "departamento",
                                "forza_label",
                            )
                            if k in rec
                        }
                    )
                    pedido = self.record_to_pedido(
                        rec, i + 2, platform="forza" if use_forza else "sistrack"
                    )
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
                        close = getattr(bot, "close", None) or getattr(bot, "quit", None)
                        if close:
                            close()
                except Exception:
                    pass
                on_done()

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
