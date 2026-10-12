"""Orquesta el análisis completo de una foto de recibo."""

from __future__ import annotations

import logging
import time

import cv2

from ..extractores.afinia import ExtractorAfinia
from ..extractores.aguas_cartagena import ExtractorAguasCartagena
from ..extractores.base import ExtractorBase
from ..extractores.surtigas import ExtractorSurtigas
from ..schemas.recibo import Empresa, ResultadoAnalisis
from .codigos import leer_codigos
from .detector import detectar_empresa
from .imagen import preprocesar
from .ocr import PaginaOcr, configurar_tesseract

logger = logging.getLogger(__name__)

EXTRACTORES: dict[Empresa, type[ExtractorBase]] = {
    Empresa.AFINIA: ExtractorAfinia,
    Empresa.AGUAS_DE_CARTAGENA: ExtractorAguasCartagena,
    Empresa.SURTIGAS: ExtractorSurtigas,
}


class EmpresaNoReconocidaError(ValueError):
    pass


def analizar_recibo(contenido: bytes, empresa_forzada: Empresa | None = None) -> ResultadoAnalisis:
    inicio = time.perf_counter()
    configurar_tesseract()

    imagen = preprocesar(contenido)
    gs1, qrs = leer_codigos(imagen.color)
    if gs1 is None:
        # En la foto original (sin reescalar) las barras pueden tener más resolución
        gs1, qrs_original = leer_codigos(imagen.original)
        qrs = qrs or qrs_original

    pagina = PaginaOcr(imagen.gris, base=cv2.cvtColor(imagen.color, cv2.COLOR_BGR2GRAY))

    if empresa_forzada:
        empresa, conf_empresa = empresa_forzada, 1.0
    else:
        empresa, conf_empresa = detectar_empresa(pagina.texto, gs1)
        if empresa is None:
            raise EmpresaNoReconocidaError(
                "No pudimos reconocer la empresa del recibo. Asegúrate de que el logo y "
                "los datos se vean completos, o elige la empresa manualmente."
            )

    extractor = EXTRACTORES[empresa](pagina, gs1, qrs)
    recibo, confianza, faltantes, advertencias = extractor.extraer()
    confianza["empresa"] = conf_empresa

    advertencias = imagen.advertencias + advertencias
    if not imagen.hoja_detectada:
        logger.info("No se detectó el borde de la hoja; se analizó la imagen completa")

    ms = int((time.perf_counter() - inicio) * 1000)
    logger.info("Recibo %s analizado en %d ms (faltantes: %s)", empresa.value, ms, faltantes)
    return ResultadoAnalisis(
        recibo=recibo,
        confianza=confianza,
        camposFaltantes=faltantes,
        advertencias=advertencias,
        tiempoProcesamientoMs=ms,
    )
