from datetime import date

from app.extractores.base import direccion_valida, validar_pesos
from app.services.codigos import parsear_gs1_crudo, parsear_gs1_impreso
from app.services.detector import detectar_empresa
from app.schemas.recibo import Empresa
from app.utils.fechas import buscar_fechas, buscar_mes_anio, sumar_meses
from app.utils.numeros import parsear_decimal, parsear_pesos


def test_parsear_pesos_formatos_colombianos():
    assert parsear_pesos("$427.630") == 427630
    assert parsear_pesos("$ 427.630,00") == 427630
    assert parsear_pesos("$344,546") == 344546
    assert parsear_pesos("380.638") == 380638
    assert parsear_pesos("$1O.5OO") == 10500  # letras O confundidas con ceros


def test_validar_pesos_tolera_espacios_del_ocr():
    assert validar_pesos("de 344,54 6") == 344546
    assert validar_pesos("$0") == 0


def test_parsear_decimal():
    assert parsear_decimal("15,61") == 15.61
    assert parsear_decimal("3.373,00") == 3373.0
    assert parsear_decimal(".3809") == 0.3809


def test_fechas_de_los_tres_recibos():
    assert buscar_fechas("Fecha emisión: 10/09/2026") == [date(2026, 9, 10)]
    assert buscar_fechas("2026-08-15 11:13:40") == [date(2026, 8, 15)]
    assert buscar_fechas("09-07-26") == [date(2026, 7, 9)]
    assert buscar_fechas("31/AGO/2026") == [date(2026, 8, 31)]
    assert buscar_fechas("31/AG0/2026") == [date(2026, 8, 31)]
    assert buscar_fechas("18/092026") == [date(2026, 9, 18)]
    assert buscar_fechas("10/08/2026 - 08/09/2026") == [date(2026, 8, 10), date(2026, 9, 8)]


def test_mes_anio():
    assert buscar_mes_anio("AGO2026") == (2026, 8)
    assert buscar_mes_anio("Periodo 2026/08") == (2026, 8)
    assert buscar_mes_anio("08/26") == (2026, 8)
    assert sumar_meses(2026, 2, -3) == (2025, 11)


def test_gs1_crudo_y_impreso():
    crudo = "4157709998169074802011462513380810\x1d39000000427630\x1d9620260917"
    datos = parsear_gs1_crudo(crudo)
    assert datos.gln == "7709998169074"
    assert datos.valor == 427630
    assert datos.fecha_limite == date(2026, 9, 17)

    impreso = parsear_gs1_impreso("(415)7709998014633(8020)000000027332225(3900)0000380638")
    assert impreso.valor == 380638
    assert impreso.fecha_limite is None

    # Errores típicos del OCR: "1415)" y "2900)"
    ruidoso = parsear_gs1_impreso("1415)7709998169074|8020)11462513380810/2900)0000427630/96)20260917")
    assert ruidoso.valor == 427630
    assert ruidoso.fecha_limite == date(2026, 9, 17)


def test_detector_por_palabras_y_por_gln():
    assert detectar_empresa("AFINIA Grupo EPM NIC 1146251 Caribemar")[0] == Empresa.AFINIA
    assert detectar_empresa("AGUAS DE CARTAGENA ACUEDUCTO ALCANTARILLADO")[0] == Empresa.AGUAS_DE_CARTAGENA
    assert detectar_empresa("Surtigas una empresa Promigas BRILLA")[0] == Empresa.SURTIGAS
    assert detectar_empresa("texto sin relación")[0] is None
    gs1 = parsear_gs1_impreso("(415)7709998002777(8020)0411699364(3900)0000344546")
    assert detectar_empresa("", gs1)[0] == Empresa.SURTIGAS


def test_direccion_valida_limpia_basura():
    assert direccion_valida("CL 31A 39-15 ID A") == "CL 31A 39-15"
    assert direccion_valida("CL 33 KR 63 - 28 APTO 101") == "CL 33 KR 63 - 28 APTO 101"
    assert direccion_valida("CARTAGENA") is None
