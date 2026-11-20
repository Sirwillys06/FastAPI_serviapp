"""Predicción de consumo a partir del modelo oficial ServiApp_LSTM con fiabilidad y datos de Spring Boot."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path
import unicodedata

import httpx
import joblib
import numpy as np
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ...core.config import get_settings

router = APIRouter(prefix="/api/predicciones", tags=["predicciones"])
_ARTIFACTS = Path(__file__).resolve().parents[2] / "ml_artifacts"

# Mapeo de unidades oficiales por servicio
UNIDADES_SERVICIO = {
    "AGUA": "m³",
    "ENERGIA": "kWh",
    "GAS": "m³",
}

# Codificación One-Hot oficial
ONE_HOT_SERVICIO = {
    "AGUA": [1.0, 0.0, 0.0],
    "ENERGIA": [0.0, 1.0, 0.0],
    "GAS": [0.0, 0.0, 1.0],
}

# Fiabilidad del modelo por servicio obtenida en la fase de evaluación (R² del conjunto de prueba)
FIABILIDAD_MODELO = {
    "AGUA": 95.38,
    "ENERGIA": 90.89,
    "GAS": 89.38,
}


class SolicitudPrediccion(BaseModel):
    usuarioId: str = Field(min_length=1)


class SolicitudPrediccionDirecta(BaseModel):
    servicio: str
    consumos_anteriores: list[float] = Field(min_length=5, max_length=5)
    estrato: int = Field(ge=1, le=6)
    habitantes: int = Field(ge=1)


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


def normalizar_servicio(servicio: str) -> str:
    """Normaliza el nombre del servicio eliminando tildes y espacios."""
    if not servicio:
        return ""
    texto = unicodedata.normalize("NFD", servicio)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.strip().upper()


@lru_cache
def _cargar_modelo_oficial():
    """Carga en memoria el modelo LSTM oficial y sus escaladores joblib."""
    try:
        import tensorflow as tf
    except ImportError as exc:
        raise RuntimeError("TensorFlow no está instalado en FastAPI") from exc

    modelo_path = _ARTIFACTS / "modelo_lstm_mejor.keras"
    escaladores_consumo_path = _ARTIFACTS / "escaladores_consumo.joblib"
    escalador_contexto_path = _ARTIFACTS / "escalador_contexto.joblib"

    if not modelo_path.exists():
        raise RuntimeError(f"Falta el modelo oficial: {modelo_path.name}")
    if not escaladores_consumo_path.exists():
        raise RuntimeError(f"Falta el escalador de consumo: {escaladores_consumo_path.name}")
    if not escalador_contexto_path.exists():
        raise RuntimeError(f"Falta el escalador de contexto: {escalador_contexto_path.name}")

    try:
        modelo = tf.keras.models.load_model(modelo_path, compile=False)
        escaladores_consumo = joblib.load(escaladores_consumo_path)
        escalador_contexto = joblib.load(escalador_contexto_path)
        return modelo, escaladores_consumo, escalador_contexto
    except Exception as exc:
        raise RuntimeError(f"Error cargando artefactos del modelo oficial: {exc}") from exc


async def _obtener_historico(usuario_id: str, servicio: str, meses: int = 5) -> HistoricoSpring:
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
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Periodo de recibo inválido") from exc


def ejecutar_inferencia_lstm(
    servicio: str,
    consumos: list[float],
    estrato: int,
    habitantes: int,
) -> tuple[float, float]:
    """Aplica el pipeline oficial del modelo LSTM sobre los 5 consumos cronológicos y contexto."""
    srv = normalizar_servicio(servicio)
    if srv not in ONE_HOT_SERVICIO:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Servicio inválido: {servicio}")

    if len(consumos) != 5:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Se requieren exactamente 5 consumos (recibidos {len(consumos)})")

    if any(c < 0 or not np.isfinite(c) for c in consumos):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Los consumos deben ser valores finitos no negativos")

    if not (1 <= estrato <= 6):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Estrato inválido: {estrato}. Debe estar entre 1 y 6")

    if habitantes <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Habitantes inválido: {habitantes}. Debe ser mayor a 0")

    modelo, escaladores_consumo, escalador_contexto = _cargar_modelo_oficial()

    scaler_c = escaladores_consumo[srv]
    # Escalar consumos (5, 1) y luego dar forma de tensor (1, 5, 1)
    X_hist = scaler_c.transform(np.array(consumos, dtype=float).reshape(-1, 1)).astype(np.float32).reshape(1, 5, 1)

    # Escalar contexto (estrato, habitantes) sin requerir pandas -> shape (1, 2)
    # StandardScaler aplica: (X - mean_) / scale_
    ctx_vals = np.array([[float(estrato), float(habitantes)]], dtype=np.float32)
    if hasattr(escalador_contexto, "mean_") and hasattr(escalador_contexto, "scale_"):
        X_ctx = (ctx_vals - escalador_contexto.mean_) / escalador_contexto.scale_
    else:
        X_ctx = escalador_contexto.transform(ctx_vals).astype(np.float32)

    # One-hot del servicio -> shape (1, 3)
    X_srv = np.array([ONE_HOT_SERVICIO[srv]], dtype=np.float32)

    # Predicción
    pred_scaled = modelo.predict({"historial": X_hist, "contexto": X_ctx, "servicio": X_srv}, verbose=0)

    # Inverse transform
    pred = scaler_c.inverse_transform(pred_scaled)
    consumo_estimado = max(0.0, round(float(pred[0][0]), 2))
    porcentaje_fiabilidad = FIABILIDAD_MODELO.get(srv, 90.0)

    return consumo_estimado, porcentaje_fiabilidad


def _predecir_con_historial_spring(historial: HistoricoSpring, servicio_nom: str) -> dict:
    consumos = [r.consumo for r in historial.historialRecibos]
    if len(consumos) != 5:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"El modelo oficial LSTM requiere exactamente 5 meses de consumo (disponibles {len(consumos)})",
        )

    srv = normalizar_servicio(servicio_nom)
    datos = historial.datosUsuario
    consumo_estimado, fiabilidad_pct = ejecutar_inferencia_lstm(
        srv,
        consumos,
        datos.estrato,
        datos.habitantes,
    )

    ultimo = historial.historialRecibos[-1]
    periodo_estimado = _periodo_siguiente(ultimo.periodo)
    unidad = UNIDADES_SERVICIO.get(srv, ultimo.unidad or "unidad")

    return {
        "usuarioId": historial.usuarioId,
        "servicio": srv,
        "ventanaMesesSolicitada": historial.cantidadMesesSolicitados,
        "ventanaUsadaPorModelo": 5,
        "prediccionSiguienteMes": {
            "periodoEstimado": periodo_estimado,
            "consumoEstimado": consumo_estimado,
            "unidad": unidad,
        },
        "porcentajeFiabilidad": fiabilidad_pct,
        "historicoAnalizado": [r.model_dump(include={"periodo", "consumo"}) for r in historial.historialRecibos],
        "metadatosModelo": {
            "versionModelo": "ServiApp_LSTM_Final",
            "algoritmo": "LSTM Red Neuronal Recurrente",
        },
    }


# =====================================================================
# Endpoints de la API
# =====================================================================

@router.post("/predecir")
async def predecir_directo(solicitud: SolicitudPrediccionDirecta) -> dict:
    """Endpoint directo según contrato oficial del documento."""
    srv = normalizar_servicio(solicitud.servicio)
    if srv not in ONE_HOT_SERVICIO:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Servicio inválido: {solicitud.servicio}")

    try:
        consumo_estimado, fiabilidad_pct = await asyncio.to_thread(
            ejecutar_inferencia_lstm,
            srv,
            solicitud.consumos_anteriores,
            solicitud.estrato,
            solicitud.habitantes,
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return {
        "servicio": srv,
        "consumo_estimado": consumo_estimado,
        "unidad": UNIDADES_SERVICIO.get(srv, "unidad"),
        "porcentajeFiabilidad": fiabilidad_pct,
    }


@router.post("/{servicio}")
async def predecir_por_usuario(servicio: str, solicitud: SolicitudPrediccion) -> dict:
    """Ruta general por usuario: obtiene los 5 meses de Spring Boot y genera la predicción LSTM."""
    srv = normalizar_servicio(servicio)
    if srv not in ONE_HOT_SERVICIO:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "servicio debe ser ENERGIA, AGUA o GAS")

    historico = await _obtener_historico(solicitud.usuarioId, srv, meses=5)
    try:
        return await asyncio.to_thread(_predecir_con_historial_spring, historico, srv)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.post("/{servicio}/meses{meses}")
async def predecir_por_usuario_con_meses(servicio: str, meses: int, solicitud: SolicitudPrediccion) -> dict:
    """Ruta retrocompatible con la prueba anterior. Requiere meses=5 para el modelo oficial LSTM."""
    srv = normalizar_servicio(servicio)
    if srv not in ONE_HOT_SERVICIO:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "servicio debe ser ENERGIA, AGUA o GAS")

    if meses != 5:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"El modelo oficial LSTM de ServiApp opera exclusivamente con ventana de 5 meses (se solicitó meses={meses}).",
        )

    historico = await _obtener_historico(solicitud.usuarioId, srv, meses=5)
    try:
        return await asyncio.to_thread(_predecir_con_historial_spring, historico, srv)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc



