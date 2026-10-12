# Imagen para desplegar en Railway (o cualquier servicio con Docker)
FROM python:3.12-slim

# Tesseract con idioma español + librerías que necesita OpenCV
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa tesseract-ocr-osd libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
# Railway define PORT automáticamente
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
