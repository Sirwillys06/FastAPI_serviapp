"""Predicción de consumo a partir del perfil y recibos guardados en Spring."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path

import httpx
import joblib
import numpy as np
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ...core.config import get_settings

router = APIRouter(prefix="/api/predicciones", tags=["predicciones"])
_ARTIFACTS = Path(__file__).resolve().parents[2] / "ml_artifacts"


class SolicitudPrediccion(BaseModel):
    usuarioId: str = Field(min_length=1)


class DatosUsuario(BaseModel):
    estrato: int
    tipoPredio: str
    habitantes: int


class ReciboHistorico(BaseModel):
    reciboId: str
    periodo: str
    consumo: float
    unidad: str


class HistoricoSpring(BaseModel):
    usuarioId: str
    servicio: str
    cantidadMesesSolicitados: int
    datosUsuario: DatosUsuario
    historialRecibos: list[ReciboHistorico]


@lru_cache
def _cargar_modelo(servicio: str):
    """Carga una vez los artefactos exportados desde el notebook de Colab."""
    try:
        import tensorflow as tf
    except ImportError as exc:
        raise RuntimeError("TensorFlow no está instalado en FastAPI") from exc
    try:
        modelo = tf.keras.models.load_model(_ARTIFACTS / f"modelo_{servicio}.keras")
        return (
            modelo,
            joblib.load(_ARTIFACTS / f"scaler_x_{servicio}.pkl"),
            joblib.load(_ARTIFACTS / f"scaler_y_{servicio}.pkl"),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Falta el artefacto del modelo para {servicio}") from exc


async def _obtener_historico(usuario_id: str, servicio: str, meses: int) -> HistoricoSpring:
    settings = get_settings()
    if not settings.ml_internal_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ML_INTERNAL_API_KEY no está configurada")
    try:
        async with httpx.AsyncClient(timeout=settings.spring_timeout_seconds) as cliente:
            respuesta = await cliente.get(
                f"{settings.spring_boot_url}/api/internal/ml/historico-consumo",
                params={"usuarioId": usuario_id, "servicio": servicio.upper(), "meses": meses},
                headers={"X-Internal-Api-Key": settings.ml_internal_api_key},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No fue posible conectar con Spring Boot") from exc
    if respuesta.is_error:
        try:
            detalle = respuesta.json().get("detail", respuesta.text)
        except ValueError:
            detalle = respuesta.text
        raise HTTPException(respuesta.status_code, detalle)
    return HistoricoSpring.model_validate(respuesta.json())


def _periodo_siguiente(periodo: str) -> str:
    try:
        anio, mes = map(int, periodo.split("-"))
        if mes == 12:
            return f"{anio + 1}-01"
        return f"{anio}-{mes + 1:02d}"
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Periodo de recibo inválido") from exc


def _predecir(historial: HistoricoSpring, servicio: str) -> dict:
    consumos = [r.consumo for r in historial.historialRecibos]
    if len(consumos) < 3:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Se necesitan al menos tres recibos")
    ultimo = historial.historialRecibos[-1]
    periodo_estimado = _periodo_siguiente(ultimo.periodo)
    mes_objetivo = int(periodo_estimado[-2:])
    datos = historial.datosUsuario
    entrada = np.array([[
        datos.estrato,
        datos.habitantes,
        1 if datos.tipoPredio.upper() == "CASA" else 0,
        np.sin(2 * np.pi * mes_objetivo / 12.0),
        np.cos(2 * np.pi * mes_objetivo / 12.0),
        *consumos[-3:],
    ]], dtype=float)
    modelo, scaler_x, scaler_y = _cargar_modelo(servicio)
    prediccion_escalada = modelo.predict(scaler_x.transform(entrada), verbose=0)
    consumo = max(0.0, round(float(scaler_y.inverse_transform(prediccion_escalada)[0][0]), 1))
    return {
        "usuarioId": historial.usuarioId,
        "servicio": servicio.upper(),
        "ventanaMesesSolicitada": historial.cantidadMesesSolicitados,
        "ventanaUsadaPorModelo": 3,
        "prediccionSiguienteMes": {
            "periodoEstimado": periodo_estimado,
            "consumoEstimado": consumo,
            "unidad": ultimo.unidad,
        },
        "historicoAnalizado": [r.model_dump(include={"periodo", "consumo"}) for r in historial.historialRecibos],
        "metadatosModelo": {"versionModelo": f"{servicio}_tensorflow_v1", "algoritmo": "TensorFlow Sequential"},
    }


@router.post("/{servicio}/meses{meses}")
async def predecir(servicio: str, meses: int, solicitud: SolicitudPrediccion) -> dict:
    servicio = servicio.lower()
    if servicio not in {"energia", "agua"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "servicio debe ser energia o agua")
    if meses not in {3, 5}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "meses debe ser 3 o 5")
    historico = await _obtener_historico(solicitud.usuarioId, servicio, meses)
    try:
        return await asyncio.to_thread(_predecir, historico, servicio)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
