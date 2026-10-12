"""
Construcción del recibo estructurado a partir de las lecturas OCR.

Flujo:
1. Detectar la empresa por palabras clave.
2. Para cada campo, buscar sus etiquetas en cada lectura (página
   completa y zonas releídas) y tomar el valor a la derecha o debajo.
3. Agrupar evidencias por valor y elegir candidato (candidates.decide).
4. Leer la gráfica de consumo por coordenadas (charts).
5. Validaciones cruzadas (período coherente, orden de fechas).
6. Devolver `datos` (editable), `revision` (dudas) y `evidencia`.
"""

import re
from datetime import date

from app.candidates import Evidence, FieldDecision, decide
from app.charts import extract_series, locate_chart
from app.layout import (
    Box,
    LabelMatch,
    Word,
    build_lines,
    find_label,
    label_key,
    rows_below,
    virtual_label,
    words_from_reading,
    words_in_box,
    words_right_of,
)
from app.providers import PROVIDERS, FieldSpec, LabelRule, Provider
from app.values import (
    clean_address,
    find_amounts,
    find_codes,
    find_dates,
    find_decimal,
    find_identifiers,
    find_integers,
    find_months,
)


SCHEMA_VERSION = "2.0"

# Campos que Flutter siempre muestra, aunque no se hayan encontrado.
PRIORITY_FIELDS = (
    "identificador.valor",
    "numero_factura",
    "direccion_suministro",
    "periodo.mes",
    "periodo.inicio",
    "periodo.fin",
    "fechas.emision",
    "fechas.pago_oportuno",
    "fechas.suspension",
    "valores.total_facturado",
    "consumo.actual",
    "consumo.historico",
)


# ---------------------------------------------------------------------------
# Empresa
# ---------------------------------------------------------------------------

def detect_provider(readings: list[dict]) -> tuple[Provider | None, dict]:
    texts = [label_key(reading.get("text", "")) for reading in readings]
    scores = {}

    for provider in PROVIDERS:
        scores[provider.name] = sum(
            1
            for keyword in provider.keywords
            for text in texts
            if re.search(keyword, text)
        )

    ranking = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_name, best_score = ranking[0]
    second_score = ranking[1][1] if len(ranking) > 1 else 0

    if best_score == 0 or best_score < 2 * second_score:
        return None, scores

    provider = next(p for p in PROVIDERS if p.name == best_name)

    return provider, scores


# ---------------------------------------------------------------------------
# Extracción de valores junto a etiquetas
# ---------------------------------------------------------------------------

def _clean_tokens(words: list[Word]) -> list[Word]:
    """Quita palabras que son solo puntuación (":", "|", "$" suelto se conserva)."""
    return [
        word
        for word in words
        if re.search(r"[A-Za-z0-9$]", word.text)
    ]


def _finder(spec: FieldSpec):
    kind = spec.kind

    if kind == "identificador":
        return lambda text: find_identifiers(text, spec.min_length, spec.max_length)
    if kind == "fecha":
        return find_dates
    if kind == "mes":
        return find_months
    if kind == "importe":
        return lambda text: find_amounts(text, spec.minimum)
    if kind == "entero":
        return lambda text: find_integers(text, 1, spec.max_length)
    if kind == "decimal":
        return find_decimal

    raise ValueError(f"Tipo de campo sin buscador: {kind}")


def _first_value(tokens: list[Word], finder, max_window: int = 4):
    """El valor más cercano a la etiqueta, con la menor cantidad de palabras."""
    for start in range(len(tokens)):
        for end in range(start, min(len(tokens), start + max_window)):
            text = " ".join(token.text for token in tokens[start:end + 1])
            found = finder(text)

            if found:
                return found[0], tokens[start:end + 1]

    return None


def _box_of(tokens: list[Word]):
    box = tokens[0].box

    for token in tokens[1:]:
        box = box.union(token.box)

    return box


def _segment(tokens: list[Word], max_gap_factor: float) -> list[Word]:
    """Primer tramo continuo de palabras (sin huecos grandes)."""
    if not tokens:
        return []

    segment = [tokens[0]]

    for token in tokens[1:]:
        gap = token.box.x - segment[-1].box.right

        if gap > max_gap_factor * max(segment[-1].box.h, token.box.h):
            break

        segment.append(token)

    return segment


