"""
Conversión de texto OCR a valores tipados.

Cada función devuelve una lista de (valor, nota). La nota es None cuando
el texto coincide exactamente con un formato conocido; si hubo que
reinterpretar algo (por ejemplo, un separador ausente), la nota lo dice
para que el candidato reciba menos peso y quede visible en la revisión.

Nunca se sustituyen letras por dígitos ni se completan cifras ausentes.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.layout import normalize_text


MONTHS = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}

# Confusión O/0 dentro del nombre del mes ("AG0" en lugar de "AGO").
# Solo se acepta dentro de un mes con letras; nunca en cifras.
MONTH_ALIASES = {
    name.replace("O", "0"): number
    for name, number in MONTHS.items()
    if "O" in name
}

MIN_YEAR = 2000
MAX_YEAR = 2099


def _valid_date(year: int, month: int, day: int):
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None

    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _month_number(name: str):
    if name in MONTHS:
        return MONTHS[name], None

    if name in MONTH_ALIASES:
        return MONTH_ALIASES[name], "mes_con_cero_por_o"

    return None, None


def find_dates(text: str) -> list[tuple[str, str | None]]:
    text = normalize_text(text)
    results = []

    def add(value, note):
        if value and value not in [item[0] for item in results]:
            results.append((value, note))

    # 2026-08-15 | 2026/08/15
    for match in re.finditer(r"(?<!\d)(\d{4})[-/.](\d{2})[-/.](\d{2})(?!\d)", text):
        year, month, day = map(int, match.groups())
        add(_valid_date(year, month, day), None)

    # 30/08/2026 | 22-08-2026
    for match in re.finditer(r"(?<!\d)(\d{2})[-/.](\d{2})[-/.](\d{4})(?!\d)", text):
        day, month, year = map(int, match.groups())
        add(_valid_date(year, month, day), None)

    # 16/AGO/2026 | 31/AG0/2026
    for match in re.finditer(
        r"(?<!\d)(\d{1,2})[-/. ]?([A-Z0]{3,4})[-/. ]?(\d{4})(?!\d)", text
    ):
        month, note = _month_number(match.group(2))

        if month:
            add(_valid_date(int(match.group(3)), month, int(match.group(1))), note)

    # 18/092026: falta un separador, pero están todas las cifras.
    for match in re.finditer(r"(?<!\d)(\d{2})[-/](\d{2})(\d{4})(?!\d)", text):
        day, month, year = map(int, match.groups())
        add(_valid_date(year, month, day), "separador_ausente")

    for match in re.finditer(r"(?<!\d)(\d{2})(\d{2})[-/](\d{4})(?!\d)", text):
        day, month, year = map(int, match.groups())
        add(_valid_date(year, month, day), "separador_ausente")

    return results


def find_months(text: str) -> list[tuple[str, str | None]]:
    """Mes facturado como 'AAAA-MM'. Ejemplos: AGO2026, 2026/08."""
    text = normalize_text(text)
    results = []

    def add(year, month, note):
        if MIN_YEAR <= year <= MAX_YEAR and 1 <= month <= 12:
            value = f"{year:04d}-{month:02d}"

            if value not in [item[0] for item in results]:
                results.append((value, note))

    for match in re.finditer(r"(?<![\d/-])(\d{4})[/-](\d{2})(?![\d/-])", text):
        add(int(match.group(1)), int(match.group(2)), None)

    for match in re.finditer(r"(?<![A-Z])([A-Z0]{3,4})[ /.-]?(\d{4})(?!\d)", text):
        month, note = _month_number(match.group(1))

        if month:
            add(int(match.group(2)), month, note)

    return results


def parse_amount(token: str):
    """
    Importe en pesos. Ejemplos: 427630 | $427.630 | 344,546 | 183.950,00.
    Devuelve None si el formato no es inequívoco.
    """
    value = token.replace("$", "").replace(" ", "").strip(".,:;")

    if re.fullmatch(r"\d+", value):
        normalized = value
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", value):
        normalized = value.replace(".", "").replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{2}", value):
        normalized = value.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})*\.\d{2}", value):
        normalized = value.replace(",", "")
    else:
        return None

    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None

    return int(amount) if amount == amount.to_integral_value() else float(amount)


def find_amounts(text: str, minimum: int = 0) -> list[tuple[int | float, str | None]]:
    results = []

    for token in re.findall(r"\$?\s?\d[\d.,]*", text):
        amount = parse_amount(token)

        if amount is None or amount < minimum:
            continue

        has_separator = bool(re.search(r"\d[.,]\d{3}", token))
        note = None if has_separator or "$" in token else "sin_separador_de_miles"

        if amount not in [item[0] for item in results]:
            results.append((amount, note))

    return results


def find_decimal(text: str) -> list[tuple[float, str | None]]:
    """Números con decimales como 15,61 (coma o punto decimal)."""
    results = []

    for match in re.finditer(r"(?<![\d.,])(\d{1,4})[.,](\d{1,2})(?![\d.,])", text):
        value = float(f"{match.group(1)}.{match.group(2)}")

        if value not in [item[0] for item in results]:
            results.append((value, None))

    return results


def find_integers(text: str, min_digits: int = 1, max_digits: int = 6) -> list[tuple[int, str | None]]:
    """Enteros aislados (consumos, lecturas). No interpreta letras como cifras."""
    results = []

    for match in re.finditer(
        rf"(?<![\w.,/-])(\d{{{min_digits},{max_digits}}})(?![\w.,/-])",
        normalize_text(text),
    ):
        value = int(match.group(1))

        if value not in [item[0] for item in results]:
            results.append((value, None))

    return results


def find_identifiers(text: str, min_length: int, max_length: int) -> list[tuple[str, str | None]]:
    """Identificadores numéricos (NIC, contrato, póliza)."""
    results = []

    for match in re.finditer(
        rf"(?<![\w])(\d{{{min_length},{max_length}}})(?![\w])",
        normalize_text(text),
    ):
        if match.group(1) not in [item[0] for item in results]:
            results.append((match.group(1), None))

    return results


def find_codes(text: str, min_length: int, max_length: int) -> list[tuple[str, str | None]]:
    """Códigos alfanuméricos con al menos un dígito (números de factura)."""
    results = []

    for match in re.finditer(
        rf"(?<![A-Z0-9])([A-Z0-9]{{{min_length},{max_length}}})(?![A-Z0-9])",
        normalize_text(text),
    ):
        code = match.group(1)

        if re.search(r"\d", code) and code not in [item[0] for item in results]:
            results.append((code, None))

    return results


STREET_PREFIX = re.compile(
    r"^(?:CL|CLL|CALLE|CR|CRA|KR|KRA|CARRERA|TV|TR|TRANSV|TRANSVERSAL|"
    r"DG|DIAGONAL|AV|AVENIDA|AK|AC|MZ|MANZANA|C)\s?\d"
)


def clean_address(text: str) -> tuple[str | None, str | None]:
    """Normaliza espacios y valida que empiece como una nomenclatura."""
    # Forma canónica para comparar lecturas: "63 - 28" = "63-28" y
    # "CL31A" = "CL 31A". Los prefijos de una letra ("C32") no se separan.
    value = normalize_text(text)
    value = re.sub(r"\s*-\s*", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" :|.,;-")
    value = re.sub(
        r"^(CL|CLL|CALLE|CR|CRA|KR|KRA|CARRERA|TV|TR|DG|AV|AK|AC|MZ)(?=\d)",
        r"\1 ",
        value,
    )

    # Bordes de casillas leídos como letras sueltas al final ("101 I").
    value = re.sub(r"(?:\s+[IL|!])+$", "", value)

    if len(value) < 5:
        return None, None

    if not STREET_PREFIX.match(value):
        return value, "nomenclatura_no_reconocida"

    return value, None
