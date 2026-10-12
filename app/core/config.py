"""Configuración del servicio leída desde variables de entorno.

Variables soportadas (todas opcionales):

- ``TESSERACT_CMD``: ruta al ejecutable de Tesseract. Si no se define se busca
  en el PATH y en las rutas típicas de Windows.
- ``SERVIAPP_API_KEY``: si se define, todas las peticiones a ``/recibos`` deben
  enviar la cabecera ``X-API-Key`` con este valor.
- ``MAX_UPLOAD_MB``: tamaño máximo de la imagen recibida (por defecto 15 MB).
- ``OCR_LANG``: idioma de Tesseract (por defecto ``spa``).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_RUTAS_WINDOWS = (
    r"D:\Tesseract OCR\tesseract.exe",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def _cargar_env_local() -> None:
    """Carga un archivo ``.env`` en la raíz del proyecto si existe (sin dependencias)."""
    env = Path(__file__).resolve().parents[2] / ".env"
    if not env.exists():
        return
    for linea in env.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def _buscar_tesseract() -> str | None:
    if os.getenv("TESSERACT_CMD"):
        return os.environ["TESSERACT_CMD"]
    encontrado = shutil.which("tesseract")
    if encontrado:
        return encontrado
    for ruta in _RUTAS_WINDOWS:
        if Path(ruta).exists():
            return ruta
    return None


@dataclass(frozen=True)
class Settings:
    tesseract_cmd: str | None
    api_key: str | None
    max_upload_bytes: int
    ocr_lang: str


@lru_cache
def get_settings() -> Settings:
    _cargar_env_local()
    return Settings(
        tesseract_cmd=_buscar_tesseract(),
        api_key=os.getenv("SERVIAPP_API_KEY") or None,
        max_upload_bytes=int(float(os.getenv("MAX_UPLOAD_MB", "15")) * 1024 * 1024),
        ocr_lang=os.getenv("OCR_LANG", "spa"),
    )