def _evidence(value, note, tokens, rule, label, method=None):
    return Evidence(
        value=value,
        raw=" ".join(token.text for token in tokens),
        method=method or rule.direction,
        source=tokens[0].source,
        confidence=min(token.conf for token in tokens),
        box=_box_of(tokens),
        label=label.line.text if label else None,
        note=note,
    )


def _values_from_tokens(spec: FieldSpec, tokens: list[Word], rule, label, method=None) -> list[Evidence]:
    if spec.kind == "direccion":
        # En direcciones el guion separa la placa ("63 - 28"): se conserva.
        tokens = [token for token in tokens if re.search(r"[A-Za-z0-9-]", token.text)]
    else:
        tokens = _clean_tokens(tokens)

    if not tokens:
        return []

    if spec.kind == "direccion":
        segment = _segment(tokens, 2.5)[:12]
        value, note = clean_address(" ".join(token.text for token in segment))

        return [_evidence(value, note, segment, rule, label, method)] if value else []

    if spec.kind == "codigo":
        # Une palabras casi pegadas: el OCR a veces corta el número
        # ("00012026A 102354386"). Un hueco grande o una palabra sin
        # dígitos termina el código.
        first_with_digit = next((i for i, t in enumerate(tokens) if re.search(r"\d", t.text)), None)

        if first_with_digit is None:
            return []

        segment = []

        for token in _segment(tokens[first_with_digit:], 0.8):
            if not re.search(r"\d", token.text):
                break
            segment.append(token)
        joined = "".join(token.text for token in segment)
        found = find_codes(joined, spec.min_length, spec.max_length)

        if not found:
            return []

        note = "codigo_unido" if len(segment) > 1 else None

        return [_evidence(found[0][0], note, segment, rule, label, method)]

    if spec.kind == "periodo":
        dates = []

        for token in tokens[:6]:
            for value, note in find_dates(token.text):
                dates.append((value, note, token))

        if len(dates) < 2:
            return []

        (start, start_note, start_token), (end, end_note, end_token) = dates[:2]

        return [
            _evidence(start, start_note, [start_token], rule, label, method),
            _evidence(end, end_note, [end_token], rule, label, method),
        ]

    result = _first_value(tokens, _finder(spec))

    if not result:
        return []

    (value, note), used = result

    return [_evidence(value, note, used, rule, label, method)]


def _field_evidence(
    spec: FieldSpec,
    rule: LabelRule,
    label: LabelMatch,
    words: list[Word],
    page_width: int,
) -> list[Evidence]:
    if rule.direction == "derecha":
        tokens = words_right_of(label, words, rule.reach * page_width, rule.band)

        return _values_from_tokens(spec, tokens, rule, label)

    if rule.direction == "caja":
        tokens = words_in_box(label, words, rule.box, page_width)

        for line in build_lines(tokens):
            found = _values_from_tokens(spec, line.words, rule, label, method="plantilla")

            if found:
                return found

        return []

    rows = rows_below(label, words, rule.reach * page_width, rule.x_pad * page_width)

    # Dirección: solo la primera fila y solo si está justo debajo de la
    # etiqueta. Si el OCR se saltó esa fila, la siguiente es el barrio.
    if spec.kind == "direccion":
        candidates_rows = [
            row
            for row in rows[:1]
            if min(word.box.y for word in row) <= label.box.bottom + 1.0 * label.height
        ]
    else:
        candidates_rows = rows[:2]

    for row in candidates_rows:
        found = _values_from_tokens(spec, row, rule, label)

        if found:
            return found

    return []


