"""Lectura de las gráficas de consumo histórico.

- ``leer_fila_numeros``: gráficas que imprimen el valor de cada barra en una
  fila (Afinia). Se hace OCR de la región y se toma la fila con más números.
- ``leer_barras``: gráficas de barras con el valor encima de cada barra
  (Aguas de Cartagena). Se detectan las barras con OpenCV, se lee el número de
  cada una y, si el OCR falla en alguna, se estima por la altura de la barra
  usando la proporción altura/valor de las barras que sí se leyeron.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np

from ..services.ocr import PaginaOcr, Palabra, votar_digitos_lote
from ..utils.numeros import corregir_digitos

Zona = tuple[float, float, float, float]


def _agrupar_filas(palabras: list[Palabra], tolerancia: float) -> list[list[Palabra]]:
    filas: list[list[Palabra]] = []
    for p in sorted(palabras, key=lambda p: p.cy):
        if filas and abs(filas[-1][-1].cy - p.cy) <= tolerancia:
            filas[-1].append(p)
        else:
            filas.append([p])
    return [sorted(f, key=lambda p: p.x0) for f in filas]


def segmentar_numeros(recorte: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Segmenta con OpenCV los grupos de dígitos de una gráfica (cajas x0, y0, x1, y1).

    1. Binariza y toma componentes conectados del tamaño de un carácter.
    2. Agrupa los componentes por filas y elige la fila con más grupos.
    3. Dentro de la fila une los caracteres cercanos en números.
    """
    alto, ancho = recorte.shape[:2]
    _, binaria = cv2.threshold(recorte, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, _, stats, _ = cv2.connectedComponentsWithStats(binaria, connectivity=8)
    caracteres = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        # Dígitos (o varios dígitos pegados): altura moderada y bastante "relleno"
        if not (alto * 0.04 <= h <= alto * 0.25) or w > h * 4 or w < 2:
            continue
        if area < 0.15 * w * h:
            continue  # contornos huecos (bordes de las barras)
        caracteres.append((x, y, x + w, y + h))
    if not caracteres:
        return []

    # Filas por centro vertical
    caracteres.sort(key=lambda c: (c[1] + c[3]) / 2)
    filas: list[list[tuple[int, int, int, int]]] = []
    for c in caracteres:
        cy = (c[1] + c[3]) / 2
        h = c[3] - c[1]
        if filas and abs(np.mean([(f[1] + f[3]) / 2 for f in filas[-1]]) - cy) < h * 0.5:
            filas[-1].append(c)
        else:
            filas.append([c])

    def agrupar(fila):
        fila = sorted(fila, key=lambda c: c[0])
        h_med = float(np.median([c[3] - c[1] for c in fila]))
        grupos = [[fila[0]]]
        for c in fila[1:]:
            if c[0] - grupos[-1][-1][2] <= h_med * 0.6:
                grupos[-1].append(c)
            else:
                grupos.append([c])
        return [
            (min(c[0] for c in g), min(c[1] for c in g), max(c[2] for c in g), max(c[3] for c in g))
            for g in grupos
        ]

    candidatas = [agrupar(f) for f in filas if len(f) >= 3]
    if not candidatas:
        return []
    return max(candidatas, key=len)


def leer_fila_numeros(
    pagina: PaginaOcr, zona: Zona, minimo: float, maximo: float, esperados: int
) -> tuple[list[float | None], float]:
    """Lee la fila de números de una gráfica. Devuelve valores (izq→der) y confianza."""
    recorte = pagina.recorte(zona)
    if recorte.size == 0:
        return [], 0.0
    alto_r, ancho_r = recorte.shape[:2]
    x0z, y0z, x1z, y1z = zona
    cajas = segmentar_numeros(recorte)
    if cajas:
        # Descarta ruido: grupos mucho más angostos que la mayoría
        ancho_med = float(np.median([c[2] - c[0] for c in cajas]))
        cajas = [c for c in cajas if (c[2] - c[0]) >= 0.4 * ancho_med]
    valores: list[float | None] = []
    leidos = 0
    if 2 <= len(cajas) <= esperados + 1:
        subzonas = []
        for x0, y0, x1, y1 in cajas:
            m = (y1 - y0) * 0.4
            subzonas.append((
                x0z + (x0 - m) / ancho_r * (x1z - x0z),
                y0z + (y0 - m) / alto_r * (y1z - y0z),
                x0z + (x1 + m) / ancho_r * (x1z - x0z),
                y0z + (y1 + m) / alto_r * (y1z - y0z),
            ))
        todos_votos = [
            {k: v for k, v in votos.items() if minimo <= int(k) <= maximo}
            for votos in votar_digitos_lote(pagina, subzonas)
        ]
        # Los valores de una misma gráfica suelen tener la misma cantidad de dígitos:
        # se favorecen las lecturas con la longitud más votada.
        largos: dict[int, int] = {}
        for votos in todos_votos:
            for k, v in votos.items():
                largos[len(k)] = largos.get(len(k), 0) + v
        largo_comun = max(largos, key=largos.get) if largos else None
        for votos in todos_votos:
            if not votos:
                valores.append(None)
                continue
            mejor = max(votos, key=lambda k: (votos[k] + (2 if len(k) == largo_comun else 0), votos[k]))
            valores.append(float(mejor))
            leidos += 1
        # Si sobra un grupo (p. ej. un número del eje), se descarta el que no se pudo leer
        if len(valores) > esperados and None in valores:
            valores.remove(None)
    valores = descartar_atipicos(valores)
    leidos = sum(v is not None for v in valores)
    if leidos >= max(2, esperados // 2):
        completitud = leidos / esperados
        return valores[:esperados], round(min(0.93, 0.45 + 0.45 * completitud), 2)
    return _leer_fila_numeros_ocr(pagina, zona, minimo, maximo, esperados)


def descartar_atipicos(valores: list[float | None]) -> list[float | None]:
    """Anula lecturas absurdas frente al resto (p. ej. "4" entre valores de ~480)."""
    presentes = [v for v in valores if v]
    if len(presentes) < 3:
        return valores
    mediana = float(np.median(presentes))
    return [v if v is None or mediana / 4 <= v <= mediana * 4 else None for v in valores]


def _leer_fila_numeros_ocr(
    pagina: PaginaOcr, zona: Zona, minimo: float, maximo: float, esperados: int
) -> tuple[list[float | None], float]:
    """Alternativa: OCR de toda la región y fila con más números."""
    mejores: list[Palabra] = []
    for psm in (11, 6):
        palabras = pagina.palabras_region(zona, psm=psm, lista_blanca="0123456789")
        numeros = []
        for p in palabras:
            texto = re.sub(r"\D", "", corregir_digitos(p.texto))
            if texto and minimo <= int(texto) <= maximo and 1 <= len(texto) <= 5:
                numeros.append(Palabra(texto, p.x0, p.y0, p.x1, p.y1, p.conf))
        if not numeros:
            continue
        alto_medio = float(np.median([n.alto for n in numeros]))
        filas = _agrupar_filas(numeros, tolerancia=alto_medio * 0.6)
        # Los valores de la gráfica tienen todos la misma cantidad de dígitos aproximadamente
        fila = max(filas, key=len)
        if len(fila) > len(mejores) and len(fila) <= esperados:
            mejores = fila
        if len(mejores) == esperados:
            break
    if not mejores:
        return [], 0.0
    valores: list[float | None] = [float(p.texto) for p in mejores]
    conf_ocr = float(np.mean([p.conf for p in mejores])) / 100
    completitud = len(valores) / esperados
    return valores, round(min(0.95, 0.35 + 0.4 * conf_ocr + 0.2 * completitud), 2)


@dataclass
class Barra:
    x0: int
    y0: int  # tope de la barra (px dentro del recorte)
    x1: int
    base: int

    @property
    def alto(self) -> int:
        return self.base - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


def detectar_barras(recorte: np.ndarray, esperadas: int) -> list[Barra]:
    """Detecta barras verticales rellenas (gris medio sobre fondo blanco)."""
    alto, ancho = recorte.shape[:2]
    suave = cv2.GaussianBlur(recorte, (3, 3), 0)
    # Relleno de las barras: ni blanco del papel ni negro del texto
    mascara = cv2.inRange(suave, 70, 225)
    # Elimina texto y líneas finas; conserva bloques anchos y altos
    ancho_min = max(3, ancho // (esperadas * 4))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (ancho_min, max(3, alto // 25)))
    mascara = cv2.morphologyEx(mascara, cv2.MORPH_OPEN, kernel)
    contornos, _ = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    barras = []
    for c in contornos:
        x, y, w, h = cv2.boundingRect(c)
        if w < ancho_min or w > ancho / esperadas * 1.2 or h < alto * 0.05:
            continue
        barras.append(Barra(x, y, x + w, y + h))
    if not barras:
        return []
    # Las barras comparten la base (eje X); descarta lo que no esté alineado
    base = int(np.median([b.base for b in barras]))
    barras = [b for b in barras if abs(b.base - base) <= alto * 0.06]
    for b in barras:
        b.base = base
    return sorted(barras, key=lambda b: b.x0)


def leer_barras(
    pagina: PaginaOcr, zona: Zona, esperadas: int, maximo: float = 9999
) -> tuple[list[float | None], list[Barra], float]:
    """Lee el valor impreso encima de cada barra; completa por altura si hace falta."""
    recorte = pagina.recorte(zona, base=True)
    if recorte.size == 0:
        return [], [], 0.0
    barras = detectar_barras(recorte, esperadas)
    if not barras:
        return [], [], 0.0

    alto_r, ancho_r = recorte.shape[:2]
    x0z, y0z, x1z, y1z = zona
    ancho_z, alto_z = x1z - x0z, y1z - y0z
    subzonas = []
    for b in barras:
        margen_x = (b.x1 - b.x0) * 0.6
        alto_texto = max(alto_r * 0.12, (b.x1 - b.x0) * 0.9)
        subzonas.append((
            x0z + (b.x0 - margen_x) / ancho_r * ancho_z,
            y0z + (b.y0 - alto_texto) / alto_r * alto_z,
            x0z + (b.x1 + margen_x) / ancho_r * ancho_z,
            y0z + (b.y0 + 2) / alto_r * alto_z,
        ))
    candidatos = [
        {float(k): v for k, v in votos.items() if 0 < float(k) <= maximo}
        for votos in votar_digitos_lote(pagina, subzonas)
    ]

    # El valor de cada barra es proporcional a su altura. Se busca la proporción
    # valor/altura que más lecturas respaldan (tolerancia ±25 %); las lecturas que
    # no encajan se consideran errores del OCR.
    razones = [(v / b.alto, n) for cand, b in zip(candidatos, barras) if b.alto > 0 for v, n in cand.items()]
    escala, apoyo = None, 0
    for r, _ in razones:
        barras_apoyo = sum(
            1 for cand, b in zip(candidatos, barras) if any(abs(v / b.alto / r - 1) <= 0.25 for v in cand)
        )
        if barras_apoyo > apoyo:
            escala, apoyo = r, barras_apoyo
    if escala is None or apoyo < max(2, len(barras) // 3):
        return [None] * len(barras), barras, 0.25

    # Refina la escala con la mediana de las lecturas coherentes
    coherentes = [
        v / b.alto for cand, b in zip(candidatos, barras) for v in cand if abs(v / b.alto / escala - 1) <= 0.25
    ]
    escala = float(np.median(coherentes))

    valores: list[float | None] = []
    leidas_ok = 0
    for cand, b in zip(candidatos, barras):
        estimado = b.alto * escala
        compatibles = {v: n for v, n in cand.items() if abs(v / estimado - 1) <= 0.25}
        if compatibles:
            valores.append(max(compatibles, key=lambda v: (compatibles[v], -abs(v - estimado))))
            leidas_ok += 1
        else:
            valores.append(float(round(estimado)))  # estimado por la altura de la barra

    conf = 0.4 + 0.45 * leidas_ok / max(1, len(barras)) + 0.1 * min(1, len(barras) / esperadas)
    if leidas_ok < len(barras):
        conf -= 0.1
    return valores, barras, round(max(0.2, min(0.92, conf)), 2)
