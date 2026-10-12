"""Endpoints de análisis de recibos."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from ...core.config import get_settings
from ...schemas.recibo import (
    EMPRESA_A_IDENTIFICADOR,
    EMPRESA_A_SERVICIO,
    EMPRESA_A_UNIDAD,
    Empresa,
    ErrorRespuesta,
    ResultadoAnalisis,
)
from ...services.analizador import EmpresaNoReconocidaError, analizar_recibo
from ...services.imagen import ImagenInvalidaError
from ...services.ocr import OcrNoDisponibleError
from ..deps import verificar_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recibos", tags=["recibos"], dependencies=[Depends(verificar_api_key)])

TIPOS_IMAGEN = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/octet-stream"}


def _error(codigo: str, mensaje: str, http: int) -> HTTPException:
    return HTTPException(status_code=http, detail={"codigo": codigo, "mensaje": mensaje})


@router.post(
    "/analizar",
    response_model=ResultadoAnalisis,
    responses={
        400: {"model": ErrorRespuesta},
        413: {"model": ErrorRespuesta},
        422: {"model": ErrorRespuesta},
        503: {"model": ErrorRespuesta},
    },
    summary="Analiza la foto de un recibo y extrae sus datos",
)
async def analizar(
    imagen: UploadFile = File(..., description="Foto del recibo (JPG o PNG)"),
    empresa: Empresa | None = Form(None, description="Opcional: fuerza la empresa si la detección falla"),
) -> ResultadoAnalisis:
    settings = get_settings()
    if imagen.content_type and imagen.content_type.lower() not in TIPOS_IMAGEN:
        raise _error("TIPO_NO_SOPORTADO", "Envía una foto en formato JPG o PNG.", status.HTTP_400_BAD_REQUEST)

    contenido = await imagen.read(settings.max_upload_bytes + 1)
    if len(contenido) > settings.max_upload_bytes:
        raise _error(
            "IMAGEN_MUY_GRANDE",
            f"La imagen supera {settings.max_upload_bytes // (1024 * 1024)} MB.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    if not contenido:
        raise _error("IMAGEN_VACIA", "No se recibió ninguna imagen.", status.HTTP_400_BAD_REQUEST)

    try:
        # El análisis es intensivo en CPU: se ejecuta fuera del event loop
        return await run_in_threadpool(analizar_recibo, contenido, empresa)
    except ImagenInvalidaError as exc:
        raise _error("IMAGEN_INVALIDA", str(exc), status.HTTP_400_BAD_REQUEST) from exc
    except EmpresaNoReconocidaError as exc:
        raise _error("EMPRESA_NO_RECONOCIDA", str(exc), 422) from exc
    except OcrNoDisponibleError as exc:
        raise _error("OCR_NO_DISPONIBLE", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE) from exc


@router.get("/empresas", summary="Empresas soportadas")
def empresas() -> list[dict]:
    return [
        {
            "empresa": e.value,
            "tipoServicio": EMPRESA_A_SERVICIO[e].value,
            "identificador": EMPRESA_A_IDENTIFICADOR[e].value,
            "unidadConsumo": EMPRESA_A_UNIDAD[e],
        }
        for e in Empresa
    ]