def collect_evidence(
    provider: Provider,
    words_by_reading: dict[str, list[Word]],
    lines_by_reading: dict[str, list],
    page_width: int,
    anchors_by_reading: dict[str, list[dict]] | None = None,
) -> dict[str, list[Evidence]]:
    """
    Evidencias por campo.

    En cada lectura se buscan las etiquetas del campo. En las zonas
    releídas además se usa como ancla virtual la etiqueta que originó la
    zona (su caja viene de la pasada de página completa), porque a veces
    el recorte lee el valor pero no la etiqueta.
    """
    evidence: dict[str, list[Evidence]] = {}
    anchors_by_reading = anchors_by_reading or {}

    def add(spec, found):
        if spec.kind == "periodo":
            if len(found) == 2:
                evidence.setdefault("periodo.inicio", []).append(found[0])
                evidence.setdefault("periodo.fin", []).append(found[1])
            return

        key = "identificador.valor" if spec.key == "identificador" else spec.key
        evidence.setdefault(key, []).extend(found)

    for spec in provider.fields:
        for rule_index, rule in enumerate(spec.labels):
            for name, lines in lines_by_reading.items():
                labels = list(find_label(lines, rule.pattern))

                for anchor in anchors_by_reading.get(name, []):
                    if anchor["field"] == spec.key and anchor["rule"] == rule_index:
                        labels.append(
                            virtual_label(Box(*anchor["box"]), anchor["height"], anchor["text"], name)
                        )

                for label in labels:
                    add(spec, _field_evidence(spec, rule, label, words_by_reading[name], page_width))

    return evidence


# ---------------------------------------------------------------------------
# Validaciones cruzadas
# ---------------------------------------------------------------------------

