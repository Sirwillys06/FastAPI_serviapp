"""
Representación geométrica de las palabras OCR.

Tesseract agrupa las palabras en líneas según su propia segmentación,
que cambia entre pasadas y suele mezclar columnas. Aquí reconstruimos
filas a partir de las coordenadas para poder:

* localizar etiquetas aunque estén partidas en varias palabras;
* buscar el valor a la derecha o debajo de una etiqueta;
* trabajar con regiones concretas del recibo (gráficas, paneles).
"""

import re
import unicodedata
from dataclasses import dataclass
from statistics import median


def normalize_text(text: str) -> str:
    """Mayúsculas y sin tildes. Conserva la puntuación."""
    decomposed = unicodedata.normalize("NFD", text)

    return "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    ).upper()


def label_key(text: str) -> str:
    """Texto reducido a letras, dígitos y espacios para comparar etiquetas."""
    return re.sub(r"[^A-Z0-9]+", " ", normalize_text(text)).strip()


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def union(self, other: "Box") -> "Box":
        x1 = min(self.x, other.x)
        y1 = min(self.y, other.y)
        x2 = max(self.right, other.right)
        y2 = max(self.bottom, other.bottom)

        return Box(x1, y1, x2 - x1, y2 - y1)

    def vertical_overlap(self, top: float, bottom: float) -> float:
        return max(0.0, min(self.bottom, bottom) - max(self.y, top))

    def contains_center_of(self, other: "Box") -> bool:
        return (
            self.x <= other.cx <= self.right
            and self.y <= other.cy <= self.bottom
        )

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.w, "height": self.h}


@dataclass(frozen=True)
class Word:
    text: str
    conf: float
    box: Box
    source: str

    @property
    def norm(self) -> str:
        return normalize_text(self.text)

    @property
    def key(self) -> str:
        return label_key(self.text)


def words_from_reading(reading: dict) -> list[Word]:
    words = []

    for item in reading.get("words", []):
        box = item["box"]
        words.append(
            Word(
                text=item["text"],
                conf=float(item.get("confidence", -1)),
                box=Box(box["x"], box["y"], box["width"], box["height"]),
                source=reading["name"],
            )
        )

    return words


@dataclass
class Line:
    words: list[Word]

    @property
    def box(self) -> Box:
        box = self.words[0].box

        for word in self.words[1:]:
            box = box.union(word.box)

        return box

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)


def build_lines(words: list[Word]) -> list[Line]:
    """
    Agrupa palabras en filas por solapamiento vertical y separa
    columnas cuando el espacio horizontal es grande.
    """
    if not words:
        return []

    typical_height = median(word.box.h for word in words)

    # Cajas gigantes (logos, códigos QR leídos como texto) unirían filas.
    regular = [w for w in words if w.box.h <= 4 * typical_height]
    oversized = [w for w in words if w.box.h > 4 * typical_height]

    rows: list[list[Word]] = []

    for word in sorted(regular, key=lambda w: w.box.cy):
        best_row = None
        best_overlap = 0.0

        for row in rows[-12:]:
            top = median(w.box.y for w in row)
            bottom = median(w.box.bottom for w in row)
            overlap = word.box.vertical_overlap(top, bottom)
            needed = 0.5 * min(word.box.h, bottom - top)

            if overlap >= needed and overlap > best_overlap:
                best_row = row
                best_overlap = overlap

        if best_row is None:
            rows.append([word])
        else:
            best_row.append(word)

    lines: list[Line] = []

    for row in rows:
        row.sort(key=lambda w: w.box.x)
        row_height = median(w.box.h for w in row)
        current = [row[0]]

        for word in row[1:]:
            gap = word.box.x - current[-1].box.right

            if gap > 1.6 * row_height:
                lines.append(Line(current))
                current = [word]
            else:
                current.append(word)

        lines.append(Line(current))

    lines.extend(Line([word]) for word in oversized)
    lines.sort(key=lambda line: (line.box.y, line.box.x))

    return lines


