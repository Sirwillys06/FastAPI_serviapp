"""Servicio de ServiApp para analizar fotos de recibos de servicios públicos.

Ejecutar en desarrollo (accesible desde el celular en la misma red Wi-Fi):

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import recibos
from .core.config import get_settings
from .schemas.recibo import ResultadoAnalisis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="ServiApp - Análisis de recibos",
    description=(
        "Recibe la foto de un recibo de Afinia, Aguas de Cartagena o Surtigas, la procesa "
        "con OpenCV + Tesseract OCR y devuelve los datos en JSON."
    ),
    version=ResultadoAnalisis.model_fields["versionExtractor"].default,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.exception_handler(RequestValidationError)
async def _error_validacion(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": {"codigo": "SOLICITUD_INVALIDA", "mensaje": "Solicitud inválida.", "errores": exc.errors()}},
    )


@app.exception_handler(Exception)
async def _error_inesperado(_: Request, exc: Exception) -> JSONResponse:
    logging.getLogger(__name__).exception("Error inesperado analizando el recibo", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "codigo": "ERROR_INTERNO",
                "mensaje": "Ocurrió un error procesando la foto. Intenta con otra foto o más tarde.",
            }
        },
    )


@app.get("/health", tags=["estado"])
def health() -> dict:
    settings = get_settings()
    return {"estado": "ok", "tesseract": settings.tesseract_cmd is not None, "version": app.version}


app.include_router(recibos.router)
