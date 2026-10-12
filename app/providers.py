"""
Reglas por empresa: qué etiquetas buscar y dónde está su valor.

Las distancias (`reach`, `x_pad`) son fracciones del ancho de la página
preparada, para que no dependan de la resolución de la foto.

Los patrones de etiqueta se aplican sobre texto normalizado (mayúsculas,
sin tildes, solo letras/dígitos/espacios). Los formatos esperados salen
de los recibos de prueba: hay que revisarlos cuando lleguen más fotos.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LabelRule:
    pattern: str
    direction: str          # "derecha" | "debajo" | "caja"
    reach: float = 0.0      # distancia máxima al valor (fracción del ancho)
    x_pad: float = 0.03     # solo "debajo": ensanchamiento horizontal
    band: float = 0.6       # solo "derecha": tolerancia vertical (alturas)
    reread: bool = True     # releer la zona con un recorte enfocado
    # Solo "caja": (x0, y0, x1, y1) en fracciones del ancho; x desde el
    # borde izquierdo de la etiqueta, y desde su centro. Para valores cuya
    # etiqueta casi nunca se lee y se ubican respecto a otra etiqueta fiable.
    box: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class FieldSpec:
    key: str
    kind: str               # identificador | codigo | fecha | mes | periodo
                            # importe | direccion | entero | decimal
    labels: tuple[LabelRule, ...]
    expected_format: str | None = None
    require_format: bool = False
    min_length: int = 1
    max_length: int = 30
    minimum: int = 0        # importes: mínimo admitido


@dataclass(frozen=True)
class ChartSpec:
    title: str              # etiqueta que marca el borde superior
    bottom: str | None      # etiqueta que marca el borde inferior
    layout: str             # "fila": valores en una fila bajo las barras
                            # "sobre_barras": valores encima, meses debajo
    x_range: tuple[float, float]   # fracciones del ancho
    x_relative: bool        # True: relativo al x de la etiqueta del título
    max_height: float       # alto máximo si no aparece la etiqueta inferior
    current_anchor: str | None = None
    expected_points: int | None = None
    max_digits: int = 4


@dataclass(frozen=True)
class Provider:
    name: str
    service: str
    identifier_type: str
    unit: str
    keywords: tuple[str, ...]
    fields: tuple[FieldSpec, ...]
    chart: ChartSpec | None = None
    no_history_phrase: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


R = LabelRule
F = FieldSpec


AFINIA = Provider(
    name="Afinia",
    service="energia",
    identifier_type="NIC",
    unit="kWh",
    keywords=(r"\bAFINIA\b", r"\bCARIBEMAR\b", r"\bKWH\b"),
    fields=(
        F("identificador", "identificador", (
            R(r"\bNIC\b", "derecha", 0.30),
        ), expected_format=r"\d{7}", min_length=5, max_length=10),
        F("numero_factura", "codigo", (
            R(r"\bN FACTURA\b", "derecha", 0.25),
            R(r"\bDOCUMENTO EQUIVALENTE NO\b", "derecha", 0.25),
        ), expected_format=r"\d{14}", require_format=True, min_length=6),
        F("direccion_suministro", "direccion", (
            R(r"\bDIRECCION DE SUMINISTRO\b", "debajo", 0.03, x_pad=0.02),
        )),
        F("adicional.direccion_envio", "direccion", (
            R(r"\bDIRECCION DE ENVIO\b", "debajo", 0.03, x_pad=0.02, reread=False),
        )),
        F("valores.total_facturado", "importe", (
            R(r"\bTOTAL A PAGAR\b", "derecha", 0.30),
            R(r"\bTOTAL MES\b", "derecha", 0.30),
            R(r"\bTOTAL MES\b", "debajo", 0.05, x_pad=0.03),
            R(r"\bTOTAL DE DOCUMENTOS\b", "debajo", 0.06, x_pad=0.03),
            # "Total a Pagar"/"Total Mes" (texto azul claro) casi nunca se
            # leen; el valor está encima de "Suspensión a partir de".
            R(r"\bSUSPENSION A PARTIR DE\b", "caja", box=(0.15, -0.078, 0.40, -0.040)),
        ), minimum=1000),
        F("fechas.emision", "fecha", (
            R(r"\bFECHA EMISION\b", "derecha", 0.30),
        )),
        F("fechas.pago_oportuno", "fecha", (
            R(r"\bPAGO OPORTUNO\b", "derecha", 0.30, band=1.2),
            # La etiqueta casi nunca se lee: fecha justo encima de la de suspensión.
            R(r"\bSUSPENSION A PARTIR DE\b", "caja", box=(0.15, -0.040, 0.40, -0.012)),
        )),
        F("fechas.suspension", "fecha", (
            R(r"\bSUSPENSION A PARTIR DE\b", "derecha", 0.30),
        )),
        F("periodo", "periodo", (
            R(r"\bPERIODO FACTURADO\b", "derecha", 0.50),
        )),
        F("adicional.promedio_consumo_diario", "decimal", (
            R(r"\bPROMEDIO CONSUMO DIARIO\b", "derecha", 0.20, reread=False),
        )),
    ),
    chart=ChartSpec(
        title=r"\bCONSUMO DE LOS ULTIMOS\b",
        bottom=r"\bPROMEDIO CONSUMO\b",
        layout="fila",
        x_range=(-0.03, 0.37),
        x_relative=True,
        max_height=0.13,
        current_anchor=r"\bPERIODO\b",
        expected_points=7,
        max_digits=5,
    ),
)


SURTIGAS = Provider(
    name="Surtigas",
    service="gas",
    identifier_type="CONTRATO",
    unit="m3",
    keywords=(r"\bSURTIGAS\b", r"\bSURTIDORA DE GAS\b", r"\bPROMIGAS\b", r"\bBRILLA\b"),
    fields=(
        F("identificador", "identificador", (
            R(r"\bCONTRATO\b", "derecha", 0.25, band=1.2),
            R(r"\bDE CONTRATO\b", "debajo", 0.025, x_pad=0.02, reread=False),
        ), expected_format=r"\d{7}", min_length=5, max_length=10),
        F("numero_factura", "codigo", (
            R(r"\bDE VENTA N\w*", "derecha", 0.15),
        ), expected_format=r"[A-Z]{3}\d{8}", require_format=True, min_length=6),
        F("direccion_suministro", "direccion", (
            R(r"\bDIR DE PREDIO\b", "derecha", 0.45),
        )),
        F("adicional.direccion_entrega", "direccion", (
            R(r"\bDIR DE ENTREGA\b", "derecha", 0.45, reread=False),
        )),
        F("valores.total_facturado", "importe", (
            R(r"\bTOTAL A PAGAR\b", "debajo", 0.04, x_pad=0.04),
            R(r"\bVALOR VENCIDO A PAGAR\b", "debajo", 0.025, x_pad=0.02),
        ), minimum=1000),
        F("fechas.emision", "fecha", (
            R(r"\bFECHA DE EMISION\b", "derecha", 0.45),
        )),
        F("fechas.pago_oportuno", "fecha", (
            R(r"\bFECHA DE VENCIMIENTO\b", "debajo", 0.035, x_pad=0.06),
        )),
        F("fechas.suspension", "fecha", (
            R(r"\bSUSPENSION A PARTIR DE\b", "debajo", 0.035, x_pad=0.06),
        )),
        F("periodo.mes", "mes", (
            R(r"\bMES FACTURADO\b", "derecha", 0.45),
        )),
        F("consumo.actual", "entero", (
            R(r"\bCONSUMO MES\b", "derecha", 0.20),
            R(r"\bCONSUMO TOTAL FACTURADO\b", "derecha", 0.12),
            R(r"\bCONSUMO M3\b", "derecha", 0.35),
            R(r"\bCONSUMO FACTURADO M3\b", "derecha", 0.30),
        ), max_length=5),
        F("adicional.lectura_actual", "entero", (
            R(r"\bLECTURA FECHA ACTUAL\b", "derecha", 0.20, reread=False),
        ), max_length=7),
        F("adicional.lectura_anterior", "entero", (
            R(r"\bLECTURA FECHA ANTERIOR\b", "derecha", 0.20, reread=False),
        ), max_length=7),
        # Los componentes (gas / Brilla) no se extraen todavía: el valor
        # está debajo de un ícono y "Brilla" aparece también en la
        # publicidad inferior ("Cupo disponible $0"), lo que producía
        # valores falsos. Mejor ausente que incorrecto.
    ),
    no_history_phrase=r"\bSIN CONSUMOS ANTERIORES\b",
    notes=(
        "El total incluye gas y financiación Brilla; no es solo el costo del gas.",
    ),
)


AGUAS = Provider(
    name="Aguas de Cartagena",
    service="agua",
    identifier_type="POLIZA",
    unit="m3",
    keywords=(r"\bAGUAS DE CARTAGENA\b", r"\bACUEDUCTO\b", r"\bALCANTARILLADO\b", r"\bPACARIBE\b"),
    fields=(
        F("identificador", "identificador", (
            R(r"\bPOLIZA\b", "derecha", 0.15),
            R(r"\bPOLIZA\b", "debajo", 0.03, x_pad=0.02),
        ), expected_format=r"\d{6}", min_length=4, max_length=10),
        F("numero_factura", "codigo", (
            R(r"\bFACTURA DE VENTA\b", "derecha", 0.20),
            R(r"^FACTURA\b", "derecha", 0.20),
        ), expected_format=r"\d{8}[A-Z]\d{9}", require_format=True, min_length=6),
        F("direccion_suministro", "direccion", (
            R(r"\bDOMICILIO\b", "derecha", 0.40),
            # "Domicilio" está en una casilla sombreada que el OCR no lee;
            # el valor está debajo del de "Fecha de Emisión".
            R(r"\bFECHA DE EMISION\b", "caja", box=(0.06, 0.012, 0.50, 0.045)),
        )),
        F("valores.total_facturado", "importe", (
            R(r"\bTOTAL A PAGAR\b", "derecha", 0.30),
        ), minimum=1000),
        F("fechas.emision", "fecha", (
            R(r"\bFECHA DE EMISION\b", "derecha", 0.15),
        )),
        F("fechas.pago_oportuno", "fecha", (
            R(r"\bPAGO SIN RECARGO\b", "debajo", 0.04, x_pad=0.10),
        )),
        F("fechas.suspension", "fecha", (
            R(r"\bSUSPENSION A PARTIR DE\b", "derecha", 0.15),
        )),
        F("periodo.mes", "mes", (
            R(r"^PERIODO\b(?! FACTURADO)", "derecha", 0.15),
        )),
        F("consumo.actual", "entero", (
            R(r"\bCONSUMO ?MES\b", "debajo", 0.03, x_pad=0.02),
        ), max_length=5),
        F("adicional.lectura_anterior", "entero", (
            # Al inicio de la fila: así no coincide con "Fecha lectura anterior".
            R(r"^LECTURA ANTERIOR\b", "debajo", 0.03, x_pad=0.02),
        ), max_length=7),
        F("adicional.lectura_actual", "entero", (
            R(r"^LECTURA ACTUAL\b", "debajo", 0.03, x_pad=0.02),
        ), max_length=7),
    ),
    chart=ChartSpec(
        title=r"\bOBSERVACIONES\b",
        bottom=r"\bCOMPONENTES TARIFA\b|\bSUSPENSION A PARTIR\b",
        layout="sobre_barras",
        x_range=(0.73, 0.97),
        x_relative=False,
        max_height=0.10,
        max_digits=3,
    ),
    notes=(
        "El recibo incluye aseo (Pacaribe); el total no es solo agua.",
        "La gráfica inferior izquierda es de valores de aseo, no de consumo.",
    ),
)


PROVIDERS = (AFINIA, SURTIGAS, AGUAS)
