"""Preprocesamiento de la foto del recibo con OpenCV.

Pasos:
1. Decodificar respetando la orientación EXIF de la cámara.
2. Detectar el contorno de la hoja y corregir la perspectiva.
3. Corregir rotaciones de 90/180/270 grados (OSD de Tesseract).
4. Enderezar inclinaciones pequeñas.
5. Normalizar el tamaño y mejorar el contraste para el OCR.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

ANCHO_OBJETIVO = 2200  # píxeles de ancho con los que trabaja el OCR


class ImagenInvalidaError(ValueError):
    pass


@dataclass
class ImagenProcesada:
    color: np.ndarray  # BGR normalizada (para recortes)
    gris: np.ndarray  # escala de grises mejorada (para OCR)
    original: np.ndarray  # imagen original orientada, sin recortar (para códigos de barras)
    hoja_detectada: bool = False
    rotacion: int = 0
    advertencias: list[str] = field(default_factory=list)

    @property
    def alto(self) -> int:
        return self.gris.shape[0]

    @property
    def ancho(self) -> int:
        return self.gris.shape[1]


def decodificar(contenido: bytes) -> np.ndarray:
    """Convierte los bytes recibidos en una imagen BGR aplicando la rotación EXIF."""
    try:
        pil = Image.open(io.BytesIO(contenido))
        pil = ImageOps.exif_transpose(pil).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - PIL lanza varios tipos
        raise ImagenInvalidaError("El archivo no es una imagen válida.") from exc
    img = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    if min(img.shape[:2]) < 500:
        raise ImagenInvalidaError("La imagen tiene muy poca resolución. Toma la foto más cerca.")
    return img


def _ordenar_esquinas(pts: np.ndarray) -> np.ndarray:
    pts = pts.reshape(4, 2).astype("float32")
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype="float32",
    )


def _buscar_cuadrilatero(img: np.ndarray) -> np.ndarray | None:
    """Busca el contorno de la hoja. Devuelve 4 esquinas en coordenadas de ``img``."""
    alto, ancho = img.shape[:2]
    escala = 900.0 / max(alto, ancho)
    peq = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    gris = cv2.cvtColor(peq, cv2.COLOR_BGR2GRAY)
    gris = cv2.GaussianBlur(gris, (5, 5), 0)
    area_total = peq.shape[0] * peq.shape[1]

    candidatos = []
    # a) Bordes
    bordes = cv2.Canny(gris, 50, 150)
    bordes = cv2.dilate(bordes, np.ones((5, 5), np.uint8), iterations=2)
    candidatos.append(bordes)
    # b) Papel claro sobre fondo más oscuro
    _, claro = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    claro = cv2.morphologyEx(claro, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    candidatos.append(claro)

    mejor = None
    mejor_area = 0.0
    for mascara in candidatos:
        contornos, _ = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(contornos, key=cv2.contourArea, reverse=True)[:5]:
            area = cv2.contourArea(c)
            if area < 0.25 * area_total:
                break
            perimetro = cv2.arcLength(c, True)
            aprox = cv2.approxPolyDP(c, 0.02 * perimetro, True)
            if len(aprox) == 4 and cv2.isContourConvex(aprox) and area > mejor_area:
                mejor, mejor_area = aprox, area

    if mejor is None:
        return None
    # Si el "documento" ocupa prácticamente toda la imagen no hay nada que recortar.
    if mejor_area > 0.97 * area_total:
        return None
    return _ordenar_esquinas(mejor) / escala


def _corregir_perspectiva(img: np.ndarray, esquinas: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = esquinas
    ancho = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    alto = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    destino = np.array([[0, 0], [ancho - 1, 0], [ancho - 1, alto - 1], [0, alto - 1]], dtype="float32")
    matriz = cv2.getPerspectiveTransform(esquinas, destino)
    return cv2.warpPerspective(img, matriz, (ancho, alto), flags=cv2.INTER_CUBIC)


def _rotacion_osd(gris: np.ndarray) -> int:
    """Grados (0/90/180/270) que hay que rotar en sentido horario para enderezar."""
    try:
        escala = 1400.0 / max(gris.shape)
        peq = cv2.resize(gris, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA) if escala < 1 else gris
        osd = pytesseract.image_to_osd(peq, config="--psm 0", output_type=pytesseract.Output.DICT)
        if float(osd.get("orientation_conf", 0)) >= 1.5:
            return int(osd.get("rotate", 0)) % 360
    except pytesseract.TesseractError:
        pass
    except Exception:  # noqa: BLE001 - OSD falla en imágenes con poco texto
        logger.debug("OSD no disponible", exc_info=True)
    return 0


def _rotar(img: np.ndarray, grados: int) -> np.ndarray:
    if grados == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if grados == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if grados == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def _angulo_inclinacion(gris: np.ndarray) -> float:
    """Estima la inclinación del texto (grados) usando las líneas horizontales largas."""
    escala = 1200.0 / gris.shape[1]
    peq = cv2.resize(gris, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    bordes = cv2.Canny(peq, 50, 150)
    lineas = cv2.HoughLinesP(bordes, 1, np.pi / 720, threshold=120, minLineLength=peq.shape[1] // 4, maxLineGap=10)
    if lineas is None:
        return 0.0
    angulos = []
    # Según la versión de OpenCV el arreglo viene como (N, 1, 4) o (N, 4)
    for x1, y1, x2, y2 in np.asarray(lineas).reshape(-1, 4):
        ang = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(ang) < 15:
            angulos.append(ang)
    if len(angulos) < 3:
        return 0.0
    return float(np.median(angulos))


def _enderezar(img: np.ndarray, angulo: float) -> np.ndarray:
    alto, ancho = img.shape[:2]
    matriz = cv2.getRotationMatrix2D((ancho / 2, alto / 2), angulo, 1.0)
    return cv2.warpAffine(img, matriz, (ancho, alto), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _mejorar(gris: np.ndarray) -> np.ndarray:
    """Normaliza la iluminación (sombras de la mano / flash) y sube el contraste."""
    fondo = cv2.medianBlur(cv2.dilate(gris, np.ones((7, 7), np.uint8)), 31)
    normal = cv2.divide(gris, fondo, scale=255)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(normal)


def _nitidez(gris: np.ndarray) -> float:
    return float(cv2.Laplacian(gris, cv2.CV_64F).var())


def preprocesar(contenido: bytes) -> ImagenProcesada:
    original = decodificar(contenido)
    advertencias: list[str] = []

    esquinas = _buscar_cuadrilatero(original)
    img = _corregir_perspectiva(original, esquinas) if esquinas is not None else original

    rotacion = _rotacion_osd(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    img = _rotar(img, rotacion)
    original = _rotar(original, rotacion)

    if esquinas is None:
        try:
            angulo = _angulo_inclinacion(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        except Exception:  # noqa: BLE001 - enderezar es opcional: si falla se sigue sin hacerlo
            logger.warning("No se pudo estimar la inclinación", exc_info=True)
            angulo = 0.0
        if 0.3 < abs(angulo) < 15:
            img = _enderezar(img, angulo)

    escala = ANCHO_OBJETIVO / img.shape[1]
    interp = cv2.INTER_CUBIC if escala > 1 else cv2.INTER_AREA
    img = cv2.resize(img, None, fx=escala, fy=escala, interpolation=interp)

    gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if _nitidez(cv2.resize(gris, None, fx=0.5, fy=0.5)) < 40:
        advertencias.append("La foto se ve borrosa; revisa con cuidado los valores extraídos.")
    gris = _mejorar(gris)

    return ImagenProcesada(
        color=img,
        gris=gris,
        original=original,
        hoja_detectada=esquinas is not None,
        rotacion=rotacion,
        advertencias=advertencias,
    )