@dataclass
class LabelMatch:
    """Una etiqueta encontrada: palabras que la forman y su caja."""

    line: Line
    start: int
    end: int
    source: str
    text_height: float | None = None

    @property
    def words(self) -> list[Word]:
        return self.line.words[self.start:self.end]

    @property
    def box(self) -> Box:
        box = self.words[0].box

        for word in self.words[1:]:
            box = box.union(word.box)

        return box

    @property
    def height(self) -> float:
        if self.text_height:
            return self.text_height

        return median(word.box.h for word in self.words)

    @property
    def confidence(self) -> float:
        return min(word.conf for word in self.words)


def virtual_label(box: Box, height: float, text: str, source: str) -> LabelMatch:
    """
    Etiqueta ubicada en otra lectura. Se usa en las zonas releídas cuando
    el recorte leyó el valor pero no volvió a leer la etiqueta.
    """
    word = Word(text=text, conf=100.0, box=box, source=source)

    return LabelMatch(line=Line([word]), start=0, end=1, source=source, text_height=height)


def _matches_in(line: Line, regex, split: int | None = None) -> list[LabelMatch]:
    spans = []
    text = ""

    for word in line.words:
        if text:
            text += " "

        spans.append((len(text), len(text) + len(word.key)))
        text += word.key

    matches = []

    for match in regex.finditer(text):
        covered = [
            index
            for index, (start, end) in enumerate(spans)
            if end > match.start() and start < match.end() and end > start
        ]

        if not covered:
            continue

        # En filas apiladas solo interesan etiquetas que cruzan ambas filas.
        if split is not None and not (covered[0] < split <= covered[-1]):
            continue

        words = [line.words[i] for i in covered]

        matches.append(
            LabelMatch(
                line=line,
                start=covered[0],
                end=covered[-1] + 1,
                source=line.words[0].source,
                text_height=median(word.box.h for word in words),
            )
        )

    return matches


def _stacked_pairs(lines: list[Line]):
    """Filas consecutivas alineadas: "TOTAL A" encima de "PAGAR"."""
    for index, upper in enumerate(lines):
        upper_box = upper.box
        height = median(word.box.h for word in upper.words)

        for lower in lines[index + 1:]:
            lower_box = lower.box

            if lower_box.y > upper_box.bottom + 2 * height:
                break

            gap = lower_box.y - upper_box.bottom
            overlap = min(upper_box.right, lower_box.right) - max(upper_box.x, lower_box.x)

            if -0.3 * height <= gap <= 1.0 * height and (
                overlap > 0 or abs(lower_box.x - upper_box.x) <= 3 * height
            ):
                yield Line(upper.words + lower.words), len(upper.words)


def find_label(lines: list[Line], pattern: str) -> list[LabelMatch]:
    """
    Busca una expresión regular sobre las claves normalizadas de cada fila.

    La búsqueda se hace por fila reconstruida, no por palabra, para
    admitir etiquetas de varias palabras como "FECHA DE EMISION". También
    se prueban pares de filas apiladas, porque algunas etiquetas se
    imprimen en dos renglones ("No. de" / "contrato:").

    Los espacios del patrón son opcionales: el OCR a menudo une palabras
    de una etiqueta ("TOTALA PAGAR", "CONSUMOMES").
    """
    regex = re.compile(pattern.replace(" ", " ?"))
    matches = []

    for line in lines:
        matches.extend(_matches_in(line, regex))

    for line, split in _stacked_pairs(lines):
        matches.extend(_matches_in(line, regex, split))

    return matches


