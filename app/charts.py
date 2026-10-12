"""
Lectura de las gráficas de consumo usando coordenadas.

El orden del texto OCR no respeta el orden de las barras, así que:

1. Se localiza la gráfica con etiquetas ancla (título y borde inferior).
2. Se toman solo los números dentro de esa región, de todas las pasadas.
3. Se agrupan por posición horizontal: cada grupo es una barra.
4. Si la separación entre barras es regular y falta una, se deja su
   casilla con valor null. Nunca se rellena el número.
5. El punto actual se identifica por un ancla ("Período actual") o por
   la etiqueta del mes; si no hay ancla, se asume la última barra y se
   avisa.
"""

import re
from statistics import median

from app.candidates import Evidence, decide
from app.layout import Box, Line, Word, find_label, representative_match, words_inside
from app.providers import ChartSpec


def locate_chart(
    spec: ChartSpec,
    lines_by_reading: dict[str, list[Line]],
    shape: tuple[int, int],
) -> tuple[Box, float] | None:
    height, width = shape
    titles = [
        match
        for lines in lines_by_reading.values()
        for match in find_label(lines, spec.title)
    ]
    title = representative_match(titles)

    if title is None:
        return None

    top = title.box.bottom
    limit = top + spec.max_height * height
    bottoms = [
        match
        for lines in lines_by_reading.values()
        for match in find_label(lines, spec.bottom)
        if top < match.box.y <= limit
    ] if spec.bottom else []
    bottom = min((match.box.y for match in bottoms), default=limit)

    if spec.x_relative:
        x1 = title.box.x + spec.x_range[0] * width
        x2 = title.box.x + spec.x_range[1] * width
    else:
        x1 = spec.x_range[0] * width
        x2 = spec.x_range[1] * width

    x1 = int(max(0, x1))
    x2 = int(min(width, x2))
    y1 = int(max(0, top))
    y2 = int(min(height, bottom))

    if x2 - x1 < 50 or y2 - y1 < 30:
        return None

    return Box(x1, y1, x2 - x1, y2 - y1), title.height


def _numeric_tokens(words: list[Word], max_digits: int) -> list[tuple[Word, int]]:
    tokens = []

    for word in words:
        text = word.text.strip("|.,:;'\"`´()[]{}")

        if re.fullmatch(rf"\d{{1,{max_digits}}}", text):
            tokens.append((word, int(text)))

    return tokens


def _cluster_by_x(items: list[tuple[Word, object]], tolerance: float) -> list[list[tuple[Word, object]]]:
    clusters: list[list[tuple[Word, object]]] = []

    for item in sorted(items, key=lambda item: item[0].box.cx):
        if clusters:
            center = median(other[0].box.cx for other in clusters[-1])

            if item[0].box.cx - center <= tolerance:
                clusters[-1].append(item)
                continue

        clusters.append([item])

    return clusters


def _slot_spacing(centers: list[float]) -> float | None:
    if len(centers) < 3:
        return None

    diffs = [b - a for a, b in zip(centers, centers[1:])]
    smallest = min(diffs)
    regular = [diff for diff in diffs if diff <= 1.5 * smallest]

    return median(regular) if regular else None


def _with_gaps(clusters: list, spacing: float | None) -> list:
    """Inserta casillas vacías donde la separación indica una barra no leída."""
    if spacing is None or not clusters:
        return list(clusters)

    slots = [clusters[0]]

    for previous, current in zip(clusters, clusters[1:]):
        prev_x = median(item[0].box.cx for item in previous)
        curr_x = median(item[0].box.cx for item in current)
        ratio = (curr_x - prev_x) / spacing
        missing = round(ratio) - 1

        if missing >= 1 and abs(ratio - round(ratio)) < 0.25:
            slots.extend([None] * missing)

        slots.append(current)

    return slots


def _slot_decision(index: int, cluster) -> tuple:
    evidences = [
        Evidence(
            value=value,
            raw=word.text,
            method="grafica",
            source=word.source,
            confidence=word.conf,
            box=word.box,
        )
        for word, value in cluster
    ]

    return decide(f"consumo.historico[{index}]", evidences)


def extract_series(
    spec: ChartSpec,
    region: Box,
    title_height: float,
    words_by_reading: dict[str, list[Word]],
    lines_by_reading: dict[str, list[Line]],
    current_month: str | None,
) -> dict:
    words = [
        word
        for reading_words in words_by_reading.values()
        for word in words_inside(region, reading_words)
    ]

    if spec.layout == "fila":
        return _series_in_row(spec, region, title_height, words, lines_by_reading)

    return _series_over_bars(spec, region, words, current_month)


