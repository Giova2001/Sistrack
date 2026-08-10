# -*- coding: utf-8 -*-
"""Parser local: convierte texto desordenado de pedidos en registros estructurados."""
from __future__ import annotations

import re
from typing import Any

from cargar_pedidos_sistrack import (
    DEFAULT_OBSERVATIONS,
    clean_phone,
    infer_payment,
    normalize_notes,
)
from ubicaciones import get_catalog, infer_location, norm
from web.store import (
    DEFAULT_PRODUCT_KEYWORDS,
    load_product_keywords,
    parse_natural_delivery_date,
)


DEFAULT_FIELDS = [
    {"key": "nombre", "label": "Nombre", "enabled": True},
    {"key": "telefono", "label": "Telefono", "enabled": True},
    {"key": "departamento", "label": "Departamento", "enabled": True},
    {"key": "municipio", "label": "Municipio", "enabled": True},
    {"key": "direccion", "label": "Direccion", "enabled": True},
    {"key": "punto_referencia", "label": "Punto de referencia", "enabled": True},
    {"key": "producto", "label": "Contenido o Producto/s", "enabled": True},
    {"key": "grabado", "label": "Grabado (Si/No)", "enabled": True},
    {"key": "mensaje_grabado", "label": "Mensaje del grabado", "enabled": True},
    {"key": "precio", "label": "Precio total", "enabled": True},
    {"key": "pagado", "label": "Pagado (Si/No)", "enabled": True},
    {"key": "fecha_entrega", "label": "Fecha de entrega", "enabled": True},
    {"key": "observaciones", "label": "Observaciones", "enabled": True},
    {"key": "numero_de_emergencia", "label": "Numero de emergencia", "enabled": True},
]

_PRODUCT_KEYS = tuple(DEFAULT_PRODUCT_KEYWORDS)


def _product_keys() -> tuple[str, ...]:
    try:
        return tuple(load_product_keywords())
    except Exception:
        return _PRODUCT_KEYS

_REF_START = re.compile(
    r"(?i)\b(?:frente\s+a|atras\s+de|atrás\s+de|detras\s+de|detrás\s+de|"
    r"al\s+lado\s+de|cerca\s+de|junto\s+a|a\s+la\s+par\s+de|"
    r"punto\s+de\s+referencia|referencia|ref\.?)\b"
)

_OBS_START = re.compile(
    r"(?i)\b(?:contactar(?:\s+al\s+cliente)?|pagado|envio\s+gratis|"
    r"envío\s+gratis|entrega\s+dia|llamar|notificar|cambio\b)"
)

_PRODUCT_START = re.compile(
    r"(?i)(?:^|[\s,\-–—])("
    r"promoci[oó]n|casio|seiko|wood|aviador|ray-?ban|old\s+money|retro|"
    r"rose\s+gold|mrw|mtp|ltp|classic|vintage|luxury|kit|lente|lentes|"
    r"reloj|gafas|2x1|producto|contenido"
    r")\b"
)

_PRICE_RE = re.compile(
    r"(?i)(?:total\s*)?\$\s*(\d+(?:[.,]\d+)?)|(?:precio|total)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)"
)

_PHONE_RE = re.compile(
    r"(?:\+?503[\s\-]*)?(\d{4}[\s\-]?\d{4})|(?:\+?\d{1,3}[\s\-]*)?(\d{8,11})"
)

_EMERGENCY_RE = re.compile(
    r"(?i)(?:emergencia|tel(?:efono)?\s*(?:de\s+)?emergencia|otro\s+numero)\s*[:\-]?\s*"
    r"((?:\+?503[\s\-]*)?\d{4}[\s\-]?\d{4})"
)


def _split_blocks(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").strip()
    # limpiar emojis frecuentes de WhatsApp
    text = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]+",
        " ",
        text,
    )
    if not text:
        return []
    parts = re.split(r"\n\s*\n+", text)
    if len(parts) == 1:
        numbered = re.split(r"(?=\n?\s*\d{1,2}[\.\)]\s+)", "\n" + text)
        numbered = [p.strip() for p in numbered if p.strip()]
        if len(numbered) > 1:
            return numbered
        inline = re.split(r"(?=\b\d{1,2}[\.\)]\s+[A-Za-zÁÉÍÓÚáéíóúÑñ])", text)
        inline = [p.strip() for p in inline if p.strip()]
        if len(inline) > 1:
            return inline
        # Separadores tipo "----" o "•••"
        dashed = re.split(r"\n\s*[-–—•]{3,}\s*\n", text)
        dashed = [p.strip() for p in dashed if p.strip()]
        if len(dashed) > 1:
            return dashed
    return [p.strip() for p in parts if p.strip()]


