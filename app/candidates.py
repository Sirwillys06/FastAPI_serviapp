"""
Agrupación de evidencias y elección de candidatos por campo.

Principios:

* Una lectura discrepante no anula a las demás: se agrupan valores,
  se puntúan y se conservan las alternativas para el usuario.
* Las pasadas OCR están correlacionadas (misma imagen, mismo motor),
  así que repetir el mismo valor en varias pasadas suma poco. Pesa más
  encontrar el valor junto a etiquetas en lugares distintos del recibo.
* Un valor contenido en otro más largo (27005 dentro de 527005) se
  marca como lectura parcial, no como competidor.
* La puntuación ordena candidatos; no es una probabilidad de acierto.
"""

import re
from dataclasses import dataclass, field

from app.layout import Box


# Ajuste del peso por la confianza que reporta Tesseract.
# Solo modula; una confianza alta no convierte un valor en correcto.
def _confidence_factor(confidence: float | None) -> float:
    if confidence is None or confidence < 0:
        return 0.8

    return 0.6 + 0.4 * min(confidence, 100) / 100


NOTE_PENALTY = {
    "separador_ausente": 0.7,
    "mes_con_cero_por_o": 0.9,
    "sin_separador_de_miles": 0.85,
    "nomenclatura_no_reconocida": 0.4,
    "codigo_unido": 0.95,
}


def family_of(source: str) -> str:
    if source.startswith("zona"):
        return "zona"
    if "adaptive" in source:
        return "adaptativa"
    if "normalized" in source:
        return "normalizada"
    return "gris"


@dataclass
class Evidence:
    value: object
    raw: str
    method: str          # "derecha", "debajo", "grafica", "frase"
    source: str          # nombre de la pasada OCR
    confidence: float | None
    box: Box | None
    label: str | None = None
    note: str | None = None

    @property
    def weight(self) -> float:
        weight = _confidence_factor(self.confidence)

        if self.note:
            weight *= NOTE_PENALTY.get(self.note, 0.8)

        # Ubicado por posición en la plantilla, sin leer su etiqueta.
        if self.method == "plantilla":
            weight *= 0.8

        return weight

    def to_dict(self) -> dict:
        data = {
            "valor": self.value,
            "texto": self.raw,
            "metodo": self.method,
            "lectura": self.source,
            "confianza_ocr": (
                round(self.confidence, 1)
                if self.confidence is not None
                else None
            ),
        }

        if self.label:
            data["etiqueta"] = self.label
        if self.note:
            data["nota"] = self.note
        if self.box:
            data["caja"] = self.box.to_dict()

        return data


@dataclass
class Group:
    value: object
    evidences: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def passes(self) -> int:
        return len({evidence.source for evidence in self.evidences})

    @property
    def families(self) -> int:
        return len({family_of(evidence.source) for evidence in self.evidences})

    @property
    def locations(self) -> int:
        return len(_cluster_locations(self.evidences))


def _cluster_locations(evidences: list[Evidence]) -> list[list[Evidence]]:
    """Agrupa evidencias que están en el mismo lugar del recibo."""
    clusters: list[list[Evidence]] = []

    for evidence in evidences:
        if evidence.box is None:
            clusters.append([evidence])
            continue

        for cluster in clusters:
            reference = cluster[0].box

            if reference is None:
                continue

            tolerance = 3 * max(reference.h, evidence.box.h, 12)

            if (
                abs(reference.cx - evidence.box.cx) <= tolerance * 3
                and abs(reference.cy - evidence.box.cy) <= tolerance
            ):
                cluster.append(evidence)
                break
        else:
            clusters.append([evidence])

    return clusters


def _score(group: Group) -> float:
    clusters = _cluster_locations(group.evidences)
    score = 0.0

    for cluster in clusters:
        best = max(evidence.weight for evidence in cluster)
        passes = len({evidence.source for evidence in cluster})
        # Repeticiones en el mismo lugar: aportan poco y con tope.
        score += best + 0.1 * min(passes - 1, 3)

    score += 0.15 * (group.families - 1)

    return score


def _is_partial(short: object, long: object) -> bool:
    """`short` parece una lectura incompleta de `long`."""
    if isinstance(short, bool) or isinstance(long, bool):
        return False

    if isinstance(short, int) and isinstance(long, int):
        short, long = str(short), str(long)

    if not isinstance(short, str) or not isinstance(long, str):
        return False

    if len(short) >= len(long):
        return False

    return short in long and len(short) >= 0.5 * len(long)


