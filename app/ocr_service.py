import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import cv2
import numpy as np
import pytesseract
from pytesseract import Output


TOTAL_OCR_SECONDS = float(os.getenv("OCR_TOTAL_SECONDS", "120"))
PER_PASS_SECONDS = 25
BORDER = 16

# Evita ejecutar varios trabajos pesados a la vez en este proceso.
OCR_LOCK = threading.Lock()


class OcrUnavailableError(RuntimeError):
    pass


class OcrProcessingError(RuntimeError):
    pass


tesseract_path = os.getenv("TESSERACT_CMD")

if tesseract_path:
    pytesseract.pytesseract.tesseract_cmd = tesseract_path


@dataclass
class OcrSession:
    """Presupuesto de tiempo compartido por todas las lecturas de un recibo."""

    started: float = field(default_factory=time.monotonic)
    warnings: list[str] = field(default_factory=list)

    @property
    def deadline(self) -> float:
        return self.started + TOTAL_OCR_SECONDS

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 2)


@contextmanager
def ocr_session():
    if not OCR_LOCK.acquire(blocking=False):
        raise OcrUnavailableError(
            "El lector está procesando otro recibo. "
            "Intenta nuevamente en unos segundos."
        )

    try:
        yield OcrSession()
    finally:
        OCR_LOCK.release()


def _read_pass(
    image: np.ndarray,
    name: str,
    psm: int,
    timeout: float,
    offset_x: int = 0,
    offset_y: int = 0,
    scale: float = 1.0,
) -> dict:
    """
    Ejecuta Tesseract sobre una imagen o recorte.

    Las cajas se devuelven en coordenadas de la imagen preparada:
    se deshace la ampliación (`scale`) y se suma el desplazamiento
    del recorte.
    """
    started = time.monotonic()

    padded = cv2.copyMakeBorder(
        image,
        BORDER,
        BORDER,
        BORDER,
        BORDER,
        cv2.BORDER_CONSTANT,
        value=255,
    )

    data = pytesseract.image_to_data(
        padded,
        lang=os.getenv("OCR_LANG", "spa"),
        config=f"--psm {psm}",
        output_type=Output.DICT,
        timeout=timeout,
    )

    lines = {}
    words = []
    confidences = []

    height, width = image.shape

    for index, raw_word in enumerate(data["text"]):
        word = raw_word.strip()

        if not word:
            continue

        confidence = float(data["conf"][index])

        left = int(data["left"][index]) - BORDER
        top = int(data["top"][index]) - BORDER
        right = left + int(data["width"][index])
        bottom = top + int(data["height"][index])

        # Coordenadas dentro de la imagen, sin el borde añadido.
        x1 = max(0, min(width, left))
        y1 = max(0, min(height, top))
        x2 = max(0, min(width, right))
        y2 = max(0, min(height, bottom))

        if x2 <= x1 or y2 <= y1:
            continue

        line_key = (
            data["page_num"][index],
            data["block_num"][index],
            data["par_num"][index],
            data["line_num"][index],
        )

        lines.setdefault(line_key, []).append(word)

        words.append({
            "text": word,
            "confidence": round(confidence, 2),
            "box": {
                "x": int(round(x1 / scale)) + offset_x,
                "y": int(round(y1 / scale)) + offset_y,
                "width": max(1, int(round((x2 - x1) / scale))),
                "height": max(1, int(round((y2 - y1) / scale))),
            },
        })

        if confidence >= 0:
            confidences.append(confidence)

    text = "\n".join(
        " ".join(line_words)
        for line_words in lines.values()
    )

    mean_confidence = (
        sum(confidences) / len(confidences)
        if confidences
        else None
    )

    return {
        "name": name,
        "psm": psm,
        "text": text,
        "word_count": len(words),
        "mean_confidence": (
            round(mean_confidence, 2)
            if mean_confidence is not None
            else None
        ),
        "seconds": round(time.monotonic() - started, 2),
        "words": words,
    }


def _run(session: OcrSession, image, name, psm, **kwargs) -> dict | None:
    remaining = session.remaining()

    if remaining < 1:
        message = (
            "Se alcanzó el límite de tiempo. "
            "Algunas lecturas no se ejecutaron."
        )

        if message not in session.warnings:
            session.warnings.append(message)

        return None

    try:
        return _read_pass(
            image=image,
            name=name,
            psm=psm,
            timeout=min(PER_PASS_SECONDS, remaining),
            **kwargs,
        )

    except pytesseract.TesseractNotFoundError as error:
        raise OcrUnavailableError(
            "No se encontró Tesseract. Revisa TESSERACT_CMD."
        ) from error

    except pytesseract.TesseractError as error:
        raise OcrProcessingError(
            "Tesseract no pudo ejecutar la lectura. "
            "Verifica su instalación y el idioma OCR_LANG."
        ) from error

    except RuntimeError:
        session.warnings.append(
            f"La lectura '{name}' superó su tiempo permitido."
        )
        return None