def _collapse_ws(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n")).strip()


def _extract_phone(text: str) -> str:
    m = _PHONE_RE.search(text)
    if not m:
        return ""
    return clean_phone(m.group(0))


def _extract_price(text: str) -> str:
    m = _PRICE_RE.search(text)
    if m:
        return (m.group(1) or m.group(2) or "").replace(",", ".")
    if re.search(r"(?i)\bpagado\b", text) and not re.search(r"\$\s*\d", text):
        return "0"
    return ""


def _extract_emergency(text: str, main_phone: str) -> str:
    m = _EMERGENCY_RE.search(text)
    if m:
        return clean_phone(m.group(1))
    return ""


def _cut_after_markers(text: str) -> tuple[str, str]:
    """Separa observaciones al final (Contactar..., Pagado...)."""
    m = _OBS_START.search(text)
    if not m:
        return text.strip(), ""
    return text[: m.start()].strip(" -\t,;"), text[m.start() :].strip()


def _find_product_span(text: str) -> tuple[str, str, str]:
    """Devuelve (antes, producto, despues) buscando inicio de producto y precio/obs."""
    work, obs_tail = _cut_after_markers(text)
    price_m = _PRICE_RE.search(work)
    end = price_m.start() if price_m else len(work)
    head = work[:end]
    tail_price = work[end:] if price_m else ""

    pm = _PRODUCT_START.search(head)
    if pm:
        before = head[: pm.start(1)].rstrip(" -\t,;")
        product = head[pm.start(1) :].strip(" -\t,;")
        return before, product, (tail_price + (" " + obs_tail if obs_tail else "")).strip()

    # Multi-linea: lineas con keywords
    keys = _product_keys()
    lines = [ln.strip(" -*\t") for ln in text.splitlines() if ln.strip()]
    products: list[str] = []
    kept: list[str] = []
    for ln in lines:
        low = norm(ln)
        if any(k in low for k in keys) or low.startswith(("-", "•")):
            clean = re.sub(r"^[\-\*•]\s*", "", ln).strip()
            clean = _PRICE_RE.sub("", clean).strip(" -")
            if clean and not _OBS_START.match(clean):
                products.append(clean)
        else:
            kept.append(ln)
    if products:
        return "\n".join(kept), "; ".join(products), obs_tail
    return work, "", (tail_price + (" " + obs_tail if obs_tail else "")).strip()


def _extract_name_before_phone(text: str, phone: str) -> str:
    if phone:
        # Cortar en el telefono (primera aparicion)
        m = re.search(re.escape(phone[:4]) + r"[\s\-]?" + re.escape(phone[4:]), text)
        if not m:
            m = re.search(r"\d{4}[\s\-]?\d{4}", text)
        if m:
            name = text[: m.start()]
        else:
            name = text
    else:
        name = text.splitlines()[0] if text.splitlines() else text

    name = name.replace("\n", " ")
    name = re.sub(r"^\s*\d{1,2}[\.\)]\s*", "", name)
    name = re.sub(r"(?i)^(nombre|cliente)\s*[:\-]\s*", "", name)
    name = re.sub(r"\s+", " ", name).strip(" -,\t")
    # Si el "nombre" arrastra direccion, cortar en palabras tipicas
    cut = re.search(
        r"(?i)\b(calle|colonia|residencial|urbanizacion|urbanización|canton|cantón|"
        r"pasaje|avenida|av\.|final|km\.?|poligono|polígono|departamento|municipio)\b",
        name,
    )
    if cut and cut.start() > 8:
        name = name[: cut.start()].strip(" -,")
    return name.strip()


def _known_place_spans(blob: str) -> list[tuple[int, int, str, str]]:
    """Lista (start, end, dept, municipio_label) de lugares conocidos en el texto."""
    from ubicaciones import (
        DISTRITO_SPELLINGS,
        EXTRA_ALIASES,
        _canon_dept,
        sistrack_city_label,
    )

    catalog = get_catalog()
    spans: list[tuple[int, int, str, str]] = []

    def add_span(start: int, end: int, dept: str, muni: str) -> None:
        spans.append((start, end, dept, muni))

    for d in catalog.departments:
        for m in re.finditer(rf"(?i)(?<!\w){re.escape(d)}(?!\w)", blob):
            add_span(m.start(), m.end(), d, "")

    items = sorted(catalog.distrito_canon.items(), key=lambda x: -len(x[0]))
    for dkey, distrito in items:
        for m in re.finditer(rf"(?i)(?<!\w){re.escape(distrito)}(?!\w)", blob):
            add_span(
                m.start(),
                m.end(),
                catalog.distrito_to_dept[dkey],
                sistrack_city_label(distrito),
            )

    for key, (dpt, distrito) in EXTRA_ALIASES.items():
        for m in re.finditer(rf"(?i)(?<!\w){re.escape(distrito)}(?!\w)", blob):
            dpt_c = _canon_dept(
                catalog.distrito_to_dept.get(norm(distrito), dpt), catalog.departments
            )
            add_span(m.start(), m.end(), dpt_c, sistrack_city_label(distrito))
        if " " not in key and len(key) >= 5:
            for m in re.finditer(rf"(?i)(?<!\w){re.escape(key)}(?!\w)", blob):
                dpt_c = _canon_dept(
                    catalog.distrito_to_dept.get(norm(distrito), dpt), catalog.departments
                )
                add_span(m.start(), m.end(), dpt_c, sistrack_city_label(distrito))

    for key, distrito in sorted(DISTRITO_SPELLINGS.items(), key=lambda x: -len(x[0])):
        dkey = norm(distrito)
        dpt = catalog.distrito_to_dept.get(dkey)
        if not dpt:
            for dk, name in catalog.distrito_canon.items():
                if norm(name) == dkey:
                    dpt = catalog.distrito_to_dept[dk]
                    distrito = name
                    break
        if not dpt:
            continue
        for m in re.finditer(rf"(?i)(?<!\w){re.escape(key)}(?!\w)", blob):
            add_span(m.start(), m.end(), dpt, sistrack_city_label(distrito))
        # tambien la forma canonica si difiere
        for m in re.finditer(rf"(?i)(?<!\w){re.escape(distrito)}(?!\w)", blob):
            add_span(m.start(), m.end(), dpt, sistrack_city_label(distrito))

    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    chosen: list[tuple[int, int, str, str]] = []
    for s in spans:
        if any(not (s[1] <= c[0] or s[0] >= c[1]) for c in chosen):
            continue
        chosen.append(s)
    chosen.sort(key=lambda s: s[0])
    return chosen


def _extract_ref_from_address(addr: str) -> tuple[str, str]:
    m = _REF_START.search(addr)
    if not m:
        return addr.strip(" -,\t"), ""
    before = addr[: m.start()].strip(" -,\t")
    ref = addr[m.start() :].strip(" -,\t")
    # Cortar ref si sigue municipio/depto tipico al final ", San Pedro..., La Paz"
    # Dejar que el split de lugares recorte; aqui solo separamos el marcador
    return before, ref


def _split_address_location(addr_blob: str) -> tuple[str, str, str, str]:
    """
    Devuelve direccion, punto_referencia, departamento, municipio.
    Soporta: '... El Achiotal- Frente a la bomba, San Pedro Masahuat, La Paz'
    """
    blob = re.sub(r"\s+", " ", addr_blob).strip(" -,\t")
    if not blob:
        return "", "Sin referencia", "", ""

    direccion, ref = _extract_ref_from_address(blob)

    # Si la ref arrastra municipio/depto, separarlos
    loc_src = ref if ref else direccion
    places = _known_place_spans(blob)
    dept = ""
    muni = ""
    cut_at = len(blob)

    if places:
        # Preferir el ultimo municipio real (no homonimo del depto) y depto al final
        munis = [p for p in places if p[3]]
        depts = [p for p in places if not p[3]]
        real_munis = [p for p in munis if norm(p[3]) != norm(p[2])]
        if real_munis:
            munis = real_munis
        if munis:
            last_m = munis[-1]
            muni = last_m[3]
            dept = last_m[2]
            cut_at = min(cut_at, last_m[0])
        if depts:
            last_d = depts[-1]
            if last_d[0] >= len(blob) * 0.4 or not dept:
                dept = last_d[2]
            cut_at = min(cut_at, last_d[0])

        # Recortar direccion/ref antes de municipio/depto finales
        prefix = blob[:cut_at].strip(" -,\t")
        if ref:
            # ref puede incluir muni; recalcular
            rm = _REF_START.search(blob)
            if rm:
                ref_body = blob[rm.start() : cut_at].strip(" -,\t")
                # quitar coma final y restos
                ref_body = re.sub(r"[,\-\s]+$", "", ref_body)
                # quitar el label "Frente a..."
                direccion = blob[: rm.start()].strip(" -,\t")
                ref = ref_body or ref
            else:
                direccion = prefix
        else:
            direccion = prefix

    # Inferir con catalogo si falta
    if not dept or not muni:
        d2, m2 = infer_location(blob, ref)
        dept = dept or d2
        muni = muni or m2

    # Limpieza: no dejar muni/depto dentro de direccion
    if dept:
        direccion = re.sub(re.escape(dept) + r"\s*$", "", direccion, flags=re.I).strip(" -,")
        ref = re.sub(re.escape(dept) + r"\s*$", "", ref, flags=re.I).strip(" -,")
    if muni:
        # muni en sistrack es UPPER sin acentos; buscar variante en texto
        for p in places:
            if p[3] == muni:
                label = blob[p[0] : p[1]]
                direccion = re.sub(re.escape(label) + r"\s*$", "", direccion, flags=re.I).strip(" -,")
                ref = re.sub(re.escape(label) + r"\s*$", "", ref, flags=re.I).strip(" -,")

    direccion = re.sub(r"[\s,\-–—]+$", "", direccion).strip()
    ref = re.sub(r"[\s,\-–—]+$", "", ref).strip()

    # Quitar municipio/depto residuales al final de direccion/ref
    for _ in range(4):
        trimmed = False
        for p in sorted(places, key=lambda x: -(x[1] - x[0])):
            label = blob[p[0] : p[1]]
            nd = re.sub(rf"[,\s\-–—]*{re.escape(label)}\s*$", "", direccion, flags=re.I)
            nr = re.sub(rf"[,\s\-–—]*{re.escape(label)}\s*$", "", ref, flags=re.I)
            if nd != direccion or nr != ref:
                direccion, ref = nd.strip(" -,"), nr.strip(" -,")
                trimmed = True
        if not trimmed:
            break

    if not ref:
        ref = "Sin referencia"
    return direccion, ref, dept, muni


def _extract_engraving(text: str) -> tuple[str, str]:
    if not re.search(r"(?i)\bgrabado\b", text):
        return "No", ""
    m = re.search(
        r"(?i)grabado\s*[:\-]?\s*(.+?)(?=\n\$|\ncontactar|\ntotal|\nentrega|\Z)",
        text,
        re.S,
    )
    msg = ""
    if m:
        msg = m.group(1).strip()
        msg = re.sub(r"(?i)^si\s*[:\-]?\s*", "", msg).strip()
        msg = _PRICE_RE.sub("", msg).strip(" -|")
        if norm(msg) in ("si", "no", ""):
            msg = "" if norm(msg) != "si" else msg
            if norm(msg) == "no":
                return "No", ""
    return "Si", msg


def _extract_paid(text: str) -> str:
    n = norm(text)
    if "pagado" in n or "pagina web" in n or "transferencia" in n:
        return "Si"
    return "No"


def _extract_observations(text: str) -> str:
    # Preferir tramo desde "Contactar..." / notas tipicas
    m = _OBS_START.search(text)
    if m:
        note = text[m.start() :].strip()
        note = _PRICE_RE.sub("", note).strip(" -|")
        # Si empieza por Pagado y luego contactar, preferir contactar
        m2 = re.search(r"(?i)\bcontactar\b", note)
        if m2 and m2.start() > 0:
            note = note[m2.start() :].strip()
        note = normalize_notes(note)
        compact = note.lower().strip()
        if (
            not compact
            or compact == "contactar"
            or (compact.startswith("contactar") and len(compact) < 55)
            or compact in ("pagado", "pagado.")
        ):
            return DEFAULT_OBSERVATIONS
        return note

    lines = []
    for ln in text.splitlines():
        low = norm(ln)
        if any(
            k in low
            for k in (
                "contactar",
                "pagado",
                "cambio",
                "llamar",
                "notificar",
                "envio gratis",
                "entrega dia",
            )
        ):
            if low.strip() in ("pagado", "pagado."):
                continue
            lines.append(ln.strip())
    note = normalize_notes(" | ".join(lines) if lines else "")
    compact = note.lower().strip()
    if (
        not compact
        or compact == "contactar"
        or (compact.startswith("contactar") and len(compact) < 55)
    ):
        return DEFAULT_OBSERVATIONS
    return note


def parse_order_text(text: str, default_delivery: str = "") -> list[dict[str, Any]]:
    """Parsea uno o varios pedidos desde texto libre (una o varias lineas)."""
    blocks = _split_blocks(text)
    records: list[dict[str, Any]] = []
    for raw_block in blocks:
        block = _collapse_ws(raw_block)
        if not block:
            continue

        telefono = _extract_phone(block)
        precio = _extract_price(block) or "0"
        grabado, mensaje = _extract_engraving(block)
        pagado = _extract_paid(block)
        obs = _extract_observations(block) or DEFAULT_OBSERVATIONS
        emergencia = _extract_emergency(block, telefono)

        nombre = _extract_name_before_phone(block, telefono)

        # Resto despues del telefono
        rest = block
        if telefono:
            m = re.search(r"\d{4}[\s\-]?\d{4}", rest)
            if m:
                rest = rest[m.end() :].strip(" -,\t")

        before_prod, producto, _after = _find_product_span(rest)
        if not producto:
            _, producto, _ = _find_product_span(block)
        producto = _PRICE_RE.sub("", producto or "").strip(" -,\t")
        producto = _OBS_START.split(producto)[0].strip(" -,\t") if producto else ""
        if producto:
            prod_lines = []
            for ln in re.split(r"[\n|;]+", producto):
                low = norm(ln)
                if not ln.strip():
                    continue
                if low.startswith("grabado") or _OBS_START.match(ln.strip()):
                    continue
                if _PRICE_RE.search(ln) and len(ln.strip()) < 12:
                    continue
                prod_lines.append(ln.strip(" -,\t"))
            producto = "; ".join(prod_lines).strip(" -,\t;")

        addr_blob = before_prod.strip(" -,\t")
        if not addr_blob:
            addr_blob = rest
            for chunk in (producto,):
                if chunk and chunk in addr_blob:
                    addr_blob = addr_blob.replace(chunk, " ", 1)
            addr_blob = _PRICE_RE.sub("", addr_blob)
            addr_blob = _OBS_START.split(addr_blob)[0]
            addr_blob = addr_blob.strip(" -,\t")

        direccion, ref, dept, muni = _split_address_location(addr_blob)
        if not dept or not muni:
            d2, m2 = infer_location(direccion or addr_blob, ref)
            dept = dept or d2
            muni = muni or m2

        if pagado == "Si":
            precio = "0"

        if not nombre and not telefono:
            continue

        if nombre:
            nombre = re.sub(r"^\s*\d{1,2}[\.\)]\s*", "", nombre).strip()

        entrega = parse_natural_delivery_date(block) or default_delivery
        payment = infer_payment(obs + (" PAGADO" if pagado == "Si" else ""))

        warnings: list[str] = []
        if not telefono or len(re.sub(r"\D", "", telefono)) < 8:
            warnings.append("Telefono incompleto")
        if not direccion or len(direccion) < 5:
            warnings.append("Direccion incompleta")
        if not muni or (dept and norm(muni) == norm(dept)):
            warnings.append("Ubicacion dudosa")
        if not producto or producto == "Producto":
            warnings.append("Producto generico o vacio")

        records.append(
            {
                "nombre": nombre,
                "telefono": telefono,
                "departamento": dept,
                "municipio": muni,
                "direccion": direccion,
                "punto_referencia": ref or "Sin referencia",
                "producto": producto or "Producto",
                "grabado": grabado,
                "mensaje_grabado": mensaje,
                "precio": precio,
                "pagado": pagado,
                "fecha_entrega": entrega,
                "observaciones": obs,
                "numero_de_emergencia": emergencia,
                "peso": "0.1",
                "payment_type": payment,
                "incomplete": bool(warnings),
                "warnings": warnings,
                "location_uncertain": "Ubicacion dudosa" in warnings,
                "upload_status": "pending",
                "upload_error": "",
            }
        )
    return records
