"""
Orquestación: imagen → lecturas OCR → recibo estructurado.

Se separa en dos etapas para poder evaluar el parser sin repetir OCR:

* `run_ocr(content)`: todo lo que depende de la imagen y de Tesseract.
  Devuelve un diccionario serializable (se puede guardar en JSON).
* `build_receipt(ocr_result)`: función pura sobre ese diccionario.
"""

from app.image_processing import build_image_variants, prepare_receipt_image
from app.layout import build_lines, words_from_reading
from app.ocr_service import ocr_session, read_full_page, read_regions
from app.receipt_parser import build_receipt, detect_provider
from app.zones import plan_regions


def run_ocr(content: bytes) -> dict:
    gray = prepare_receipt_image(content)

    with ocr_session() as session:
        variants = build_image_variants(gray)
        readings = read_full_page(session, variants)
        base = [reading for reading in readings if reading["text"].strip()]
        provider, _ = detect_provider(base)
        regions = []

        if provider:
            lines_by_reading = {
                reading["name"]: build_lines(words_from_reading(reading))
                for reading in base
            }
            regions = plan_regions(provider, lines_by_reading, gray.shape)
            readings.extend(read_regions(session, gray, regions))

        return {
            "shape": list(gray.shape),
            "readings": readings,
            "regions": [
                {"name": region["name"], "box": list(region["box"]), "text_height": region["text_height"]}
                for region in regions
            ],
            "warnings": session.warnings,
            "elapsed_seconds": session.elapsed(),
        }


def process_receipt(content: bytes, include_words: bool = False) -> dict:
    ocr_result = run_ocr(content)

    if not any(reading["text"].strip() for reading in ocr_result["readings"]):
        return {"sin_texto": True, "advertencias": ocr_result["warnings"]}

    return build_receipt(ocr_result, include_words=include_words)
