"""Conversión de cadenas reconocidas por OCR a números."""

from __future__ import annotations

import re

# Confusiones típicas de Tesseract entre letras y dígitos.
_CONFUSIONES = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "D": "0",
        "Q": "0",
        "I": "1",
        "l": "1",
        "|": "1",
        "i": "1",
        "S": "5",
        "s": "5",
        "B": "8",
        "Z": "2",
        "z": "2",
        "g": "9",
    }
)


def corregir_digitos(texto: str) -> str:
    """Reemplaza letras parecidas a dígitos. Solo usar en zonas que deben ser numéricas."""
    return texto.translate(_CONFUSIONES)


def parsear_pesos(texto: str) -> int | None:
    """Convierte un valor en pesos colombianos a entero.

    Acepta ``$427.630``, ``$ 427.630,00``, ``$344,546``, ``380.638`` y variantes
    con espacios. Los recibos no usan centavos significativos, así que una parte
    decimal de dos dígitos se descarta.
    """
    if not texto:
        return None
    limpio = corregir_digitos(texto.replace("$", " ")).strip()
    limpio = re.sub(r"[^\d.,]", "", limpio)
    if not limpio or not re.search(r"\d", limpio):
        return None
    # Centavos: ",00" o ".00" al final
    limpio = re.sub(r"[.,]\d{2}$", "", limpio) if re.search(r"[.,]\d{3}[.,]\d{2}$", limpio) else limpio
    digitos = re.sub(r"[.,]", "", limpio)
    if not digitos:
        return None
    return int(digitos)


def parsear_decimal(texto: str) -> float | None:
    """Convierte ``15,61`` o ``3.373,00`` o ``0.3809`` a float."""
    if not texto:
        return None
    limpio = re.sub(r"[^\d.,\-]", "", corregir_digitos(texto))
    if not re.search(r"\d", limpio):
        return None
    if "," in limpio and "." in limpio:
        if limpio.rfind(",") > limpio.rfind("."):
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif "," in limpio:
        limpio = limpio.replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return None


def parsear_entero(texto: str) -> int | None:
    if not texto:
        return None
    digitos = re.sub(r"\D", "", corregir_digitos(texto))
    return int(digitos) if digitos else None


VALOR_PESOS = re.compile(r"\$?\s?\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{2})?|\$\s?\d{1,9}")
