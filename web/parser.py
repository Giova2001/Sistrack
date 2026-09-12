# -*- coding: utf-8 -*-
"""Parser local: convierte texto desordenado de pedidos en registros estructurados."""
from __future__ import annotations

import re
from typing import Any

from sistrack.cargar_pedidos_sistrack import (
    DEFAULT_OBSERVATIONS,
    clean_phone,
    infer_payment,
    normalize_notes,
)
from sistrack.ubicaciones import get_catalog, infer_location, norm
from forza.ubicaciones_forza import decipher_forza_fields, extract_colonia
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

# Campos orientados al flujo Forza (Crear Guías).
# Solo campos que el portal usa; sin fecha/grabado/emergencia.
DEFAULT_FIELDS_FORZA = [
    {"key": "nombre", "label": "Nombre del cliente", "enabled": True},
    {"key": "telefono", "label": "Telefono (8 digitos)", "enabled": True},
    {"key": "departamento", "label": "Departamento", "enabled": True},
    {"key": "municipio", "label": "Municipio", "enabled": True},
    {"key": "colonia", "label": "Poblado / Colonia", "enabled": True},
    {"key": "direccion", "label": "Direccion destinatario", "enabled": True},
    {"key": "punto_referencia", "label": "Punto de referencia", "enabled": True},
    {"key": "producto", "label": "Producto (quien recibe / descripcion)", "enabled": True},
    {"key": "precio", "label": "Monto a cobrar (COD)", "enabled": True},
    {"key": "pagado", "label": "Ya pagado (Si=Estandar / No=COD)", "enabled": True},
    {"key": "devolucion", "label": "Es una devolucion (Si/No)", "enabled": True},
    {"key": "peso", "label": "Peso (Lbs)", "enabled": True},
    {"key": "observaciones", "label": "Indicaciones para entrega", "enabled": True},
]

# No aplican en Forza (existen en Express / Sistrack)
FORZA_UNUSED_FIELD_KEYS = frozenset(
    {
        "fecha_entrega",
        "numero_de_emergencia",
        "grabado",
        "mensaje_grabado",
    }
)

# Abreviaturas frecuentes para anexar al nombre de contacto (~50 chars)
_FORZA_PRODUCT_ABBR = (
    (re.compile(r"(?i)\bold\s*money.*rose"), "OMRG"),
    (re.compile(r"(?i)\bold\s*money"), "OM"),
    (re.compile(r"(?i)\bcasio\b.*\bl2\b|\bl2x1\b"), "L2x1"),
    (re.compile(r"(?i)\bcmtp\s*4\b|\bcmtp4\b"), "CMTP4"),
    (re.compile(r"(?i)\bcomr\b"), "COMR"),
    (re.compile(r"(?i)\bqql\b"), "QQL"),
    (re.compile(r"(?i)\bcrr\b"), "CRR"),
    (re.compile(r"(?i)\bseiko\b"), "SA"),
    (re.compile(r"(?i)\bcasio\b"), "Casio"),
)


def abbreviate_product_forza(producto: str, max_len: int = 18) -> str:
    """Producto corto para anexar al nombre en Forza (límite ~50)."""
    s = re.sub(r"\s+", " ", (producto or "").strip())
    s = re.sub(r"(?i)\s*[—\-]\s*Grabado\s*:.*$", "", s).strip()
    if not s:
        return ""
    s = re.split(r"[;|/]", s)[0].strip()
    low = s.lower()
    if low in {"producto", "productos", "contenido"}:
        return ""
    for rx, abbr in _FORZA_PRODUCT_ABBR:
        if rx.search(s):
            return abbr[:max_len] if max_len > 0 else abbr
    s = re.sub(r"\$?\d+([.,]\d+)?", "", s).strip(" -,\t")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    # Iniciales si hay varias palabras largas
    parts = [p for p in re.split(r"\s+", s) if p]
    if len(parts) >= 3 and sum(len(p) for p in parts) > max_len:
        initials = "".join(p[0].upper() for p in parts if p[:1].isalnum())
        if 2 <= len(initials) <= max_len:
            return initials
    if max_len > 0 and len(s) > max_len:
        cut = s[:max_len].rsplit(" ", 1)[0].strip()
        s = cut or s[:max_len]
    return s.strip(" -,\t")


