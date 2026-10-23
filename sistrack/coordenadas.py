# -*- coding: utf-8 -*-
"""Extrae lat/lng, plus codes y URLs de Google Maps para el mapa de Express."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote
from urllib.request import Request, urlopen

# El Salvador (aprox.) — sirve para ordenar lat,lng vs lng,lat
_SV_LAT = (12.0, 14.8)
_SV_LNG = (-90.6, -87.3)

_MAPS_URL_RE = re.compile(
    r"https?://(?:maps\.app\.goo\.gl/\S+|goo\.gl/maps/\S+|"
    r"(?:www\.)?google\.[^\s/]+/maps\S+|maps\.google\.[^\s]+)",
    re.I,
)
_AT_RE = re.compile(r"@(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)")
_Q_RE = re.compile(
    r"[?&](?:q|query|ll|destination)=(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)",
    re.I,
)
_BANG_RE = re.compile(r"!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)")
_PAIR_RE = re.compile(
    r"(?<!\d)(-?\d{1,2}\.\d+)\s*[,;]\s*(-?\d{1,3}\.\d+)(?!\d)"
)
_PLUS_RE = re.compile(
    r"\b([23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3})\b",
    re.I,
)


def _in_sv(lat: float, lng: float) -> bool:
    return _SV_LAT[0] <= lat <= _SV_LAT[1] and _SV_LNG[0] <= lng <= _SV_LNG[1]


def _as_pair(a: str, b: str) -> tuple[float, float] | None:
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if _in_sv(x, y):
        return x, y
    if _in_sv(y, x):
        return y, x
    return None


def _fmt(pair: tuple[float, float]) -> str:
    lat, lng = pair
    return f"{lat:.7f}".rstrip("0").rstrip(".") + "," + f"{lng:.7f}".rstrip("0").rstrip(".")


def parse_latlng(text: str) -> tuple[float, float] | None:
    """Devuelve (lat, lng) si el texto ya trae coordenadas numéricas."""
    raw = unquote(text or "")
    for rx in (_BANG_RE, _AT_RE, _Q_RE, _PAIR_RE):
        m = rx.search(raw)
        if m:
            pair = _as_pair(m.group(1), m.group(2))
            if pair:
                return pair
    return None


def extract_maps_url(text: str) -> str:
    m = _MAPS_URL_RE.search(text or "")
    return (m.group(0).rstrip(").,;]") if m else "").strip()


def extract_plus_code(text: str) -> str:
    m = _PLUS_RE.search(text or "")
    return (m.group(1) if m else "").strip()


def extract_coordenadas(text: str) -> str:
    """Valor para el campo coordenadas: lat,lng | plus code | URL de Maps."""
    raw = (text or "").strip()
    if not raw:
        return ""
    pair = parse_latlng(raw)
    if pair:
        return _fmt(pair)
    plus = extract_plus_code(raw)
    if plus:
        return plus
    url = extract_maps_url(raw)
    if url:
        return url
    return ""


def strip_coordenadas_blob(text: str) -> str:
    """Quita URLs de Maps, pares lat/lng y plus codes del texto libre."""
    s = text or ""
    s = _MAPS_URL_RE.sub(" ", s)
    s = _BANG_RE.sub(" ", s)
    s = _AT_RE.sub(" ", s)
    s = _PAIR_RE.sub(" ", s)
    s = _PLUS_RE.sub(" ", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip(" \t,;-|")


def expand_maps_url(url: str, timeout: float = 8.0) -> tuple[float, float] | None:
    """Sigue redirecciones de maps.app.goo.gl para sacar @lat,lng."""
    u = (url or "").strip()
    if not u.startswith("http"):
        return None
    already = parse_latlng(u)
    if already:
        return already
    try:
        req = Request(
            u,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Sistrack/1.0)"},
        )
        with urlopen(req, timeout=timeout) as resp:
            final = resp.geturl() or u
        return parse_latlng(final) or parse_latlng(unquote(final))
    except Exception:
        return None


def resolve_map_query(coordenadas: str) -> dict[str, Any]:
    """Prepara lat/lng y texto de búsqueda para el mapa Leaflet."""
    raw = (coordenadas or "").strip()
    out: dict[str, Any] = {"query": "", "lat": None, "lng": None, "raw": raw}
    if not raw:
        return out
    pair = parse_latlng(raw)
    if not pair and raw.startswith("http"):
        pair = expand_maps_url(raw)
    if pair:
        out["lat"], out["lng"] = pair
        out["query"] = _fmt(pair)
        return out
    out["query"] = raw
    return out
