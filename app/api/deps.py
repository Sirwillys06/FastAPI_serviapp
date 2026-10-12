"""Dependencias compartidas de la API."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from ..core.config import get_settings


def verificar_api_key(x_api_key: str | None = Header(None)) -> None:
    """Exige la cabecera ``X-API-Key`` solo si ``SERVIAPP_API_KEY`` está configurada."""
    esperada = get_settings().api_key
    if esperada is None:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, esperada):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"codigo": "API_KEY_INVALIDA", "mensaje": "API key inválida o ausente."},
        )