def forza_nombre_con_producto(
    nombre: str, producto: str, *, max_len: int = 50
) -> str:
    """Nombre de contacto + producto abreviado, respetando tope ~50 de Forza."""
    base = re.sub(r"\s+", " ", (nombre or "").strip())
    # Evitar duplicar el producto completo si ya venía pegado al nombre
    prod_full = re.sub(r"\s+", " ", (producto or "").strip())
    abbr = abbreviate_product_forza(prod_full, max_len=18)
    if not base:
        return (abbr or prod_full or "Cliente")[:max_len]
    if abbr:
        # Si el abbr o el producto ya está en el nombre, no repetir
        fold_base = norm(base)
        if norm(abbr) in fold_base or (prod_full and norm(prod_full) in fold_base):
            out = base
        else:
            out = f"{base} {abbr}".strip()
    else:
        out = base
    if max_len > 0 and len(out) > max_len:
        out = out[:max_len].rsplit(" ", 1)[0].strip() or out[:max_len]
    return out.strip()


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

# SV: 8 digitos (2/6/7…), con o sin 503, pegado a letras, con espacio o guion
_PHONE_FIND_RE = re.compile(
    r"(?<!\d)"
    r"(?:\+?\s*503[\s\-.]*)?"
    r"([267]\d{3}|[2-9]\d{3})"
    r"[\s\-.]?"
    r"(\d{4})"
    r"(?!\d)"
)

_EMERGENCY_RE = re.compile(
    r"(?i)(?:emergencia|tel(?:efono)?\s*(?:de\s+)?emergencia|otro\s+numero)\s*[:\-]?\s*"
    r"((?:\+?\s*503[\s\-.]*)?\d{4}[\s\-.]?\d{4})"
)

_ADDR_HINT_RE = re.compile(
    r"(?i)\b(calle|colonia|residencial|urbanizacion|urbanización|canton|cantón|"
    r"pasaje|avenida|av\.|final|km\.?|poligono|polígono|departamento|municipio|"
    r"barrio|lotificacion|lotificación|condominio|sendero|boulevard|blvd\.?|"
    r"casa\b|apto\.?|apartamento|local\b|etapa|bloque|pol\b)\b"
)

_NAME_LABEL_RE = re.compile(r"(?i)^(nombre|cliente)\s*[:\-]\s*")
_ORDER_NUM_RE = re.compile(r"^\s*\d{1,2}[\.\)]\s*")


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


def _phone_digits(raw: str) -> str:
    return clean_phone(raw or "")


def _find_phone_matches(text: str) -> list[re.Match[str]]:
    """Todas las apariciones de telefono SV en el texto (orden de aparicion)."""
    if not text:
        return []
    out: list[re.Match[str]] = []
    seen: set[tuple[int, int]] = set()
    for m in _PHONE_FIND_RE.finditer(text):
        digits = _phone_digits(m.group(0))
        if len(digits) < 8:
            continue
        # Evitar capturar precios raros / anios pegados: digitos del match ~8-11
        raw_digits = re.sub(r"\D", "", m.group(0))
        if len(raw_digits) > 11:
            continue
        key = (m.start(), m.end())
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def _extract_phone(text: str) -> str:
    matches = _find_phone_matches(text)
    if not matches:
        return ""
    return _phone_digits(matches[0].group(0))


def _strip_phones(text: str, *, skip_emergency_labels: bool = False) -> str:
    """Quita numeros de telefono del texto para que no contaminen otros campos."""
    if not text:
        return ""
    matches = _find_phone_matches(text)
    if not matches:
        return text
    # Recortar de atras hacia adelante
    out = text
    for m in reversed(matches):
        if skip_emergency_labels:
            pre = out[max(0, m.start() - 40) : m.start()]
            if re.search(
                r"(?i)(?:emergencia|otro\s+numero|tel(?:efono)?\s*(?:de\s+)?emergencia)\s*[:\-]?\s*$",
                pre,
            ):
                continue
        out = out[: m.start()] + " " + out[m.end() :]
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" *\n *", "\n", out)
    return out.strip(" \t-,/")