# Pasadas de página completa. La variable OCR_PASES (lista separada por
# comas) elige cuáles ejecutar, para medir su aporte con tools/evaluar.py.
#
# Por defecto no se ejecutan los cuadrantes: en los recibos de prueba no
# cambiaron ningún campo y costaban ~5 s por recibo. Las zonas enfocadas
# alrededor de cada etiqueta cumplen mejor esa función. Volver a medir
# con fotos de peor calidad antes de descartarlos del todo.
DEFAULT_PASSES = {
    "full_gray_sparse",
    "full_gray_layout",
    "full_normalized",
    "full_adaptive",
}

FULL_PAGE_JOBS = (
    ("full_gray_sparse", "gray", 11),
    ("full_gray_layout", "gray", 3),
    ("full_normalized", "normalized", 11),
    ("full_adaptive", "adaptive", 11),
)

QUADRANTS = (
    ("upper_left", 0.00, 0.00, 0.58, 0.58),
    ("upper_right", 0.42, 0.00, 1.00, 0.58),
    ("lower_left", 0.00, 0.42, 0.58, 1.00),
    ("lower_right", 0.42, 0.42, 1.00, 1.00),
)


def _enabled_passes() -> set[str]:
    value = os.getenv("OCR_PASES", "").strip()

    if not value:
        return DEFAULT_PASSES

    return {item.strip() for item in value.split(",") if item.strip()}


def read_full_page(session: OcrSession, variants: dict) -> list[dict]:
    enabled = _enabled_passes()
    readings = []

    for name, variant, psm in FULL_PAGE_JOBS:
        if name not in enabled:
            continue

        reading = _run(session, variants[variant], name, psm)

        if reading:
            readings.append(reading)

    gray = variants["gray"]
    height, width = gray.shape

    # Cuadrantes con solapamiento para no depender tanto
    # de la segmentación de toda la página.
    for name, x_start, y_start, x_end, y_end in QUADRANTS:
        if name not in enabled:
            continue

        x1 = int(width * x_start)
        y1 = int(height * y_start)
        x2 = int(width * x_end)
        y2 = int(height * y_end)

        reading = _run(
            session,
            gray[y1:y2, x1:x2],
            name,
            11,
            offset_x=x1,
            offset_y=y1,
        )

        if reading:
            readings.append(reading)

    return readings


def _focus(crop: np.ndarray, variant: str) -> np.ndarray:
    # Texto claro sobre fondo oscuro (franjas de color): se invierte.
    if np.median(crop) < 140:
        crop = 255 - crop

    if variant == "normalized":
        background = cv2.GaussianBlur(crop, (0, 0), sigmaX=15, sigmaY=15)
        crop = cv2.divide(crop, np.maximum(background, 1), scale=255)

    return crop


def read_regions(
    session: OcrSession,
    gray: np.ndarray,
    regions: list[dict],
) -> list[dict]:
    """
    Relee regiones concretas (alrededor de etiquetas, gráficas).

    Cada región: {"name", "box": (x1, y1, x2, y2), "text_height", "reads":
    [(variante, psm, altura_objetivo), ...]}. El recorte se amplía para
    que el texto tenga aproximadamente la altura objetivo (máximo x3).
    Cada lectura queda como una pasada más: "zona:<region>:<variante>".
    """
    readings = []

    for region in regions:
        x1, y1, x2, y2 = region["box"]
        original = gray[y1:y2, x1:x2]

        if original.size == 0:
            continue

        for variant, psm, target_height in region["reads"]:
            scale = round(
                max(1.0, min(3.0, target_height / max(region.get("text_height", target_height), 1))),
                2,
            )
            crop = original

            if scale != 1.0:
                crop = cv2.resize(
                    original,
                    None,
                    fx=scale,
                    fy=scale,
                    interpolation=cv2.INTER_CUBIC,
                )

            reading = _run(
                session,
                _focus(crop, variant),
                f"zona:{region['name']}:{variant}{psm}",
                psm,
                offset_x=x1,
                offset_y=y1,
                scale=scale,
            )

            if reading:
                reading["region"] = {
                    "name": region["name"],
                    "box": {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1},
                    "anchors": region.get("anchors", []),
                }
                readings.append(reading)

    return readings
