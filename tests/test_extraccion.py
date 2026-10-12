"""
Pruebas de las piezas deterministas de la extracción.

No usan Tesseract ni fotos reales: construyen palabras con coordenadas
a mano. La medición sobre recibos reales está en tools/evaluar.py.
"""

from app.candidates import Evidence, decide
from app.charts import _with_gaps
from app.layout import Box, Word, build_lines, find_label, words_right_of
from app.values import clean_address, find_amounts, find_dates, find_months, parse_amount


def word(text, x, y, w=None, h=20, conf=90.0, source="p"):
    return Word(text=text, conf=conf, box=Box(x, y, w or 12 * len(text), h), source=source)


def evidence(value, source, x=100, y=100, note=None, method="derecha"):
    return Evidence(value=value, raw=str(value), method=method, source=source,
                    confidence=90.0, box=Box(x, y, 80, 20), note=note)


# --- valores ---------------------------------------------------------------

def test_importes_colombianos():
    assert parse_amount("$427.630") == 427630
    assert parse_amount("344,546") == 344546
    assert parse_amount("183.950,00") == 183950
    assert parse_amount("344,5468") is None          # no se adivina
    assert find_amounts("Total 0", minimum=1000) == []


def test_fechas_y_notas():
    assert find_dates("2026-08-15 11:13:40") == [("2026-08-15", None)]
    assert find_dates("30/08/2026") == [("2026-08-30", None)]
    assert find_dates("31/AG0/2026") == [("2026-08-31", "mes_con_cero_por_o")]
    assert find_dates("18/092026") == [("2026-09-18", "separador_ausente")]
    assert find_dates("32/13/2026") == []


def test_meses():
    assert find_months("AGO2026") == [("2026-08", None)]
    assert find_months("2026/08") == [("2026-08", None)]


def test_direcciones_canonicas():
    assert clean_address("CL31A 39 - 15") == ("CL 31A 39-15", None)
    assert clean_address("CL 33 KR 63 - 28 APTO 101 I") == ("CL 33 KR 63-28 APTO 101", None)
    assert clean_address("C32 39 13 ALCIBIA") == ("C32 39 13 ALCIBIA", None)
    assert clean_address("CLI1A 39-15")[1] == "nomenclatura_no_reconocida"


# --- candidatos --------------------------------------------------------------

def test_lectura_parcial_no_anula_al_valor_completo():
    decision = decide("identificador.valor", [
        evidence("527005", "a", y=100),
        evidence("527005", "b", y=100),
        evidence("527005", "c", y=900),
        evidence("27005", "d", y=900),
    ])
    assert decision.value == "527005"
    assert decision.status == "propuesto"
    assert any("parcial_de:527005" in alt.get("notas", []) for alt in decision.alternatives())


def test_formato_obligatorio_elige_solo_valores_validos():
    decision = decide("numero_factura", [
        evidence("00012026A1023543865", "a"),
        evidence("00012026A1023543865", "b"),
        evidence("00012026A102354386", "c", y=900),
    ], expected_format=r"\d{8}[A-Z]\d{9}", require_format=True)
    assert decision.value == "00012026A102354386"


def test_fragmento_no_se_presenta_como_completo():
    decision = decide("numero_factura", [evidence("00012026A", "a")],
                      expected_format=r"\d{8}[A-Z]\d{9}", require_format=True)
    assert decision.value is None
    assert decision.status == "incompleto"
    assert decision.alternatives()[0]["valor"] == "00012026A"


def test_una_sola_lectura_tiene_confianza_baja():
    decision = decide("consumo.actual", [evidence(1, "a")])
    assert decision.value == 1
    assert decision.confidence == "baja"


def test_sin_evidencia_no_se_inventa():
    decision = decide("consumo.actual", [])
    assert decision.value is None
    assert decision.status == "no_encontrado"


# --- geometría ---------------------------------------------------------------

def test_etiqueta_en_dos_renglones_y_palabras_unidas():
    words = [word("TOTALA", 100, 100, w=120), word("PAGAR", 110, 125, w=100)]
    matches = find_label(build_lines(words), r"\bTOTAL A PAGAR\b")
    assert matches and matches[0].box.bottom == 145


def test_valor_a_la_derecha_toma_la_fila_mas_cercana():
    words = [
        word("Predio:", 100, 100), word("CL", 400, 100), word("33", 440, 100),
        word("Entrega:", 100, 128), word("CL", 400, 128), word("34", 440, 128),
    ]
    lines = build_lines(words)
    label = find_label(lines, r"\bPREDIO\b")[0]
    right = words_right_of(label, words, max_dx=600, band=1.0)
    assert [w.text for w in right] == ["CL", "33"]


def test_grafica_deja_casilla_vacia_para_barra_no_leida():
    def cluster(x, value):
        return [(word(str(value), x, 500), value)]

    # Barras cada ~106 px; falta la segunda (x≈250).
    clusters = [cluster(143, 453), cluster(349, 488), cluster(462, 531), cluster(567, 466)]
    slots = _with_gaps(clusters, spacing=106)
    assert [None if slot is None else slot[0][1] for slot in slots] == [453, None, 488, 531, 466]
