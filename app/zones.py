"""
Planificación de relecturas enfocadas.

Las pasadas de página completa encuentran las etiquetas con bastante
fiabilidad, pero a menudo fallan en el valor (fuentes grandes, texto
blanco sobre color, números pequeños). Aquí se recorta la vecindad de
cada etiqueta encontrada, incluyendo la etiqueta, para que el valor se
pueda volver a asociar con ella por posición.
"""

from statistics import median

from app.charts import locate_chart
from app.layout import Line, find_label, offsets_region, representative_match
from app.providers import Provider


MAX_REGIONS = 24

# (variante, psm, altura objetivo del texto en píxeles). Elegidas con
# tools/experimento_zonas.py sobre los recibos de prueba: varias
# combinaciones empataron; se usan dos lecturas distintas entre sí para
# que la coincidencia entre ellas aporte algo. Revisar con más fotos.
FIELD_READS = (("gray", 6, 40), ("normalized", 11, 60))
CHART_READS = (("gray", 11, 40), ("normalized", 6, 40))


def _distinct_locations(matches):
    """Una etiqueta por lugar del recibo (las pasadas la repiten)."""
    groups = []

    for match in matches:
        for group in groups:
            reference = group[0]

            if (
                abs(reference.box.cx - match.box.cx) < 3 * reference.height
                and abs(reference.box.cy - match.box.cy) < reference.height
            ):
                group.append(match)
                break
        else:
            groups.append([match])

    return [representative_match(group) for group in groups]


def _covered(inner, outer) -> bool:
    ix1, iy1, ix2, iy2 = inner
    ox1, oy1, ox2, oy2 = outer
    width = max(0, min(ix2, ox2) - max(ix1, ox1))
    height = max(0, min(iy2, oy2) - max(iy1, oy1))
    inner_area = (ix2 - ix1) * (iy2 - iy1)
    outer_area = (ox2 - ox1) * (oy2 - oy1)

    return (
        inner_area > 0
        and width * height >= 0.9 * inner_area
        and outer_area <= 3 * inner_area
    )


def plan_regions(
    provider: Provider,
    lines_by_reading: dict[str, list[Line]],
    shape: tuple[int, int],
) -> list[dict]:
    height, width = shape
    regions = []

    def clamp(x1, y1, x2, y2):
        return (
            int(max(0, x1)),
            int(max(0, y1)),
            int(min(width, x2)),
            int(min(height, y2)),
        )

    if provider.chart:
        located = locate_chart(provider.chart, lines_by_reading, shape)

        if located:
            box, title_height = located
            regions.append({
                "name": "grafica",
                "box": (box.x, box.y, box.right, box.bottom),
                "text_height": round(title_height, 1),
                "reads": CHART_READS,
            })

    # Las etiquetas dentro de casillas con borde reciben cajas más altas
    # que su texto. Se limita la altura a 1,5 veces la altura típica de
    # las palabras de la página para no agrandar la zona sin motivo.
    word_heights = [
        word.box.h
        for lines in lines_by_reading.values()
        for line in lines
        for word in line.words
    ]
    max_text_height = 1.5 * median(word_heights) if word_heights else None

    for spec in provider.fields:
        for rule_index, rule in enumerate(spec.labels):
            if not rule.reread:
                continue

            matches = [
                match
                for lines in lines_by_reading.values()
                for match in find_label(lines, rule.pattern)
            ]

            for location, label in enumerate(_distinct_locations(matches)):
                box = label.box
                text_height = label.height

                if max_text_height:
                    text_height = min(text_height, max_text_height)

                if rule.direction == "derecha":
                    # Centrada en la etiqueta: su caja puede incluir bordes.
                    half = (0.7 + rule.band + 0.3) * text_height
                    coords = clamp(
                        box.x - 0.5 * text_height,
                        box.cy - half,
                        box.right + rule.reach * width,
                        box.cy + half,
                    )
                elif rule.direction == "debajo":
                    coords = clamp(
                        box.x - rule.x_pad * width,
                        box.y - 0.5 * text_height,
                        box.right + rule.x_pad * width,
                        box.bottom + rule.reach * width,
                    )
                else:
                    # "caja": solo la caja del valor, con un pequeño margen.
                    target = offsets_region(box, rule.box, width)
                    coords = clamp(
                        target.x - text_height,
                        target.y - 0.5 * text_height,
                        target.right + text_height,
                        target.bottom + 0.5 * text_height,
                    )

                if coords[2] - coords[0] < 10 or coords[3] - coords[1] < 10:
                    continue

                regions.append({
                    "name": f"{spec.key}#{rule_index}.{location}",
                    "box": coords,
                    "text_height": round(text_height, 1),
                    "reads": FIELD_READS,
                    # Etiqueta que originó la zona: si el recorte lee el valor
                    # pero no la etiqueta, se usa como ancla virtual.
                    "anchors": [{
                        "field": spec.key,
                        "rule": rule_index,
                        "box": [box.x, box.y, box.w, box.h],
                        "height": round(text_height, 1),
                        "text": label.line.text,
                    }],
                })

    selected = []

    for region in regions:
        cover = next(
            (other for other in selected if _covered(region["box"], other["box"])),
            None,
        )

        if cover is not None:
            cover.setdefault("anchors", []).extend(region.get("anchors", []))
            continue

        selected.append(region)

    return selected[:MAX_REGIONS]