def _is_phone_only_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if not _find_phone_matches(s):
        return False
    leftover = _strip_phones(s)
    leftover = re.sub(r"(?i)\b(?:tel(?:efono)?|cel(?:ular)?|whats?app|wp)\b", "", leftover)
    leftover = leftover.strip(" \t-:.,/")
    return len(leftover) <= 2


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
        digits = _phone_digits(m.group(1))
        if digits and digits != main_phone:
            return digits
    # Segundo telefono suelto = emergencia
    phones = [_phone_digits(m.group(0)) for m in _find_phone_matches(text)]
    extra = [p for p in phones if p and p != main_phone]
    if extra:
        return extra[0]
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
            clean = _strip_phones(clean)
            if clean and not _OBS_START.match(clean):
                products.append(clean)
        else:
            kept.append(ln)
    if products:
        return "\n".join(kept), "; ".join(products), obs_tail
    return work, "", (tail_price + (" " + obs_tail if obs_tail else "")).strip()


def _clean_person_name(name: str) -> str:
    name = name.replace("\n", " ")
    name = _ORDER_NUM_RE.sub("", name)
    name = _NAME_LABEL_RE.sub("", name)
    name = _strip_phones(name)
    # Prefijo pais suelto o restos numericos
    name = re.sub(r"(?i)\b(?:\+?503)\b", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -,\t/;")
    cut = _ADDR_HINT_RE.search(name)
    if cut and cut.start() > 8:
        name = name[: cut.start()].strip(" -,")
    # Si quedo basura de direccion corta al inicio tipico
    name = re.sub(r"\s{2,}", " ", name).strip(" -,")
    return name


def _extract_name_before_phone(text: str, phone: str) -> str:
    if phone:
        matches = _find_phone_matches(text)
        if matches:
            name = text[: matches[0].start()]
        else:
            name = text
    else:
        name = text.splitlines()[0] if text.splitlines() else text
    return _clean_person_name(name)


def _extract_name_from_clean(text: str) -> str:
    """Nombre desde texto ya sin telefonos (formato habitual o multilinea)."""
    lines = [ln.strip(" -,\t") for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    first = lines[0]
    # Si la primera linea es solo etiqueta+nombre o nombre corto
    if _is_phone_only_line(first) or _PRICE_RE.fullmatch(first.strip()):
        return ""
    if _ADDR_HINT_RE.search(first) and len(first.split()) > 6:
        # nombre + direccion en misma linea
        cut = _ADDR_HINT_RE.search(first)
        if cut and cut.start() > 8:
            return _clean_person_name(first[: cut.start()])
    return _clean_person_name(first)


def _remove_leading_name(text: str, nombre: str) -> str:
    """Quita el nombre (y numeracion 1.) solo al inicio, conservando la direccion."""
    if not text or not nombre:
        return text or ""
    pat = (
        r"^\s*(?:\d{1,2}[\.\)]\s*)?"
        + re.escape(nombre)
        + r"\s*[,:\-]?\s*"
    )
    out = re.sub(pat, "", text, count=1, flags=re.I)
    if out != text:
        return out.strip(" -,\t")
    # Misma linea: nombre aparece al inicio tras numeracion
    lines = text.splitlines()
    first = lines[0] if lines else text
    first_clean = _ORDER_NUM_RE.sub("", first).strip()
    if first_clean.lower().startswith(nombre.lower()):
        remainder = first_clean[len(nombre) :].strip(" -,\t")
        rest_lines = lines[1:]
        if remainder:
            return "\n".join([remainder, *rest_lines]).strip()
        return "\n".join(rest_lines).strip()
    return text.strip(" -,\t")


def _known_place_spans(blob: str) -> list[tuple[int, int, str, str]]:
    """Lista (start, end, dept, municipio_label) de lugares conocidos en el texto."""
    from sistrack.ubicaciones import (
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


def prune_forza_field_defs(fields: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Quita de la definición de campos los que Forza no usa."""
    out: list[dict[str, Any]] = []
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        key = str(f.get("key") or "").strip()
        if key in FORZA_UNUSED_FIELD_KEYS:
            continue
        out.append(dict(f))
    return out


def clear_forza_unused_record_fields(rec: dict[str, Any]) -> dict[str, Any]:
    """Limpia fecha/grabado/emergencia y restos en texto para Forza."""
    out = dict(rec)
    out["fecha_entrega"] = ""
    out["numero_de_emergencia"] = ""
    out["grabado"] = "No"
    out["mensaje_grabado"] = ""
    # Limpiar restos en producto / indicaciones
    for key in ("producto", "observaciones", "direccion", "punto_referencia", "nombre"):
        val = str(out.get(key) or "")
        if not val:
            continue
        cleaned = re.sub(r"(?i)\s*[|—\-]*\s*Grabado\s*:.*$", "", val).strip(" |—-")
        cleaned = re.sub(r"(?i)\s*\|\s*Emergencia\s*:.*$", "", cleaned).strip(" |")
        cleaned = re.sub(r"(?i)\s*\|\s*PAGADO\s*$", "", cleaned).strip(" |")
        out[key] = cleaned
    return out


def enrich_record_for_platform(
    rec: dict[str, Any], platform: str = "sistrack"
) -> dict[str, Any]:
    """Ajusta campos de ubicación al catálogo de la plataforma de subida."""
    out = dict(rec)
    plat = str(platform or "sistrack").strip().lower()
    direccion = str(out.get("direccion") or "")
    ref = str(out.get("punto_referencia") or out.get("referencia") or "")
    dept = str(out.get("departamento") or "")
    muni = str(out.get("municipio") or "")
    colonia = str(out.get("colonia") or "")

    if plat == "forza":
        from forza.ubicaciones_forza import find_catalog_by_label

        out = clear_forza_unused_record_fields(out)
        # Nombre en UI: cliente; en portal se arma con producto abreviado al subir.
        # Aquí solo limpiamos restos de grabado/emergencia ya hechos.
        direccion = str(out.get("direccion") or "")
        ref = str(out.get("punto_referencia") or out.get("referencia") or "")
        dept = str(out.get("departamento") or "")
        muni = str(out.get("municipio") or "")
        colonia = str(out.get("colonia") or "")

        existing_label = str(out.get("forza_label") or "").strip()
        catalog_hit = find_catalog_by_label(existing_label) if existing_label else None
        if catalog_hit is None and colonia and "," in colonia:
            catalog_hit = find_catalog_by_label(colonia)

        if catalog_hit is not None:
            # Respetar selección manual del desplegable / label ya validado
            out["colonia"] = catalog_hit.colonia or colonia
            out["municipio"] = catalog_hit.municipio or muni
            out["departamento"] = catalog_hit.departamento or dept
            out["forza_label"] = catalog_hit.label
        else:
            decoded = decipher_forza_fields(
                direccion=direccion,
                referencia=ref if ref.lower() != "sin referencia" else "",
                departamento=dept,
                municipio=muni,
                colonia=colonia,
            )
            if decoded.get("colonia"):
                out["colonia"] = decoded["colonia"]
            if decoded.get("municipio"):
                out["municipio"] = decoded["municipio"]
            if decoded.get("departamento"):
                out["departamento"] = decoded["departamento"]
            if decoded.get("forza_label"):
                out["forza_label"] = decoded["forza_label"]
        # Avisos: sin poblado claro
        warnings = list(out.get("warnings") or [])
        if not out.get("colonia"):
            if "Poblado Forza dudoso" not in warnings:
                warnings.append("Poblado Forza dudoso")
            out["incomplete"] = True
        elif "Ubicacion dudosa" in warnings and out.get("forza_label"):
            warnings = [w for w in warnings if w != "Ubicacion dudosa"]
            out["location_uncertain"] = False
        out["warnings"] = warnings
        out["incomplete"] = bool(warnings)
        out["location_uncertain"] = "Ubicacion dudosa" in warnings or (
            "Poblado Forza dudoso" in warnings
        )
    else:
        # Express/Sistrack: depto + municipio (distrito); colonia no aplica
        if not dept or not muni:
            d2, m2 = infer_location(direccion, ref)
            out["departamento"] = dept or d2
            out["municipio"] = muni or m2
        out.pop("forza_label", None)
    return out


def parse_order_text(
    text: str,
    default_delivery: str = "",
    platform: str | None = None,
) -> list[dict[str, Any]]:
    """Parsea uno o varios pedidos desde texto libre (una o varias lineas).

    Formato habitual: nombre, telefono, direccion, productos, total, comentarios.
    El telefono suele pegarse al nombre o a la direccion; se extrae y se limpia
    de los demas campos para no mezclarlos.

    platform: si es "forza", descifra poblado/municipio/depto con catálogo Forza.
    """
    blocks = _split_blocks(text)
    records: list[dict[str, Any]] = []
    for raw_block in blocks:
        # Conservar saltos de linea (formato habitual); solo colapsar espacios horizontales
        block = re.sub(r"[ \t]+", " ", raw_block.replace("\r\n", "\n")).strip()
        if not block:
            continue

        telefono = _extract_phone(block)
        precio = _extract_price(block) or "0"
        plat = str(platform or "").strip().lower()
        use_forza = plat == "forza"
        if use_forza:
            grabado, mensaje, emergencia = "No", "", ""
        else:
            grabado, mensaje = _extract_engraving(block)
            emergencia = _extract_emergency(block, telefono)
        pagado = _extract_paid(block)
        obs = _extract_observations(block) or DEFAULT_OBSERVATIONS

        # Texto sin telefonos: evita que el numero contamine nombre/dir/producto
        clean_block = _strip_phones(block)
        # Quitar lineas que solo eran el telefono
        clean_lines = [
            ln.strip(" -,\t")
            for ln in clean_block.splitlines()
            if ln.strip() and not _is_phone_only_line(ln)
        ]
        clean_block = "\n".join(clean_lines).strip()

        nombre = _extract_name_before_phone(block, telefono)
        if not nombre:
            nombre = _extract_name_from_clean(clean_block)

        # Resto = bloque limpio sin el nombre al inicio (no borrar la unica linea)
        rest = _remove_leading_name(clean_block, nombre)

        before_prod, producto, _after = _find_product_span(rest)
        if not producto:
            _, producto, _ = _find_product_span(clean_block)
        producto = _PRICE_RE.sub("", producto or "").strip(" -,\t")
        producto = _strip_phones(producto)
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
                # Evitar restos de "$" sueltos
                piece = ln.strip(" -,\t$")
                if piece and piece != "$":
                    prod_lines.append(piece)
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

        addr_blob = _strip_phones(addr_blob)
        # Si el nombre quedo al inicio de la direccion, recortar
        if nombre and addr_blob.lower().startswith(nombre.lower()):
            addr_blob = addr_blob[len(nombre) :].strip(" -,\t")

        direccion, ref, dept, muni = _split_address_location(addr_blob)
        if not dept or not muni:
            d2, m2 = infer_location(direccion or addr_blob, ref)
            dept = dept or d2
            muni = muni or m2

        # Limpieza final: nunca dejar digitos de telefono en campos de texto
        nombre = _strip_phones(nombre)
        direccion = _strip_phones(direccion)
        ref = _strip_phones(ref) if ref else ref
        producto = _strip_phones(producto)
        obs = _strip_phones(obs, skip_emergency_labels=True)

        if pagado == "Si":
            precio = "0"

        if not nombre and not telefono:
            continue

        if nombre:
            nombre = _ORDER_NUM_RE.sub("", nombre).strip()

        entrega = "" if use_forza else (parse_natural_delivery_date(block) or default_delivery)
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

        rec = {
            "nombre": nombre,
            "telefono": telefono,
            "departamento": dept,
            "municipio": muni,
            "colonia": extract_colonia(direccion, ref) or "",
            "direccion": direccion,
            "punto_referencia": ref or "Sin referencia",
            "producto": producto or "Producto",
            "grabado": grabado,
            "mensaje_grabado": mensaje,
            "precio": precio,
            "pagado": pagado,
            "devolucion": "No",
            "fecha_entrega": entrega,
            "observaciones": obs,
            "numero_de_emergencia": emergencia,
            "peso": "1",
            "payment_type": payment,
            "incomplete": bool(warnings),
            "warnings": warnings,
            "location_uncertain": "Ubicacion dudosa" in warnings,
            "upload_status": "pending",
            "upload_error": "",
        }
        if platform:
            rec = enrich_record_for_platform(rec, platform)
        records.append(rec)
    return records
