"""Contrato JSON que devuelve el análisis de un recibo.

Este mismo formato es el que Flutter muestra al usuario para revisar, el que
Spring Boot guarda en MongoDB (colección ``recibos``) y el que consume el
modelo de minería de datos. Ver ``docs/CONTRATO_JSON.md``.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


class TipoServicio(str, Enum):
    ENERGIA = "ENERGIA"
    AGUA = "AGUA"
    GAS = "GAS"


class Empresa(str, Enum):
    AFINIA = "AFINIA"
    AGUAS_DE_CARTAGENA = "AGUAS_DE_CARTAGENA"
    SURTIGAS = "SURTIGAS"


class TipoIdentificador(str, Enum):
    NIC = "NIC"
    POLIZA = "POLIZA"
    CONTRATO = "CONTRATO"


EMPRESA_A_SERVICIO = {
    Empresa.AFINIA: TipoServicio.ENERGIA,
    Empresa.AGUAS_DE_CARTAGENA: TipoServicio.AGUA,
    Empresa.SURTIGAS: TipoServicio.GAS,
}

EMPRESA_A_IDENTIFICADOR = {
    Empresa.AFINIA: TipoIdentificador.NIC,
    Empresa.AGUAS_DE_CARTAGENA: TipoIdentificador.POLIZA,
    Empresa.SURTIGAS: TipoIdentificador.CONTRATO,
}

EMPRESA_A_UNIDAD = {
    Empresa.AFINIA: "kWh",
    Empresa.AGUAS_DE_CARTAGENA: "m3",
    Empresa.SURTIGAS: "m3",
}


class Identificador(BaseModel):
    tipo: TipoIdentificador
    valor: str | None = None


class ConsumoMensual(BaseModel):
    periodo: str = Field(description="Mes del consumo en formato YYYY-MM")
    consumo: float | None = None
    esActual: bool = Field(False, description="True para el mes facturado en este recibo")


class ConceptoValor(BaseModel):
    concepto: str
    valor: int


class ReciboExtraido(BaseModel):
    tipoServicio: TipoServicio
    empresa: Empresa
    identificador: Identificador
    numeroFactura: str | None = None
    titular: str | None = None
    direccion: str | None = None
    barrio: str | None = None
    estrato: int | None = None
    periodoFacturado: str | None = Field(None, description="Mes facturado YYYY-MM")
    periodoInicio: date | None = None
    periodoFin: date | None = None
    fechaEmision: date | None = None
    fechaLimitePago: date | None = None
    fechaSuspension: date | None = Field(None, description="Fecha de corte/suspensión")
    valorTotal: int | None = Field(None, description="Total a pagar en pesos")
    unidadConsumo: str
    consumoActual: float | None = None
    lecturaAnterior: float | None = None
    lecturaActual: float | None = None
    diasFacturados: int | None = None
    historialConsumos: list[ConsumoMensual] = Field(default_factory=list)
    conceptos: list[ConceptoValor] = Field(
        default_factory=list, description="Desglose del total (aseo, brilla, energía...)"
    )


class ResultadoAnalisis(BaseModel):
    recibo: ReciboExtraido
    confianza: dict[str, float] = Field(
        default_factory=dict,
        description="Confianza 0..1 por campo. Menor a 0.6 se resalta en la app para revisar.",
    )
    camposFaltantes: list[str] = Field(default_factory=list)
    advertencias: list[str] = Field(default_factory=list)
    tiempoProcesamientoMs: int = 0
    versionExtractor: str = "1.0.0"


class ErrorRespuesta(BaseModel):
    codigo: str
    mensaje: str
