"""OCR con Tesseract y utilidades para ubicar texto por posición.

Todas las coordenadas se manejan normalizadas (0..1) respecto al ancho y alto de
la imagen, para que los extractores no dependan de la resolución de la foto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import cv2
import numpy as np
import pytesseract
from pytesseract import Output

from ..core.config import get_settings
from ..utils.texto import contiene_aprox, normalizar


class OcrNoDisponibleError(RuntimeError):
    pass


def configurar_tesseract() -> None:
    settings = get_settings()
    if not settings.tesseract_cmd:
        raise OcrNoDisponibleError(
            "No se encontró Tesseract. Instálalo o define la variable TESSERACT_CMD."
        )
    pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd


@dataclass
class Palabra:
    texto: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def alto(self) -> float:
        return self.y1 - self.y0


@dataclass
class Linea:
    palabras: list[Palabra] = field(default_factory=list)

    @property
    def texto(self) -> str:
        return " ".join(p.texto for p in self.palabras)

    @property
    def norm(self) -> str:
        return normalizar(self.texto)

    @property
    def x0(self) -> float:
        return min(p.x0 for p in self.palabras)

    @property
    def x1(self) -> float:
        return max(p.x1 for p in self.palabras)

    @property
    def y0(self) -> float:
        return min(p.y0 for p in self.palabras)

    @property
    def y1(self) -> float:
        return max(p.y1 for p in self.palabras)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def alto(self) -> float:
        return self.y1 - self.y0

    @property
    def conf(self) -> float:
        return float(np.mean([p.conf for p in self.palabras])) / 100.0

    def __repr__(self) -> str:  # útil al depurar
        return f"Linea({self.texto!r} @ x={self.x0:.3f} y={self.y0:.3f})"


def _agrupar_lineas(palabras: list[Palabra], aspecto: float) -> list[Linea]:
    """Une palabras cercanas de la misma fila en líneas.

    Dos palabras se unen si se solapan verticalmente y la separación horizontal
    es menor a ~1.8 veces la altura del texto. ``aspecto`` = alto / ancho de la
    imagen, para pasar alturas (normalizadas al alto) a unidades del ancho.
    """
    lineas: list[Linea] = []
    for p in sorted(palabras, key=lambda p: p.x0):
        mejor = None
        for linea in lineas:
            ultima = linea.palabras[-1]
            solape = min(ultima.y1, p.y1) - max(ultima.y0, p.y0)
            if solape < 0.5 * min(ultima.alto, p.alto):
                continue
            # alturas comparables (evita unir títulos con texto pequeño)
            if not (0.5 < p.alto / max(ultima.alto, 1e-6) < 2.0):
                continue
            hueco = p.x0 - ultima.x1
            if -ultima.alto * aspecto <= hueco <= 1.8 * ultima.alto * aspecto:
                mejor = linea
                break
        if mejor:
            mejor.palabras.append(p)
        else:
            lineas.append(Linea([p]))
    return sorted(lineas, key=lambda l: (round(l.y0, 2), l.x0))


class PaginaOcr:
    def __init__(self, gris: np.ndarray, lang: str | None = None, base: np.ndarray | None = None):
        self.gris = gris
        # Escala de grises sin realce de contraste (mejor para detectar formas como barras)
        self.base = base if base is not None else gris
        self.alto, self.ancho = gris.shape[:2]
        self.lang = lang or get_settings().ocr_lang
        self.palabras = self._leer_palabras()
        self.lineas = _agrupar_lineas(self.palabras, self.alto / self.ancho)

    # ------------------------------------------------------------------ OCR base
    def _leer_palabras(self) -> list[Palabra]:
        datos = pytesseract.image_to_data(
            self.gris, lang=self.lang, config="--psm 11 --oem 1", output_type=Output.DICT
        )
        palabras = []
        for i, texto in enumerate(datos["text"]):
            texto = (texto or "").strip()
            conf = float(datos["conf"][i])
            if not texto or conf < 0:
                continue
            # Descarta basura de un solo símbolo con baja confianza
            if len(texto) == 1 and not texto.isalnum():
                continue
            x, y, w, h = datos["left"][i], datos["top"][i], datos["width"][i], datos["height"][i]
            palabras.append(
                Palabra(
                    texto=texto,
                    x0=x / self.ancho,
                    y0=y / self.alto,
                    x1=(x + w) / self.ancho,
                    y1=(y + h) / self.alto,
                    conf=conf,
                )
            )
        return palabras

    @property
    def texto(self) -> str:
        return "\n".join(l.texto for l in self.lineas)

    # ------------------------------------------------------------ búsquedas
    def buscar(
        self,
        etiqueta: str,
        umbral: float = 0.8,
        zona: tuple[float, float, float, float] | None = None,
    ) -> list[Linea]:
        """Líneas que contienen ``etiqueta`` (tolerando errores del OCR)."""
        resultado = []
        varias_palabras = len(etiqueta.split()) > 1
        for linea in self.lineas:
            if zona and not _dentro(linea, zona):
                continue
            if contiene_aprox(linea.texto, etiqueta, umbral):
                resultado.append(linea)
            elif varias_palabras:
                # La etiqueta puede haber quedado partida en dos líneas vecinas
                vecinas = self.a_la_derecha(linea, max_dx=0.03)[:2]
                texto = " ".join([linea.texto] + [v.texto for v in vecinas])
                if vecinas and contiene_aprox(texto, etiqueta, umbral):
                    resultado.append(linea)
        return resultado

    def buscar_regex(
        self, patron: str, zona: tuple[float, float, float, float] | None = None
    ) -> list[tuple[Linea, re.Match]]:
        regex = re.compile(patron)
        resultado = []
        for linea in self.lineas:
            if zona and not _dentro(linea, zona):
                continue
            m = regex.search(linea.norm)
            if m:
                resultado.append((linea, m))
        return resultado

    def a_la_derecha(self, ref: Linea, max_dx: float = 0.6, tolerancia: float = 0.6) -> list[Linea]:
        """Líneas en la misma fila que ``ref`` y a su derecha, ordenadas por cercanía."""
        resultado = []
        for linea in self.lineas:
            if linea is ref or linea.x0 - ref.x1 > max_dx:
                continue
            # A la derecha: empieza después que ref y su centro pasa el centro de ref
            # (Tesseract a veces devuelve cajas solapadas para palabras vecinas)
            if linea.x0 <= ref.x0 + 0.01 or (linea.x0 + linea.x1) / 2 <= (ref.x0 + ref.x1) / 2:
                continue
            if abs(linea.cy - ref.cy) <= tolerancia * max(ref.alto, linea.alto):
                resultado.append(linea)
        return sorted(resultado, key=lambda l: l.x0)

    def debajo(self, ref: Linea, max_dy: float = 0.05, holgura_x: float = 0.05) -> list[Linea]:
        """Líneas debajo de ``ref`` que se solapan horizontalmente con ella."""
        resultado = []
        for linea in self.lineas:
            if linea is ref or linea.y0 < ref.y1 - 0.002 or linea.y0 - ref.y1 > max_dy:
                continue
            if linea.x1 < ref.x0 - holgura_x or linea.x0 > ref.x1 + holgura_x:
                continue
            resultado.append(linea)
        return sorted(resultado, key=lambda l: (l.y0, l.x0))

    def en_zona(self, zona: tuple[float, float, float, float]) -> list[Linea]:
        return [l for l in self.lineas if _dentro(l, zona)]

    # --------------------------------------------------------- OCR por regiones
    def recorte(self, zona: tuple[float, float, float, float], base: bool = False) -> np.ndarray:
        x0, y0, x1, y1 = zona
        x0, x1 = max(0.0, x0), min(1.0, x1)
        y0, y1 = max(0.0, y0), min(1.0, y1)
        imagen = self.base if base else self.gris
        return imagen[int(y0 * self.alto) : int(y1 * self.alto), int(x0 * self.ancho) : int(x1 * self.ancho)]

    def leer_region(
        self,
        zona: tuple[float, float, float, float],
        psm: int = 7,
        lista_blanca: str | None = None,
        variantes: bool = True,
    ) -> list[str]:
        """Vuelve a aplicar OCR solo a una región, con varias versiones de la imagen.

        Devuelve el texto leído en cada variante (sin vacíos), para que el
        extractor elija el que cumpla su formato.
        """
        recorte = self.recorte(zona)
        if recorte.size == 0 or min(recorte.shape[:2]) < 5:
            return []
        alto = recorte.shape[0]
        # Tesseract funciona mejor con letras de ~30-40 px
        if alto < 60 and psm == 7:
            factor = 60 / alto
            recorte = cv2.resize(recorte, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        recorte = cv2.copyMakeBorder(recorte, 12, 12, 12, 12, cv2.BORDER_REPLICATE)

        imagenes = [recorte]
        if variantes:
            _, otsu = cv2.threshold(recorte, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            imagenes.append(otsu)
            # Texto claro sobre fondo oscuro (cajas de color de Afinia/Surtigas)
            if np.mean(recorte) < 128:
                imagenes.append(255 - otsu)

        config = f"--psm {psm} --oem 1"
        if lista_blanca:
            config += f" -c tessedit_char_whitelist={lista_blanca}"
        textos = []
        for imagen in imagenes:
            try:
                texto = pytesseract.image_to_string(imagen, lang=self.lang, config=config).strip()
            except pytesseract.TesseractError:
                continue
            if texto:
                textos.append(texto)
        return textos

    def palabras_region(
        self, zona: tuple[float, float, float, float], psm: int = 11, lista_blanca: str | None = None
    ) -> list[Palabra]:
        """OCR de una región devolviendo palabras con posición (normalizada a la página)."""
        x0, y0, x1, y1 = zona
        recorte = self.recorte(zona)
        if recorte.size == 0:
            return []
        factor = 2.0 if recorte.shape[0] < 400 else 1.0
        if factor != 1.0:
            recorte = cv2.resize(recorte, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        config = f"--psm {psm} --oem 1"
        if lista_blanca:
            config += f" -c tessedit_char_whitelist={lista_blanca}"
        datos = pytesseract.image_to_data(recorte, lang=self.lang, config=config, output_type=Output.DICT)
        alto_r, ancho_r = recorte.shape[:2]
        resultado = []
        for i, texto in enumerate(datos["text"]):
            texto = (texto or "").strip()
            if not texto or float(datos["conf"][i]) < 0:
                continue
            px0 = datos["left"][i] / ancho_r
            py0 = datos["top"][i] / alto_r
            px1 = (datos["left"][i] + datos["width"][i]) / ancho_r
            py1 = (datos["top"][i] + datos["height"][i]) / alto_r
            resultado.append(
                Palabra(
                    texto=texto,
                    x0=x0 + px0 * (x1 - x0),
                    y0=y0 + py0 * (y1 - y0),
                    x1=x0 + px1 * (x1 - x0),
                    y1=y0 + py1 * (y1 - y0),
                    conf=float(datos["conf"][i]),
                )
            )
        return resultado


def _dentro(linea: Linea, zona: tuple[float, float, float, float]) -> bool:
    """La línea está en la zona si su centro vertical cae dentro y al menos la
    mitad de su ancho se solapa horizontalmente con la zona."""
    x0, y0, x1, y1 = zona
    if not y0 <= linea.cy <= y1:
        return False
    solape = min(x1, linea.x1) - max(x0, linea.x0)
    return solape >= 0.5 * max(linea.x1 - linea.x0, 1e-6)


def votar_digitos_lote(
    pagina: PaginaOcr, zonas: list[tuple[float, float, float, float]]
) -> list[dict[str, int]]:
    """Lee varios números cortos a la vez (uno por zona) con votación entre variantes.

    Pega todos los recortes en una sola tira separada por espacios en blanco y la
    procesa con Tesseract en varias escalas/binarizaciones. Así se hacen pocas
    llamadas a Tesseract en lugar de una por número y variante.
    """
    recortes = [pagina.recorte(z) for z in zonas]
    votos: list[dict[str, int]] = [{} for _ in zonas]
    validos = [i for i, r in enumerate(recortes) if r.size and min(r.shape[:2]) >= 4]
    if not validos:
        return votos
    alto_ref = max(recortes[i].shape[0] for i in validos)
    for objetivo in (48, 72):
        factor = max(1.0, objetivo / alto_ref)
        piezas = []
        for i in validos:
            r = cv2.resize(recortes[i], None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
            r = cv2.copyMakeBorder(r, 0, int(alto_ref * factor) - r.shape[0], 0, 0, cv2.BORDER_REPLICATE)
            piezas.append(r)
        separador = int(objetivo * 1.5)
        for binarizar in (False, True):
            filas = []
            limites = []  # rango x de cada pieza en la tira
            x = 20
            for r in piezas:
                if binarizar:
                    _, r = cv2.threshold(r, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                filas.append(r)
                limites.append((x, x + r.shape[1]))
                x += r.shape[1] + separador
            blanco = np.full((filas[0].shape[0], separador), 255, np.uint8)
            tira = np.hstack([v for r in filas for v in (r, blanco)][:-1])
            tira = cv2.copyMakeBorder(tira, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
            for psm in (7, 6):
                try:
                    datos = pytesseract.image_to_data(
                        tira,
                        config=f"--psm {psm} --oem 1 -c tessedit_char_whitelist=0123456789",
                        output_type=Output.DICT,
                    )
                except pytesseract.TesseractError:
                    continue
                lecturas: dict[int, str] = {}
                for j, texto in enumerate(datos["text"]):
                    digitos = re.sub(r"\D", "", texto or "")
                    if not digitos:
                        continue
                    cx = datos["left"][j] + datos["width"][j] / 2
                    for k, (a0, a1) in enumerate(limites):
                        if a0 - separador / 2 <= cx <= a1 + separador / 2:
                            lecturas[k] = lecturas.get(k, "") + digitos
                            break
                for k, digitos in lecturas.items():
                    destino = votos[validos[k]]
                    destino[digitos] = destino.get(digitos, 0) + 1
    return votos
