"""Lectura del código de barras GS1-128 y los QR de los recibos.

Los tres recibos traen al pie un código de barras con el estándar GS1-128 de
recaudo de servicios públicos en Colombia:

    (415) GLN de la empresa  (8020) referencia de pago
    (3900) valor a pagar     (96) fecha límite (AAAAMMDD, opcional)

Es la fuente más confiable para el **valor total** y la **fecha límite**. Se
intenta primero decodificar el código (zxing-cpp) y, si la foto no tiene
resolución suficiente, se lee el texto impreso debajo de las barras con OCR.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import cv2
import numpy as np

from ..utils.numeros import corregir_digitos

logger = logging.getLogger(__name__)

try:  # zxing-cpp es opcional: si falta, solo se usa el OCR del texto impreso
    import zxingcpp
except ImportError:  # pragma: no cover
    zxingcpp = None

GS = "\x1d"


@dataclass
class DatosGs1:
    gln: str | None = None
    referencia: str | None = None
    valor: int | None = None
    fecha_limite: date | None = None
    fuente: str = ""  # "barras" u "ocr"

    @property
    def util(self) -> bool:
        return self.valor is not None


def _fecha_gs1(texto: str) -> date | None:
    m = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", texto)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parsear_gs1_crudo(texto: str) -> DatosGs1 | None:
    """Interpreta el contenido decodificado del código (con separadores FNC1/GS)."""
    texto = texto.replace("]C1", "").strip()
    if not texto.startswith("415"):
        return None
    datos = DatosGs1(fuente="barras")
    resto = texto
    while resto:
        resto = resto.lstrip(GS)
        if resto.startswith("415"):
            datos.gln, resto = resto[3:16], resto[16:]
        elif resto.startswith("8020"):
            valor, _, resto = resto[4:].partition(GS)
            datos.referencia = valor
        elif re.match(r"390\d", resto):
            valor, _, resto = resto[4:].partition(GS)
            datos.valor = int(valor) if valor.isdigit() else None
        elif resto.startswith("96"):
            valor, _, resto = resto[2:].partition(GS)
            datos.fecha_limite = _fecha_gs1(valor[:8])
        else:
            break
    return datos if datos.util else None


def parsear_gs1_impreso(texto: str) -> DatosGs1 | None:
    """Interpreta el texto legible bajo las barras: ``(415)770...(8020)...(3900)...(96)...``.

    Tolera errores de OCR como ``1415)`` o ``2900)`` en lugar de ``(415)``/``(3900)``.
    """
    t = corregir_digitos(texto.replace(" ", ""))
    pares = re.findall(r"(\d{2,4})\)(\d+)", t)
    if not pares:
        return None
    datos = DatosGs1(fuente="ocr")
    for ia, valor in pares:
        if ia.endswith("415") and len(valor) >= 13:
            datos.gln = valor[:13]
        elif ia.endswith("8020") or ia.endswith("020"):
            datos.referencia = valor
        elif ia.endswith("900") and 6 <= len(valor) <= 15:
            datos.valor = int(valor)
        elif ia.endswith("96") and len(valor) >= 8:
            datos.fecha_limite = _fecha_gs1(valor[:8])
    return datos if datos.util else None


def _leer_con_zxing(img: np.ndarray):
    if zxingcpp is None:
        return []
    try:
        return zxingcpp.read_barcodes(img)
    except Exception:  # noqa: BLE001
        logger.debug("zxing falló", exc_info=True)
        return []


def leer_codigos(img_bgr: np.ndarray) -> tuple[DatosGs1 | None, list[str]]:
    """Busca el código GS1-128 y los QR en la imagen.

    Devuelve los datos GS1 (si se pudieron decodificar) y el texto de los QR.
    """
    gris = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if img_bgr.ndim == 3 else img_bgr
    alto = gris.shape[0]
    intentos = [gris, gris[int(alto * 0.75) :, :]]
    qrs: list[str] = []
    gs1: DatosGs1 | None = None
    for imagen in intentos:
        for factor in (1.0, 1.5):
            if factor != 1.0:
                imagen = cv2.resize(imagen, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
            for resultado in _leer_con_zxing(imagen):
                texto = resultado.text or ""
                formato = resultado.format.name
                if "QR" in formato and texto not in qrs:
                    qrs.append(texto)
                elif gs1 is None:
                    gs1 = parsear_gs1_crudo(texto)
            if gs1:
                return gs1, qrs
    return gs1, qrs


def localizar_barras(gris: np.ndarray) -> tuple[float, float, float, float] | None:
    """Ubica el código de barras lineal más grande usando gradientes (OpenCV).

    Las barras tienen mucho gradiente horizontal y poco vertical. Devuelve la
    caja normalizada (x0, y0, x1, y1) o ``None``.
    """
    alto, ancho = gris.shape[:2]
    escala = 1000.0 / ancho
    peq = cv2.resize(gris, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    gx = cv2.Sobel(peq, cv2.CV_32F, 1, 0, ksize=-1)
    gy = cv2.Sobel(peq, cv2.CV_32F, 0, 1, ksize=-1)
    gradiente = cv2.convertScaleAbs(cv2.subtract(np.abs(gx), np.abs(gy)))
    gradiente = cv2.blur(gradiente, (9, 9))
    _, mascara = cv2.threshold(gradiente, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mascara = cv2.morphologyEx(mascara, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (21, 7)))
    mascara = cv2.erode(mascara, None, iterations=3)
    mascara = cv2.dilate(mascara, None, iterations=3)
    contornos, _ = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mejor = None
    mejor_area = 0
    h_p, w_p = peq.shape[:2]
    for c in contornos:
        x, y, w, h = cv2.boundingRect(c)
        # Un código de barras de recaudo es ancho (>25% de la hoja) y más ancho que alto
        if w < 0.25 * w_p or w < 2.5 * h or h < 0.02 * h_p:
            continue
        if w * h > mejor_area:
            mejor, mejor_area = (x, y, w, h), w * h
    if not mejor:
        return None
    x, y, w, h = mejor
    return (x / w_p, y / h_p, (x + w) / w_p, (y + h) / h_p)