@dataclass
class FieldDecision:
    key: str
    value: object = None
    status: str = "no_encontrado"   # propuesto | dudoso | incompleto | no_encontrado
    confidence: str | None = None   # alta | media | baja
    reason: str | None = None
    groups: list[Group] = field(default_factory=list)
    chosen: Group | None = None

    def alternatives(self, limit: int = 4) -> list[dict]:
        items = []

        for group in self.groups:
            if group is self.chosen:
                continue

            item = {
                "valor": group.value,
                "lecturas": group.passes,
                "ubicaciones": group.locations,
            }

            if group.notes:
                item["notas"] = sorted(set(group.notes))

            items.append(item)

        return items[:limit]

    def evidence(self) -> list[dict]:
        return [
            evidence.to_dict()
            for group in self.groups
            for evidence in group.evidences
        ]

    def review(self) -> dict:
        data = {"estado": self.status}

        if self.confidence:
            data["confianza"] = self.confidence
        if self.chosen is not None:
            data["respaldo"] = {
                "lecturas": self.chosen.passes,
                "ubicaciones": self.chosen.locations,
            }
            if self.chosen.notes:
                data["respaldo"]["notas"] = sorted(set(self.chosen.notes))

        alternatives = self.alternatives()

        if alternatives:
            data["alternativas"] = alternatives
        if self.reason:
            data["motivo"] = self.reason

        return data


def decide(
    key: str,
    evidences: list[Evidence],
    expected_format: str | None = None,
    require_format: bool = False,
) -> FieldDecision:
    """
    Agrupa evidencias por valor y elige el mejor candidato.

    `expected_format`: expresión regular del formato observado en recibos
    reales. Un valor que no coincide pierde peso y queda anotado.
    `require_format`: si el mejor valor no coincide, no se propone
    (se usa para números de factura, donde un fragmento no sirve).
    """
    decision = FieldDecision(key=key)

    if not evidences:
        return decision

    groups: dict[object, Group] = {}

    for evidence in evidences:
        group = groups.setdefault(evidence.value, Group(value=evidence.value))
        group.evidences.append(evidence)

        if evidence.note:
            group.notes.append(evidence.note)

    ordered = list(groups.values())

    for group in ordered:
        group.score = _score(group)

        if expected_format and not re.fullmatch(expected_format, str(group.value)):
            group.score *= 0.5
            group.notes.append("formato_inesperado")

    def valid(group):
        return not expected_format or re.fullmatch(expected_format, str(group.value))

    # Lecturas parciales: no compiten con el valor completo. Un valor que
    # no cumple el formato esperado no cuenta como "completo".
    for group in ordered:
        for other in ordered:
            if other is not group and valid(other) and _is_partial(group.value, other.value):
                group.notes.append(f"parcial_de:{other.value}")
                other.score += 0.1
                group.score *= 0.3
                break

    ordered.sort(key=lambda group: group.score, reverse=True)
    decision.groups = ordered

    # Con formato obligatorio solo se proponen valores que lo cumplen;
    # los demás quedan como alternativas para el usuario.
    eligible = [group for group in ordered if valid(group)] if require_format else ordered
    best = eligible[0] if eligible else ordered[0]
    decision.chosen = best

    if expected_format and require_format and not eligible:
        decision.value = None
        decision.status = "incompleto"
        decision.confidence = "baja"
        decision.reason = (
            "Ninguna lectura coincide con el formato completo esperado; "
            "puede estar cortada. Escríbelo desde el recibo."
        )
        decision.chosen = None
        return decision

    competitors = [
        group
        for group in ordered[1:]
        if not any(note.startswith("parcial_de:") for note in group.notes)
        and group.score >= 0.5 * best.score
    ]

    decision.value = best.value

    if competitors:
        decision.status = "dudoso"
        decision.confidence = "baja"
        decision.reason = "Hay lecturas distintas con respaldo similar."
        return decision

    decision.status = "propuesto"
    clean = "formato_inesperado" not in best.notes and not any(
        note in NOTE_PENALTY for note in best.notes
    )

    if best.passes == 1:
        # Una sola lectura no tiene con qué contrastarse.
        decision.confidence = "baja"
        decision.reason = "Solo una lectura OCR respalda este valor."
    elif clean and (best.locations >= 2 or best.families >= 2):
        decision.confidence = "alta"
    elif clean or best.locations >= 2:
        decision.confidence = "media"
    else:
        decision.confidence = "baja"

    if "formato_inesperado" in best.notes:
        decision.reason = "El valor no coincide con el formato observado en otros recibos."

    if decision.confidence == "alta" and all(
        evidence.method == "plantilla" for evidence in best.evidences
    ):
        decision.confidence = "media"
        decision.reason = "Ubicado por su posición en el recibo; la etiqueta no se pudo leer."

    return decision
