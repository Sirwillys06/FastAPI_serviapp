from io import BytesIO
import warnings

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError


MAX_PIXELS = 20_000_000
MAX_SIDE = 3200
TARGET_SIDE = 2800
MAX_UPSCALE = 2.5


class InvalidReceiptImage(ValueError):
    pass


def prepare_receipt_image(content: bytes) -> np.ndarray:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "error",
                Image.DecompressionBombWarning,
            )

            with Image.open(BytesIO(content)) as original:
                if original.format not in {"JPEG", "PNG"}:
                    raise InvalidReceiptImage(
                        "El archivo debe ser una imagen JPEG o PNG."
                    )

                width, height = original.size

                if width * height > MAX_PIXELS:
                    raise InvalidReceiptImage(
                        "La imagen supera los 20 megapíxeles."
                    )

                if min(width, height) < 400:
                    raise InvalidReceiptImage(
                        "La imagen es demasiado pequeña. "
                        "Acércate al recibo y toma otra foto."
                    )

                original.load()
                oriented = ImageOps.exif_transpose(original)

                # Evita que las transparencias se conviertan en fondo negro.
                rgba = oriented.convert("RGBA")
                background = Image.new(
                    "RGBA",
                    rgba.size,
                    (255, 255, 255, 255),
                )
                rgb = Image.alpha_composite(
                    background,
                    rgba,
                ).convert("RGB")

                image = np.array(rgb)

    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise InvalidReceiptImage(
            "No se pudo abrir la imagen de forma segura."
        ) from error

    height, width = image.shape[:2]
    longest_side = max(width, height)

    if longest_side > MAX_SIDE:
        scale = MAX_SIDE / longest_side
        interpolation = cv2.INTER_AREA

    elif longest_side < TARGET_SIDE:
        scale = min(
            TARGET_SIDE / longest_side,
            MAX_UPSCALE,
        )
        interpolation = cv2.INTER_CUBIC

    else:
        scale = 1.0
        interpolation = cv2.INTER_CUBIC

    if scale != 1.0:
        image = cv2.resize(
            image,
            None,
            fx=scale,
            fy=scale,
            interpolation=interpolation,
        )

    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def build_image_variants(gray: np.ndarray) -> dict[str, np.ndarray]:
    # Estima las variaciones amplias de iluminación.
    background = cv2.GaussianBlur(
        gray,
        (0, 0),
        sigmaX=25,
        sigmaY=25,
    )

    # Evita divisiones entre cero en zonas completamente negras.
    background = np.maximum(background, 1)

    normalized = cv2.divide(
        gray,
        background,
        scale=255,
    )

    # Versión alternativa para sombras y fondos irregulares.
    # No sustituye a la imagen gris: algunos detalles pueden perderse.
    adaptive = cv2.adaptiveThreshold(
        normalized,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        51,
        15,
    )

    return {
        "gray": gray,
        "normalized": normalized,
        "adaptive": adaptive,
    }