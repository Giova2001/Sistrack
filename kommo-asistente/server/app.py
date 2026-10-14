# -*- coding: utf-8 -*-
"""API local para la extensión Kommo Asistente."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from chatbot import suggest_reply

app = FastAPI(title="Kommo Asistente", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SuggestIn(BaseModel):
    message: str = Field(..., min_length=0)
    context: str = ""


@app.get("/")
def root() -> dict:
    """Evita 404 al abrir http://127.0.0.1:8765/ en el navegador."""
    return {
        "ok": True,
        "service": "kommo-asistente",
        "health": "/health",
        "suggest": "POST /api/suggest",
        "docs": "/docs",
        "uso": "Abre Kommo en Chrome, pulsa KA → Capturar → Analizar",
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "kommo-asistente"}


@app.post("/api/suggest")
def api_suggest(body: SuggestIn) -> dict:
    result = suggest_reply(body.message)
    if body.context.strip():
        result["context_received"] = True
    return result
