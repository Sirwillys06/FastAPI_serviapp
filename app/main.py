from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from starlette.concurrency import run_in_threadpool

from app.image_processing import InvalidReceiptImage
from app.ocr_service import OcrProcessingError, OcrUnavailableError
from app.pipeline import process_receipt


app = FastAPI(
    title="ServiApp OCR",
    description="Servicio de lectura y extracción de datos de recibos públicos.",
    version="0.2.0",
)

MAX_IMAGE_BYTES = 12 * 1024 * 1024

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
}



@app.post("/v1/extractions")
async def extract_receipt(
    file: UploadFile = File(...),
    detalle: Literal["compacto", "completo"] = Query(
        "compacto",
        description=(
            "compacto: datos, revisión y evidencia por campo con el texto "
            "de cada lectura. completo: añade todas las palabras OCR con "
            "sus coordenadas (para guardar junto a la imagen)."
        ),
    ),
):
    try:
        # Validar el tipo declarado del archivo.
        # Su contenido real se valida en prepare_receipt_image.
        if file.content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=415,
                detail="Selecciona una imagen JPEG o PNG.",
            )

        content = await file.read(MAX_IMAGE_BYTES + 1)

        if not content:
            raise HTTPException(
                status_code=400,
                detail="El archivo está vacío.",
            )

        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="La imagen supera el límite de 12 MB.",
            )

        try:
            receipt = await run_in_threadpool(
                process_receipt,
                content,
                detalle == "completo",
            )

        except InvalidReceiptImage as error:
            raise HTTPException(
                status_code=422,
                detail=str(error),
            ) from error

        except OcrUnavailableError as error:
            raise HTTPException(
                status_code=503,
                detail=str(error),
            ) from error

        except OcrProcessingError as error:
            raise HTTPException(
                status_code=503,
                detail=str(error),
            ) from error

        if receipt.get("sin_texto"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "No encontramos texto legible. "
                    "Toma otra foto con buena iluminación y enfoque."
                ),
            )

        return {
            "archivo": file.filename,
            **receipt,
        }

    finally:
        await file.close()
