# -*- coding: utf-8 -*-
"""Chatbot por reglas (sin IA) para sugerir respuestas en Kommo."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


_PHONE_RE = re.compile(
    r"(?:\+?503[\s\-]?)?(?:6|7)\d{3}[\s\-]?\d{4}|\b\d{8}\b|\b\d{4}[\s\-]\d{4}\b"
)
_YES_RE = re.compile(
    r"(?i)\b(si|sí|ok|vale|claro|dale|listo|quiero|me interesa|lo quiero|"
    r"esta bien|está bien|de acuerdo|perfecto)\b"
)
_NO_RE = re.compile(r"(?i)\b(no|nop|nel|cancelar|ya no|no quiero|no gracias)\b")
_PRICE_RE = re.compile(r"(?i)\b(precio|cuanto|cuánto|cuesta|vale|costo|\$)\b")
_SHIP_RE = re.compile(r"(?i)\b(envio|envío|entrega|llega|demora|tiempo|dias|días)\b")
_PAY_RE = re.compile(r"(?i)\b(pago|pagar|transfer|efectivo|contraentrega|cod|tarjeta)\b")
_GREET_RE = re.compile(r"(?i)^\s*(hola|buenas|buen[oa]s\s*(d[ií]as|tardes|noches)|hey|saludos)\b")
_NAME_LABEL_RE = re.compile(
    r"(?i)(?:^|\n)\s*(?:nombre|me llamo|soy)\s*[:\-]?\s*"
    r"([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ.'-]*(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ.']*){0,3})"
)
_NAME_STOP_RE = re.compile(
    r"(?i)\b(whatsapp|telefono|tel[eé]fono|direcci[oó]n|colonia|envio|env[ií]o)\b"
)
_ADDR_HINT_RE = re.compile(
    r"(?i)\b(colonia|col\.|pasaje|calle|avenida|av\.|residencial|canton|cantón|"
    r"municipio|departamento|referencia|cerca de|frente a|pol[ií]gono)\b"
)
_DEPT_RE = re.compile(
    r"(?i)\b(san salvador|la libertad|santa ana|san miguel|sonsonate|"
    r"ahuachapan|ahuachapán|la paz|cuscatlan|cuscatlán|chalatenango|"
    r"cabañas|usulutan|usulután|san vicente|morazan|morazán|la union|la unión)\b"
)


@dataclass
class Analysis:
    intent: str
    missing: list[str] = field(default_factory=list)
    found: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _extract_phone(text: str) -> str:
    m = _PHONE_RE.search(text or "")
    if not m:
        return ""
    digits = re.sub(r"\D", "", m.group(0))
    if digits.startswith("503") and len(digits) >= 11:
        digits = digits[-8:]
    return digits if len(digits) == 8 else m.group(0).strip()


def _looks_like_person_name(value: str) -> bool:
    ln = _clean(value)
    if not ln:
        return False
    words = ln.split()
    if not (2 <= len(words) <= 4):
        return False
    if re.search(r"\d", ln):
        return False
    if _ADDR_HINT_RE.search(ln) or _DEPT_RE.search(ln):
        return False
    if _PRICE_RE.search(ln) or _SHIP_RE.search(ln) or _PAY_RE.search(ln):
        return False
    if _YES_RE.search(ln) or _NO_RE.search(ln) or _GREET_RE.search(ln):
        return False
    # Evitar frases conversacionales
    if re.search(r"(?i)\b(quiero|interes|precio|env[ií]o|pedido|gracias|por favor)\b", ln):
        return False
    return True


def _extract_name(text: str) -> str:
    m = _NAME_LABEL_RE.search(text or "")
    if m:
        candidate = _clean(m.group(1))
        candidate = _NAME_STOP_RE.split(candidate)[0].strip(" -,:")
        if candidate and ( _looks_like_person_name(candidate) or len(candidate.split()) >= 2):
            return candidate
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) == 1 and _looks_like_person_name(lines[0]):
        return lines[0]
    for ln in lines:
        # Evitar líneas etiquetadas como WhatsApp / dirección
        if _NAME_STOP_RE.search(ln) and not re.match(r"(?i)^\s*(nombre|me llamo|soy)\b", ln):
            continue
        if _looks_like_person_name(ln):
            return ln
    return ""


def _looks_like_address(text: str) -> bool:
    t = text or ""
    if _ADDR_HINT_RE.search(t) or _DEPT_RE.search(t):
        return True
    # Varias líneas con algo de detalle
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 2 and len(t) >= 40:
        return True
    return False


def analyze_message(text: str) -> Analysis:
    raw = (text or "").strip()
    compact = _clean(raw)
    if not compact:
        return Analysis(intent="vacio", notes=["No hay texto para analizar."])

    found: dict[str, str] = {}
    phone = _extract_phone(raw)
    name = _extract_name(raw)
    if phone:
        found["telefono"] = phone
    if name:
        found["nombre"] = name
    if _looks_like_address(raw):
        found["direccion"] = "detectada"

    missing = []
    if "nombre" not in found:
        missing.append("nombre completo")
    if "telefono" not in found:
        missing.append("número de WhatsApp")
    if "direccion" not in found:
        missing.append("dirección de entrega completa")

    # Intents por prioridad
    if _GREET_RE.search(compact) and len(compact) < 40 and not found:
        return Analysis(intent="saludo", missing=missing, found=found)

    if _NO_RE.search(compact) and not _YES_RE.search(compact):
        return Analysis(intent="rechazo", missing=[], found=found)

    if _PRICE_RE.search(compact) and len(found) == 0:
        return Analysis(intent="precio", missing=missing, found=found)

    if _SHIP_RE.search(compact) and len(found) < 2:
        return Analysis(intent="envio", missing=missing, found=found)

    if _PAY_RE.search(compact):
        return Analysis(intent="pago", missing=missing, found=found)

    if not missing:
        return Analysis(intent="datos_completos", missing=[], found=found)

    if found:
        return Analysis(intent="datos_parciales", missing=missing, found=found)

    if _YES_RE.search(compact):
        return Analysis(intent="interes", missing=missing, found=found)

    return Analysis(intent="generico", missing=missing, found=found)


def suggest_reply(text: str) -> dict:
    """Devuelve sugerencia lista para copiar/pegar."""
    analysis = analyze_message(text)
    intent = analysis.intent
    missing = analysis.missing
    found = analysis.found

    if intent == "vacio":
        reply = (
            "Hola 👋 ¿Me podrías reenviar tu mensaje? No lo pude ver completo."
        )
    elif intent == "saludo":
        reply = (
            "¡Hola! 😊 Gracias por escribirnos.\n"
            "¿Te interesa el reloj? Si deseas ordenarlo, por favor envíame:\n"
            "1. Dirección de entrega completa\n"
            "2. Número de WhatsApp\n"
            "3. Nombre completo"
        )
    elif intent == "rechazo":
        reply = (
            "Entendido, no hay problema 🙂\n"
            "Si más adelante te interesa, aquí estaremos para ayudarte."
        )
    elif intent == "precio":
        reply = (
            "El precio es $35 e incluye envío gratis 🚚\n"
            "Si deseas ordenarlo, envíame:\n"
            "1. Dirección de entrega completa\n"
            "2. Número de WhatsApp\n"
            "3. Nombre completo"
        )
    elif intent == "envio":
        reply = (
            "El envío es gratis y coordinamos la entrega según tu zona 🚚\n"
            "Para procesar tu pedido necesito:\n"
            "1. Dirección de entrega completa\n"
            "2. Número de WhatsApp\n"
            "3. Nombre completo"
        )
    elif intent == "pago":
        reply = (
            "Puedes pagar contra entrega (COD) al recibir tu pedido 💵\n"
            "Si ya quieres ordenarlo, envíame:\n"
            "1. Dirección de entrega completa\n"
            "2. Número de WhatsApp\n"
            "3. Nombre completo"
        )
    elif intent == "interes":
        reply = (
            "¡Perfecto! 🙌 Para confirmar tu pedido, por favor envíame:\n"
            "1. Dirección de entrega completa\n"
            "2. Número de WhatsApp\n"
            "3. Nombre completo"
        )
    elif intent == "datos_completos":
        bits = []
        if found.get("nombre"):
            bits.append(f"Nombre: {found['nombre']}")
        if found.get("telefono"):
            bits.append(f"WhatsApp: {found['telefono']}")
        if found.get("direccion"):
            bits.append("Dirección: recibida")
        resumen = "\n".join(f"• {b}" for b in bits) if bits else "• Datos recibidos"
        reply = (
            "¡Gracias! Ya tengo tus datos ✅\n"
            f"{resumen}\n\n"
            "En breve confirmamos tu pedido. ¿Deseas agregar alguna referencia de entrega?"
        )
    elif intent == "datos_parciales":
        ok = []
        if found.get("nombre"):
            ok.append(f"nombre ({found['nombre']})")
        if found.get("telefono"):
            ok.append(f"WhatsApp ({found['telefono']})")
        if found.get("direccion"):
            ok.append("dirección")
        faltan = "\n".join(f"{i}. {m.capitalize()}" for i, m in enumerate(missing, 1))
        tiene = ", ".join(ok) if ok else "parte de la información"
        reply = (
            f"¡Gracias! Ya recibí {tiene} ✅\n"
            f"Me falta lo siguiente para confirmar tu pedido:\n"
            f"{faltan}"
        )
    else:
        reply = (
            "¡Gracias por tu mensaje! 😊\n"
            "Para ayudarte mejor con tu pedido, por favor envíame:\n"
            "1. Dirección de entrega completa\n"
            "2. Número de WhatsApp\n"
            "3. Nombre completo"
        )

    return {
        "ok": True,
        "intent": intent,
        "found": found,
        "missing": missing,
        "suggestion": reply,
    }
