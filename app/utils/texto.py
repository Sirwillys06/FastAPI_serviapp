"""Utilidades para normalizar y comparar texto reconocido por OCR."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


def sin_tildes(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def normalizar(texto: str) -> str:
    """Mayúsculas, sin tildes y con espacios simples. Útil para buscar etiquetas."""
    texto = sin_tildes(texto).upper()
    texto = texto.replace("�", "")
    return re.sub(r"\s+", " ", texto).strip()


def similitud(a: str, b: str) -> float:
    return SequenceMatcher(None, normalizar(a), normalizar(b)).ratio()


def contiene_aprox(texto: str, buscado: str, umbral: float = 0.8) -> bool:
    """Indica si ``buscado`` aparece en ``texto`` tolerando errores típicos del OCR.

    Compara ``buscado`` contra todas las ventanas de palabras del mismo tamaño.
    """
    texto_n = normalizar(texto)
    buscado_n = normalizar(buscado)
    if not buscado_n:
        return False
    if buscado_n in texto_n:
        return True
    palabras = texto_n.split()
    n = len(buscado_n.split())
    for i in range(max(1, len(palabras) - n + 1)):
        ventana = " ".join(palabras[i : i + n])
        if SequenceMatcher(None, ventana, buscado_n).ratio() >= umbral:
            return True
    return False


def limpiar_linea(texto: str) -> str:
    """Quita basura frecuente del OCR en los bordes (barras, corchetes, guiones)."""
    texto = texto.replace("�", "")
    texto = re.sub(r"^[\s|\[\]{}_\-:;.,'\"`~=]+", "", texto)
    texto = re.sub(r"[\s|\[\]{}_\-:;,'\"`~=]+$", "", texto)
    return re.sub(r"\s+", " ", texto).strip()