def _series_in_row(spec, region, title_height, words, lines_by_reading) -> dict:
    tokens = _numeric_tokens(words, spec.max_digits)

    if not tokens:
        return {"slots": [], "motivo": "No se leyó ningún valor dentro de la gráfica."}

    # Fila dominante: la que tiene más posiciones horizontales distintas.
    typical_height = median(word.box.h for word, _ in tokens)
    rows: list[list] = []

    for item in sorted(tokens, key=lambda item: item[0].box.cy):
        if rows and abs(item[0].box.cy - median(i[0].box.cy for i in rows[-1])) <= 0.7 * typical_height:
            rows[-1].append(item)
        else:
            rows.append([item])

    typical_width = median(word.box.w for word, _ in tokens)
    tolerance = 0.6 * typical_width
    row = max(rows, key=lambda r: (len(_cluster_by_x(r, tolerance)), r[0][0].box.cy))
    clusters = _cluster_by_x(row, tolerance)
    centers = [median(item[0].box.cx for item in cluster) for cluster in clusters]
    spacing = _slot_spacing(centers)
    slots = _with_gaps(clusters, spacing)
    notes = []

    # Ancla del período actual (encima de la última barra).
    current_index = len(slots) - 1
    anchor_found = False

    if spec.current_anchor and spacing:
        anchors = [
            match
            for lines in lines_by_reading.values()
            for match in find_label(lines, spec.current_anchor)
            if region.x <= match.box.cx <= region.right
            and region.y - 4 * title_height <= match.box.cy <= region.bottom
        ]

        if anchors:
            anchor_x = median(match.box.cx for match in anchors)
            slot_x = _slot_centers(slots, centers, spacing)
            closest = min(range(len(slots)), key=lambda i: abs(slot_x[i] - anchor_x))

            if abs(slot_x[closest] - anchor_x) <= spacing:
                anchor_found = True
                current_index = closest

    if not anchor_found:
        notes.append("Se asume que la última barra leída es el período actual.")

    if current_index < len(slots) - 1:
        notes.append("Se descartaron valores a la derecha del período actual.")
        slots = slots[: current_index + 1]

    # Barras iniciales no leídas: solo si el ancla confirma el período
    # actual, se conoce el número esperado y hay espacio en la región.
    if anchor_found and spec.expected_points and spacing and len(slots) < spec.expected_points:
        first_x = _slot_centers(slots, centers, spacing)[0]

        while (
            len(slots) < spec.expected_points
            and first_x - spacing >= region.x + 0.2 * spacing
        ):
            slots.insert(0, None)
            first_x -= spacing

    if spec.expected_points and len(slots) < spec.expected_points:
        notes.append(
            f"Se esperaban {spec.expected_points} barras y se ubicaron {len(slots)}."
        )

    return {
        "slots": [
            (_slot_decision(index, slot) if slot else None, None)
            for index, slot in enumerate(slots)
        ],
        "current_index": len(slots) - 1,
        "anchor_found": anchor_found,
        "notas": notes,
    }


def _slot_centers(slots, centers, spacing) -> list[float]:
    """Centro x de cada casilla, incluidas las vacías."""
    result = []
    known = iter(centers)
    last = None

    for slot in slots:
        if slot is None:
            result.append(None)
        else:
            last = next(known)
            result.append(last)

    # Rellena las vacías por interpolación con la separación regular.
    for index, value in enumerate(result):
        if value is None:
            left = next((result[i] + (index - i) * spacing for i in range(index - 1, -1, -1) if result[i] is not None), None)
            right = next((result[i] - (i - index) * spacing for i in range(index + 1, len(result)) if result[i] is not None), None)
            result[index] = left if left is not None else right

    return result


def _series_over_bars(spec, region, words, current_month) -> dict:
    months = []

    for word in words:
        match = re.fullmatch(r"(\d{2})/(\d{2})", word.text.strip("|.,:;'\""))

        if match and 1 <= int(match.group(1)) <= 12:
            months.append((word, f"20{match.group(2)}-{match.group(1)}"))

    if len(months) < 2:
        return {
            "slots": [],
            "motivo": (
                "No se pudieron leer las etiquetas de mes de la gráfica; "
                "el texto es demasiado pequeño para el OCR."
            ),
        }

    typical_width = median(word.box.w for word, _ in months)
    clusters = _cluster_by_x(months, 0.5 * typical_width)
    centers = [median(item[0].box.cx for item in cluster) for cluster in clusters]
    spacing = _slot_spacing(centers) or typical_width * 1.5
    tokens = _numeric_tokens(words, spec.max_digits)
    month_y = median(word.box.cy for word, _ in months)
    slots = []

    for index, cluster in enumerate(clusters):
        period_decision = decide("periodo", [
            Evidence(value=value, raw=word.text, method="grafica", source=word.source, confidence=word.conf, box=word.box)
            for word, value in cluster
        ])
        values = [
            (word, value)
            for word, value in tokens
            if abs(word.box.cx - centers[index]) <= 0.45 * spacing
            and word.box.cy < month_y - word.box.h
        ]
        slots.append((_slot_decision(index, values) if values else None, period_decision.value))

    current_index = len(slots) - 1
    notes = []

    if current_month and current_month in [period for _, period in slots]:
        current_index = [period for _, period in slots].index(current_month)
    else:
        notes.append("Se asume que la última barra es el período actual.")

    return {
        "slots": slots[: current_index + 1],
        "current_index": current_index,
        "anchor_found": current_month is not None,
        "notas": notes,
    }