def _days_between(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _coherent_period(decisions: dict[str, FieldDecision], warnings: list[str]):
    start = decisions.get("periodo.inicio")
    end = decisions.get("periodo.fin")

    if not start or not end or not start.groups or not end.groups:
        return

    best = None

    for start_group in start.groups[:3]:
        for end_group in end.groups[:3]:
            days = _days_between(start_group.value, end_group.value)

            if 20 <= days <= 40:
                total = start_group.score + end_group.score

                if best is None or total > best[0]:
                    best = (total, start_group, end_group)

    if best is None:
        for decision in (start, end):
            if decision.value is not None:
                decision.status = "dudoso"
                decision.confidence = "baja"
                decision.reason = "Las fechas del período no forman un rango mensual coherente."
        return

    _, start_group, end_group = best

    for decision, group in ((start, start_group), (end, end_group)):
        if decision.chosen is not group:
            decision.chosen = group
            decision.value = group.value
            decision.status = "dudoso"
            decision.confidence = "baja"
            decision.reason = (
                "Se eligió este valor porque forma un período coherente; "
                "otra lectura tenía más respaldo."
            )

        # Las alternativas que no forman un período coherente se anotan.
        for other in decision.groups:
            if other is not group:
                other.notes.append("periodo_incoherente")

        if decision.status == "dudoso" and decision.reason == "Hay lecturas distintas con respaldo similar.":
            # Las demás lecturas producen períodos imposibles.
            decision.status = "propuesto"
            decision.confidence = "media"
            decision.reason = "Se descartaron lecturas que no forman un período de un mes."


DATE_FIELDS = (
    "fechas.emision",
    "fechas.pago_oportuno",
    "fechas.suspension",
    "periodo.inicio",
    "periodo.fin",
)
MAX_DATE_SPREAD_DAYS = 60


def _coherent_dates(decisions: dict[str, FieldDecision]):
    """
    Las fechas de un recibo están a pocas semanas entre sí. Una lectura
    a meses de distancia de las demás (p. ej. 2025 en lugar de 2026, una
    confusión 6/5 que el OCR repite en varias pasadas) se anota como
    incoherente: si hay una alternativa coherente se propone esa, y si
    no, el campo queda como dudoso.
    """
    month = decisions.get("periodo.mes")

    for key in DATE_FIELDS:
        decision = decisions.get(key)

        if not decision or not decision.groups:
            continue

        references = sorted(
            date.fromisoformat(decisions[other].value)
            for other in DATE_FIELDS
            if other != key and decisions.get(other) and decisions[other].value
        )

        if month and month.value:
            references.append(date.fromisoformat(f"{month.value}-15"))
            references.sort()

        if len(references) < 2:
            continue

        reference = references[len(references) // 2]

        def coherent(group):
            days = abs((date.fromisoformat(group.value) - reference).days)
            return days <= MAX_DATE_SPREAD_DAYS

        for group in decision.groups:
            if not coherent(group) and "fecha_incoherente" not in group.notes:
                group.notes.append("fecha_incoherente")

        if decision.chosen is None:
            continue

        if not coherent(decision.chosen):
            alternative = next((group for group in decision.groups if coherent(group)), None)
            decision.status = "dudoso"
            decision.confidence = "baja"

            if alternative:
                decision.chosen = alternative
                decision.value = alternative.value
                decision.reason = "Se propone la lectura coherente con las demás fechas del recibo."
            else:
                decision.reason = "La fecha está lejos de las demás fechas del recibo."

        elif decision.status == "dudoso" and decision.reason == "Hay lecturas distintas con respaldo similar.":
            rivals = [
                group
                for group in decision.groups
                if group is not decision.chosen
                and group.score >= 0.5 * decision.chosen.score
                and not any(note.startswith("parcial_de:") for note in group.notes)
            ]

            if rivals and all(not coherent(group) for group in rivals):
                decision.status = "propuesto"
                decision.confidence = "media"
                decision.reason = "Se descartaron lecturas incoherentes con las demás fechas."


def _date_order(decisions: dict[str, FieldDecision], warnings: list[str]):
    order = ["fechas.emision", "fechas.pago_oportuno", "fechas.suspension"]
    values = [
        (key, decisions[key].value)
        for key in order
        if key in decisions and decisions[key].value
    ]

    for (first_key, first), (second_key, second) in zip(values, values[1:]):
        if first > second:
            for key in (first_key, second_key):
                decisions[key].status = "dudoso"
                decisions[key].confidence = "baja"
                decisions[key].reason = "El orden de las fechas no es coherente (emisión ≤ pago ≤ suspensión)."

            warnings.append(f"Orden de fechas incoherente entre {first_key} y {second_key}.")


def _reading_check(decisions: dict[str, FieldDecision]):
    """Si las lecturas del medidor coinciden con el consumo, se anota."""
    current = decisions.get("adicional.lectura_actual")
    previous = decisions.get("adicional.lectura_anterior")
    consumption = decisions.get("consumo.actual")

    if not (current and previous and consumption):
        return

    if None in (current.value, previous.value, consumption.value):
        return

    if current.value - previous.value == consumption.value and consumption.chosen:
        consumption.chosen.notes.append("coincide_con_lecturas_del_medidor")

        if consumption.status == "propuesto":
            consumption.confidence = "alta"


# ---------------------------------------------------------------------------
# Histórico
# ---------------------------------------------------------------------------

def _build_history(provider, chart_result, decisions, no_history, unit):
    """
    Devuelve (lista, estado, motivo, decisiones_por_punto).

    `posicion` 1 es el período actual; 2 el anterior, etc.
    """
    actual = decisions.get("consumo.actual")
    actual_value = actual.value if actual else None
    points = []
    point_reviews = {}

    if chart_result and chart_result.get("slots"):
        slots = chart_result["slots"]
        current_index = len(slots) - 1

        for index, (decision, period) in enumerate(slots):
            position = current_index - index + 1
            value = decision.value if decision else None

            if decision and decision.status != "propuesto":
                point_reviews[f"consumo.historico[{position}]"] = decision.review()

            points.append({
                "posicion": position,
                "valor": value,
                "unidad": unit,
                "periodo": period,
                "es_actual": index == current_index,
            })

        points.sort(key=lambda point: point["posicion"])
        unread = [point["posicion"] for point in points if point["valor"] is None]

        if unread:
            state = "parcial"
            reason = (
                "No se pudo leer el valor de las barras en posición "
                + ", ".join(map(str, unread))
                + ". Se dejan vacías; no se estiman."
            )
        else:
            state = "completo"
            reason = None

        if chart_result.get("notas"):
            reason = " ".join(filter(None, [reason, *chart_result["notas"]]))

        return points, state, reason, point_reviews

    if actual_value is not None:
        points.append({
            "posicion": 1,
            "valor": actual_value,
            "unidad": unit,
            "periodo": decisions["periodo.mes"].value if "periodo.mes" in decisions else None,
            "es_actual": True,
        })

    if no_history:
        return points, "declarado_sin_historico", "El recibo indica que no hay consumos anteriores.", point_reviews

    if provider.chart:
        reason = (chart_result or {}).get("motivo") or "No se encontró la gráfica de consumo."

        if points:
            return points, "parcial", reason + " Solo se tiene el consumo del mes.", point_reviews

        return points, "no_extraido", reason, point_reviews

    return points, ("parcial" if points else "no_extraido"), "Este recibo no muestra histórico de consumo reconocible.", point_reviews


# ---------------------------------------------------------------------------
# Respuesta
# ---------------------------------------------------------------------------

def _set_path(data: dict, path: str, value):
    parts = path.split(".")
    target = data

    for part in parts[:-1]:
        target = target.setdefault(part, {})

    target[parts[-1]] = value


def _empty_data(provider: Provider | None) -> dict:
    return {
        "empresa": provider.name if provider else None,
        "servicio": provider.service if provider else None,
        "identificador": {
            "tipo": provider.identifier_type if provider else None,
            "valor": None,
        },
        "numero_factura": None,
        "direccion_suministro": None,
        "periodo": {"mes": None, "inicio": None, "fin": None},
        "fechas": {"emision": None, "pago_oportuno": None, "suspension": None},
        "valores": {"moneda": "COP", "total_facturado": None},
        "consumo": {
            "unidad": provider.unit if provider else None,
            "actual": None,
            "historico": [],
            "estado_historico": "no_extraido",
        },
        "adicional": {},
    }


def _summaries(readings: list[dict], include_words: bool) -> list[dict]:
    summaries = []

    for reading in readings:
        item = {
            "nombre": reading["name"],
            "psm": reading.get("psm"),
            "segundos": reading.get("seconds"),
            "confianza_media": reading.get("mean_confidence"),
            "palabras": reading.get("word_count"),
            "texto": reading.get("text", ""),
        }

        if reading.get("region"):
            item["region"] = reading["region"]["box"]

        if include_words:
            item["detalle_palabras"] = reading.get("words", [])

        summaries.append(item)

    return summaries


def build_receipt(ocr_result: dict, include_words: bool = False) -> dict:
    readings = [
        reading
        for reading in ocr_result.get("readings", [])
        if reading.get("text", "").strip()
    ]
    height, width = ocr_result["shape"][:2]
    base = [reading for reading in readings if not reading["name"].startswith("zona:")]
    provider, provider_scores = detect_provider(base)
    data = _empty_data(provider)
    warnings = list(ocr_result.get("warnings", []))
    review_fields: dict[str, dict] = {}
    field_evidence: dict[str, list[dict]] = {}

    evidencia = {
        "proveedor_puntajes": provider_scores,
        "tiempo_segundos": ocr_result.get("elapsed_seconds"),
        "lecturas": _summaries(readings, include_words),
    }

    if provider is None:
        for key in ("empresa", "servicio", *PRIORITY_FIELDS):
            review_fields[key] = {"estado": "no_encontrado"}

        return {
            "version_esquema": SCHEMA_VERSION,
            "estado": "pendiente_revision",
            "datos": data,
            "revision": {
                "requiere_confirmacion": True,
                "campos_pendientes": list(review_fields),
                "campos": review_fields,
                "advertencias": [
                    "No se reconoció la empresa (Afinia, Surtigas o Aguas de Cartagena).",
                    *warnings,
                ],
            },
            "evidencia": evidencia,
        }

    words_by_reading = {reading["name"]: words_from_reading(reading) for reading in readings}
    lines_by_reading = {name: build_lines(words) for name, words in words_by_reading.items()}
    anchors_by_reading = {
        reading["name"]: reading["region"].get("anchors", [])
        for reading in readings
        if reading.get("region")
    }
    evidence = collect_evidence(provider, words_by_reading, lines_by_reading, width, anchors_by_reading)
    specs = {
        ("identificador.valor" if spec.key == "identificador" else spec.key): spec
        for spec in provider.fields
    }
    decisions: dict[str, FieldDecision] = {}

    for key, items in evidence.items():
        # La misma palabra puede llegar por la etiqueta leída y por el
        # ancla virtual de su zona: se cuenta una vez.
        unique = {}

        for item in items:
            box = item.box.to_dict() if item.box else None
            unique.setdefault((item.source, str(item.value), str(box)), item)

        items = list(unique.values())
        spec = specs.get(key)
        decisions[key] = decide(
            key,
            items,
            expected_format=spec.expected_format if spec else None,
            require_format=spec.require_format if spec else False,
        )

    for key in PRIORITY_FIELDS:
        if key != "consumo.historico":
            decisions.setdefault(key, FieldDecision(key=key))

    applicable = set(specs)

    if "periodo" in applicable:
        applicable |= {"periodo.inicio", "periodo.fin"}
    if provider.chart:
        applicable.add("consumo.actual")

    # Gráfica de consumo.
    chart_result = None

    if provider.chart:
        base_lines = {name: lines for name, lines in lines_by_reading.items() if not name.startswith("zona:")}
        located = locate_chart(provider.chart, base_lines, (height, width))

        if located:
            region, title_height = located
            chart_result = extract_series(
                provider.chart,
                region,
                title_height,
                words_by_reading,
                lines_by_reading,
                decisions["periodo.mes"].value,
            )
            evidencia["grafica"] = {
                "region": region.to_dict(),
                "notas": chart_result.get("notas", []),
                "motivo": chart_result.get("motivo"),
            }

            # El valor del período actual en la gráfica es evidencia de consumo.actual.
            slots = chart_result.get("slots") or []

            if slots and slots[-1][0] is not None and slots[-1][0].chosen:
                chart_evidence = list(slots[-1][0].chosen.evidences)
                merged = evidence.get("consumo.actual", []) + chart_evidence
                decisions["consumo.actual"] = decide("consumo.actual", merged)

    no_history = False

    if provider.no_history_phrase:
        no_history = any(
            find_label(lines, provider.no_history_phrase)
            for lines in lines_by_reading.values()
        )

    _coherent_period(decisions, warnings)
    _coherent_dates(decisions)
    _date_order(decisions, warnings)
    _reading_check(decisions)

    # Datos editables.
    for key, decision in decisions.items():
        if decision.value is not None:
            _set_path(data, key, decision.value)

    history, state, reason, point_reviews = _build_history(
        provider,
        chart_result,
        decisions,
        no_history,
        provider.unit,
    )

    # Si la barra actual no se leyó pero el consumo del mes sí (en otra
    # parte del recibo), es el mismo dato: se usa para el punto actual.
    current_point = next((point for point in history if point["es_actual"]), None)

    if current_point and current_point["valor"] is None and decisions["consumo.actual"].value is not None:
        current_point["valor"] = decisions["consumo.actual"].value

    if current_point and current_point["periodo"] is None and decisions["periodo.mes"].value:
        current_point["periodo"] = decisions["periodo.mes"].value

    data["consumo"]["historico"] = history
    data["consumo"]["estado_historico"] = state

    # Revisión.
    for key in PRIORITY_FIELDS:
        if key == "consumo.historico":
            review_fields[key] = {"estado": state}

            if reason:
                review_fields[key]["motivo"] = reason
            continue

        if key not in applicable and decisions[key].value is None:
            # La empresa no imprime este dato (p. ej. Afinia no trae "mes
            # facturado"): no es un dato perdido ni pendiente de revisar.
            review_fields[key] = {"estado": "no_aplica"}
            continue

        review_fields[key] = decisions[key].review()

    for key, decision in decisions.items():
        if key not in review_fields and decision.value is not None:
            review_fields[key] = decision.review()

        if decision.groups:
            field_evidence[key] = decision.evidence()

    review_fields.update(point_reviews)

    pending = [
        key
        for key, review in review_fields.items()
        if review.get("estado") not in ("propuesto", "completo", "declarado_sin_historico", "no_aplica")
        or review.get("confianza") == "baja"
    ]

    evidencia["campos"] = field_evidence

    return {
        "version_esquema": SCHEMA_VERSION,
        "estado": "pendiente_revision",
        "datos": data,
        "revision": {
            "requiere_confirmacion": True,
            "campos_pendientes": pending,
            "campos": review_fields,
            "advertencias": [
                "Datos propuestos por OCR; el usuario debe confirmarlos o corregirlos.",
                "El total facturado no indica que el recibo esté pagado.",
                *provider.notes,
                *warnings,
            ],
        },
        "evidencia": evidencia,
    }
