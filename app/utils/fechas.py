"""Reconocimiento de fechas en los formatos que usan los recibos.

Formatos vistos:
- Afinia:   ``17/09/2026``
- Aguas:    ``2026-08-15``, ``09-07-26`` y ``22-08-2026``
- Surtigas: ``30/08/2026``, ``31/AGO/2026``, ``AGO2026``
"""

from __future__ import annotations

import re
from datetime import date

from .numeros import corregir_digitos
from .texto import normalizar

MESES = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "SET": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}

_ISO = re.compile(r"(?<!\d)(20\d{2})([-/.])(\d{1,2})\2(\d{1,2})(?!\d)")
_DMY = re.compile(r"(\d{1,2})\s?[-/.]\s?(\d{1,2})\s?[-/.]\s?(\d{4}|\d{2})(?!\d)")
_DMY_PEGADO = re.compile(r"(?<!\d)(\d{1,2})[-/.]?(\d{2})[-/.]?(20\d{2})(?!\d)")
_D_MES_Y = re.compile(r"(\d{1,2})\s?[-/.]?\s?([A-Z0]{3})[A-Z]*\s?[-/.]?\s?(\d{4})")
_MES_Y = re.compile(r"\b([A-Z][A-Z0]{2})[A-Z]*\s?[-/.]?\s?(20\d{2})\b")


def _fecha_valida(anio: int, mes: int, dia: int) -> date | None:
    if anio < 100:
        anio += 2000
    if not (2000 <= anio <= 2100):
        return None
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def buscar_fechas(texto: str) -> list[date]:
    """Devuelve todas las fechas reconocibles en el texto, en orden de aparición."""
    if not texto:
        return []
    t = normalizar(texto)
    # Corrige letras en zonas de dígitos sin tocar los nombres de mes.
    t_dig = re.sub(r"[0-9OIlSB]{1,4}(?:[-/.][0-9OIlSB]{1,4}){2}", lambda m: corregir_digitos(m.group()), t)
    encontradas: list[tuple[int, date]] = []
    ocupado: list[tuple[int, int]] = []

    def agregar(m: re.Match, fecha: date | None) -> None:
        # Evita que un mismo texto se lea dos veces con patrones distintos
        if fecha and not any(m.start() < fin and m.end() > ini for ini, fin in ocupado):
            encontradas.append((m.start(), fecha))
            ocupado.append((m.start(), m.end()))

    for m in _ISO.finditer(t_dig):
        agregar(m, _fecha_valida(int(m.group(1)), int(m.group(3)), int(m.group(4))))
    for m in _DMY.finditer(t_dig):
        agregar(m, _fecha_valida(int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in _D_MES_Y.finditer(t):
        mes = MESES.get(m.group(2)[:3].replace("0", "O"))
        if mes:
            agregar(m, _fecha_valida(int(m.group(3)), mes, int(m.group(1))))
    if not encontradas:
        for m in _DMY_PEGADO.finditer(t_dig):
            agregar(m, _fecha_valida(int(m.group(3)), int(m.group(2)), int(m.group(1))))

    encontradas.sort(key=lambda x: x[0])
    vistas: list[date] = []
    for _, f in encontradas:
        if f not in vistas:
            vistas.append(f)
    return vistas


def primera_fecha(texto: str) -> date | None:
    fechas = buscar_fechas(texto)
    return fechas[0] if fechas else None


def buscar_mes_anio(texto: str) -> tuple[int, int] | None:
    """Reconoce ``AGO2026``, ``AGO 2026``, ``2026/08`` o ``08/26`` y devuelve (año, mes)."""
    t = normalizar(texto)
    for m in _MES_Y.finditer(t):
        mes = m.group(1)[:3].replace("0", "O")
        if mes in MESES:
            return int(m.group(2)), MESES[mes]
    m = re.search(r"(20\d{2})\s?[/-]\s?(\d{1,2})(?!\d)", corregir_digitos(t))
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(?<!\d)(\d{1,2})\s?/\s?(\d{2})(?!\d)", corregir_digitos(t))
    if m and 1 <= int(m.group(1)) <= 12:
        return 2000 + int(m.group(2)), int(m.group(1))
    return None


def sumar_meses(anio: int, mes: int, delta: int) -> tuple[int, int]:
    total = anio * 12 + (mes - 1) + delta
    return total // 12, total % 12 + 1


def periodo_str(anio: int, mes: int) -> str:
    return f"{anio:04d}-{mes:02d}"