def words_right_of(
    label: LabelMatch,
    words: list[Word],
    max_dx: float,
    band: float = 0.6,
) -> list[Word]:
    """
    Palabras a la derecha de la etiqueta dentro de una franja vertical.

    La franja se amplía con `band` veces la altura de la etiqueta porque
    los valores suelen tener una fuente más grande o estar desplazados.
    Las palabras que siguen a la etiqueta en su misma fila se aceptan
    aunque Tesseract haya dado a la etiqueta una caja que las cubre.
    """
    box = label.box
    height = label.height
    top = box.y - band * height
    bottom = box.bottom + band * height
    own = set(id(word) for word in label.words)
    following = set(id(word) for word in label.line.words[label.end:])

    selected = [
        word
        for word in words
        if id(word) not in own
        and (word.box.x >= box.right - 0.3 * height or id(word) in following)
        and word.box.x <= box.right + max_dx
        and word.box.vertical_overlap(top, bottom) >= 0.5 * min(word.box.h, bottom - top)
    ]

    if not selected:
        return []

    # Si la franja alcanza dos filas (p. ej. "Dir. de Predio" y "Dir. de
    # Entrega" una encima de otra), se conserva la más cercana al centro
    # de la etiqueta para no mezclar palabras de ambas.
    rows: list[list[Word]] = []

    for word in sorted(selected, key=lambda w: w.box.cy):
        if rows and abs(word.box.cy - median(w.box.cy for w in rows[-1])) <= 0.5 * max(word.box.h, height):
            rows[-1].append(word)
        else:
            rows.append([word])

    nearest = min(rows, key=lambda row: abs(median(w.box.cy for w in row) - box.cy))

    return sorted(nearest, key=lambda word: word.box.x)


def words_in_box(
    label: LabelMatch,
    words: list[Word],
    offsets: tuple[float, float, float, float],
    page_width: int,
) -> list[Word]:
    """
    Palabras dentro de una caja ubicada respecto a la etiqueta.

    `offsets` = (x0, y0, x1, y1) en fracciones del ancho de página:
    x relativo al borde izquierdo de la etiqueta, y relativo a su centro.
    Sirve para valores cuya etiqueta casi nunca se lee, ubicándolos
    respecto a otra etiqueta fiable de la misma plantilla.
    """
    region = offsets_region(label.box, offsets, page_width)
    own = set(id(word) for word in label.words)

    return [word for word in words_inside(region, words) if id(word) not in own]


def offsets_region(box: Box, offsets: tuple[float, float, float, float], page_width: int) -> Box:
    x0, y0, x1, y1 = offsets
    left = box.x + x0 * page_width
    top = box.cy + y0 * page_width
    right = box.x + x1 * page_width
    bottom = box.cy + y1 * page_width

    return Box(int(left), int(top), int(right - left), int(bottom - top))


def rows_below(
    label: LabelMatch,
    words: list[Word],
    max_dy: float,
    x_pad: float,
) -> list[list[Word]]:
    """
    Filas de palabras situadas debajo de la etiqueta, de la más cercana
    a la más lejana. Solo se consideran palabras que se solapan
    horizontalmente con la etiqueta ampliada en `x_pad`.
    """
    box = label.box
    left = box.x - x_pad
    right = box.right + x_pad
    own = set(id(word) for word in label.words)

    selected = [
        word
        for word in words
        if id(word) not in own
        and word.box.right >= left
        and word.box.x <= right
        and word.box.y >= box.bottom - 0.25 * label.height
        and word.box.y <= box.bottom + max_dy
    ]

    return [line.words for line in build_lines(selected)]


def words_inside(region: Box, words: list[Word]) -> list[Word]:
    return [word for word in words if region.contains_center_of(word.box)]


def representative_match(matches: list[LabelMatch]) -> LabelMatch | None:
    """La ubicación más repetida entre pasadas; en empate, la más confiable."""
    if not matches:
        return None

    def votes(match):
        return sum(
            1
            for other in matches
            if abs(other.box.cx - match.box.cx) < 3 * match.height
            and abs(other.box.cy - match.box.cy) < match.height
        )

    return max(matches, key=lambda match: (votes(match), match.confidence))
