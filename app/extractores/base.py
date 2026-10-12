"""Clase base de los extractores por empresa.

Cada extractor combina varias fuentes para cada campo y se queda con la de
mayor confianza:

- Texto cercano a una etiqueta ("Total a pagar", "Fecha emisión", ...).
- OCR focalizado en una región concreta con lista blanca de caracteres.
- Código de barras GS1-128 (total y fecha límite).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable, Iterable

import numpy as np

from ..schemas.recibo import (
    EMPRESA_A_IDENTIFICADOR,
    EMPRESA_A_SERVICIO,
    EMPRESA_A_UNIDAD,
    ConceptoValor,
    ConsumoMensual,
    Empresa,
    Identificador,
    ReciboExtraido,
)
from ..services.codigos import DatosGs1, localizar_barras, parsear_gs1_impreso
from ..services.ocr import Linea, PaginaOcr
from ..utils.fechas import buscar_fechas, periodo_str, sumar_meses
from ..utils.numeros import VALOR_PESOS, corregir_digitos, parsear_pesos
from ..utils.texto import limpiar_linea, normalizar, similitud

Zona = tuple[float, float, float, float]

# Campos que se reportan como faltantes si no se encuentran
CAMPOS_PRINCIPALES = (
    "identificador",
    "valorTotal",
    "periodoFacturado",
    "fechaEmision",
    "fechaLimitePago",
    "direccion",
    "consumoActual",
)


class ExtractorBase:
    empresa: Empresa
    # Plantilla del recibo: posición (x0, y0) de textos fáciles de leer ("anclas") y
    # zonas fijas de cada campo, medidas sobre un recibo de referencia (0..1).
    ANCLAS: dict[str, tuple[float, float]] = {}
    ZONAS: dict[str, Zona] = {}

    def __init__(self, pagina: PaginaOcr, gs1: DatosGs1 | None = None, qrs: list[str] | None = None):
        self.p = pagina
        self.gs1 = gs1
        self.qrs = qrs or []
        self.datos: dict[str, Any] = {}
        self.confianza: dict[str, float] = {}
        self.advertencias: list[str] = []
        self.historial: list[ConsumoMensual] = []
        self.conceptos: list[ConceptoValor] = []
        self._mapa = ((1.0, 0.0), (1.0, 0.0))
        self.anclas_encontradas = 0

    # ------------------------------------------------------------ API pública
    def extraer(self) -> tuple[ReciboExtraido, dict[str, float], list[str], list[str]]:
        self._ajustar_plantilla()
        self._leer_gs1_impreso()
        self.extraer_campos()
        self._validar()
        recibo = self._construir()
        faltantes = [c for c in CAMPOS_PRINCIPALES if not self._tiene(c, recibo)]
        if not recibo.historialConsumos:
            faltantes.append("historialConsumos")
        return recibo, self.confianza, faltantes, self.advertencias

    def extraer_campos(self) -> None:  # pragma: no cover - lo implementa cada empresa
        raise NotImplementedError

    # ------------------------------------------------------- registro de campos
    def poner(self, campo: str, valor: Any, confianza: float) -> None:
        """Guarda el valor si es más confiable que el que ya había."""
        if valor is None or valor == "":
            return
        if campo not in self.datos or confianza > self.confianza.get(campo, 0):
            self.datos[campo] = valor
            self.confianza[campo] = round(max(0.0, min(1.0, confianza)), 2)

    # ------------------------------------------------------------- búsquedas
    def textos_cerca(
        self,
        etiquetas: str | Iterable[str],
        *,
        derecha: bool = True,
        debajo: bool = True,
        max_dx: float = 0.5,
        max_dy: float = 0.04,
        zona: Zona | None = None,
        umbral: float = 0.8,
    ) -> list[tuple[str, float, Linea]]:
        """Textos candidatos alrededor de una etiqueta: la misma línea, a la derecha y debajo."""
        if isinstance(etiquetas, str):
            etiquetas = [etiquetas]
        candidatos: list[tuple[str, float, Linea]] = []
        for etiqueta in etiquetas:
            for ref in self.p.buscar(etiqueta, umbral=umbral, zona=zona):
                candidatos.append((ref.texto, ref.conf, ref))
                if derecha:
                    candidatos += [(l.texto, l.conf, l) for l in self.p.a_la_derecha(ref, max_dx=max_dx)]
                if debajo:
                    candidatos += [(l.texto, l.conf * 0.95, l) for l in self.p.debajo(ref, max_dy=max_dy)]
        return candidatos

    def fecha_cerca(self, etiquetas, **kwargs) -> tuple[date | None, float]:
        for texto, conf, _ in self.textos_cerca(etiquetas, **kwargs):
            fechas = buscar_fechas(texto)
            if fechas:
                return fechas[0], 0.55 + 0.4 * conf
        return None, 0.0

    def pesos_cerca(self, etiquetas, minimo: int = 100, **kwargs) -> tuple[int | None, float]:
        for texto, conf, _ in self.textos_cerca(etiquetas, **kwargs):
            for m in VALOR_PESOS.finditer(corregir_digitos_en_numeros(texto)):
                valor = parsear_pesos(m.group())
                if valor is not None and valor >= minimo:
                    return valor, 0.5 + 0.4 * conf
        return None, 0.0

    def regex_cerca(self, etiquetas, patron: str, grupo: int = 1, **kwargs) -> tuple[str | None, float]:
        regex = re.compile(patron)
        for texto, conf, _ in self.textos_cerca(etiquetas, **kwargs):
            m = regex.search(normalizar(texto))
            if m:
                return m.group(grupo), 0.5 + 0.4 * conf
        return None, 0.0

    def texto_debajo(self, etiquetas, max_dy: float = 0.03, **kwargs) -> tuple[str | None, float, Linea | None]:
        if isinstance(etiquetas, str):
            etiquetas = [etiquetas]
        for etiqueta in etiquetas:
            for ref in self.p.buscar(etiqueta, **kwargs):
                debajo = self.p.debajo(ref, max_dy=max_dy)
                if debajo:
                    linea = debajo[0]
                    return limpiar_linea(linea.texto), 0.45 + 0.45 * linea.conf, linea
        return None, 0.0, None

    def leer_numero_region(
        self, zona: Zona, patron: str = r"\d+", validar: Callable[[str], bool] | None = None, psm: int = 7
    ) -> tuple[str | None, float]:
        """OCR focalizado. Devuelve el valor que más se repite entre las variantes."""
        votos: dict[str, int] = {}
        textos = self.p.leer_region(zona, psm=psm, lista_blanca="0123456789.,$/-")
        for texto in textos:
            for m in re.finditer(patron, texto.replace(" ", "")):
                valor = m.group()
                if validar is None or validar(valor):
                    votos[valor] = votos.get(valor, 0) + 1
        if not votos:
            return None, 0.0
        valor = max(votos, key=votos.get)
        return valor, 0.6 + 0.3 * votos[valor] / max(1, len(textos))

    def releer_linea(self, linea: Linea, validar: Callable[[str], str | None]) -> tuple[str | None, float]:
        """Vuelve a leer una línea con OCR focalizado y se queda con la primera lectura válida."""
        margen_y = linea.alto * 0.35
        zona = (linea.x0 - 0.01, linea.y0 - margen_y, linea.x1 + 0.01, linea.y1 + margen_y)
        for texto in [linea.texto] + self.p.leer_region(zona, psm=7):
            valor = validar(texto)
            if valor:
                return valor, 0.45 + 0.45 * linea.conf
        return None, 0.0

    # ------------------------------------------------------------- plantilla
    def _ubicar_ancla(self, texto: str) -> tuple[float, float] | None:
        """Posición (x0, y0) de la primera palabra del ancla en la página."""
        lineas = self.p.buscar(texto, umbral=0.82)
        if len(lineas) != 1:
            return None  # ausente o ambigua
        primera = normalizar(texto).split()[0]
        for palabra in lineas[0].palabras:
            if similitud(palabra.texto, primera) >= 0.75:
                return palabra.x0, palabra.y0
        return lineas[0].x0, lineas[0].y0

    def _ajustar_plantilla(self) -> None:
        """Calcula la transformación plantilla → foto con las anclas encontradas.

        Corrige desplazamientos y escalas cuando la foto no quedó perfectamente
        recortada a la hoja (márgenes de mesa, hoja un poco más lejos, etc.).
        """
        pares = []
        for texto, (tx, ty) in self.ANCLAS.items():
            pos = self._ubicar_ancla(texto)
            if pos:
                pares.append((tx, ty, pos[0], pos[1]))
        self.anclas_encontradas = len(pares)
        if not pares:
            return
        self._mapa = (
            _ajuste_lineal([p[0] for p in pares], [p[2] for p in pares]),
            _ajuste_lineal([p[1] for p in pares], [p[3] for p in pares]),
        )

    def zona(self, nombre: str) -> Zona:
        x0, y0, x1, y1 = self.ZONAS[nombre]
        (a, b), (c, d) = self._mapa
        return (a * x0 + b, c * y0 + d, a * x1 + b, c * y1 + d)

    def leer_zona(
        self,
        nombre: str,
        validar: Callable[[str], Any],
        psm: int = 7,
        lista_blanca: str | None = None,
    ) -> tuple[Any, float]:
        """Lee un campo en su zona de la plantilla y vota entre las lecturas válidas."""
        zona = self.zona(nombre)
        textos = [" ".join(l.texto for l in self.p.en_zona(zona))]
        textos += self.p.leer_region(zona, psm=psm, lista_blanca=lista_blanca)
        votos: dict[Any, int] = {}
        for texto in textos:
            if not texto:
                continue
            valor = validar(texto)
            if valor is not None and valor != "":
                votos[valor] = votos.get(valor, 0) + 1
        if not votos:
            return None, 0.0
        valor = max(votos, key=votos.get)
        conf = 0.5 + 0.3 * votos[valor] / len(textos) + (0.1 if self.anclas_encontradas >= 2 else 0.0)
        return valor, conf

    def necesita(self, campo: str, umbral: float = 0.8) -> bool:
        return campo not in self.datos or self.confianza.get(campo, 0) < umbral

    def completar_con_zona(self, campo: str, zona: str, validar: Callable[[str], Any], **kwargs) -> None:
        """Si el campo falta o es dudoso, lo lee en la zona de la plantilla."""
        if self.necesita(campo) and zona in self.ZONAS:
            valor, conf = self.leer_zona(zona, validar, **kwargs)
            self.poner(campo, valor, conf)

    # ---------------------------------------------------------------- comunes
    def _leer_gs1_impreso(self) -> None:
        """Si el código no se pudo decodificar, lee el texto impreso bajo las barras."""
        if self.gs1 and self.gs1.fuente == "barras":
            return
        textos: list[str] = []
        caja = localizar_barras(self.p.gris)
        if caja:
            x0, y0, x1, y1 = caja
            alto = y1 - y0
            textos += self.p.leer_region((x0 - 0.03, y1 - 0.002, x1 + 0.03, y1 + max(0.022, alto * 0.45)),
                                         psm=7, lista_blanca="0123456789()")
            textos += self.p.leer_region((x0 - 0.03, y0 - max(0.022, alto * 0.45), x1 + 0.03, y0 + 0.002),
                                         psm=7, lista_blanca="0123456789()")
        textos += [l.texto for l in self.p.lineas if "8020" in l.texto.replace(" ", "") or "415)" in l.texto]
        for texto in textos:
            datos = parsear_gs1_impreso(texto)
            if datos and (self.gs1 is None or (datos.fecha_limite and not self.gs1.fecha_limite)):
                self.gs1 = datos
                if datos.fecha_limite:
                    break

    def poner_total(self, etiquetas: Iterable[str], **kwargs) -> None:
        valor_ocr, conf_ocr = self.pesos_cerca(etiquetas, minimo=1000, **kwargs)
        if self.gs1 and self.gs1.valor is not None:
            base = 0.97 if self.gs1.fuente == "barras" else 0.8
            if valor_ocr == self.gs1.valor:
                base = 0.99
            elif valor_ocr is not None and self.gs1.fuente == "ocr":
                self.advertencias.append(
                    f"El total leído ({valor_ocr:,}) no coincide con el del código de barras "
                    f"({self.gs1.valor:,}); verifica el valor."
                )
                base = 0.6
            self.poner("valorTotal", self.gs1.valor, base)
        self.poner("valorTotal", valor_ocr, conf_ocr)

    def poner_fecha_limite_gs1(self) -> None:
        if self.gs1 and self.gs1.fecha_limite:
            self.poner("fechaLimitePago", self.gs1.fecha_limite, 0.95 if self.gs1.fuente == "barras" else 0.85)

    def poner_historial(
        self,
        valores: list[float | None],
        periodo_actual: tuple[int, int] | None,
        meses: list[tuple[int, int]] | None = None,
        confianza: float = 0.7,
    ) -> None:
        """Construye el historial (del más antiguo al actual). El último valor es el mes facturado."""
        if not valores:
            return
        if meses is None or len(meses) != len(valores):
            if periodo_actual is None:
                self.advertencias.append("No se pudo determinar el mes facturado para fechar el historial.")
                return
            n = len(valores)
            meses = [sumar_meses(*periodo_actual, i - (n - 1)) for i in range(n)]
        self.historial = [
            ConsumoMensual(periodo=periodo_str(*mes), consumo=valor, esActual=(i == len(valores) - 1))
            for i, (mes, valor) in enumerate(zip(meses, valores))
        ]
        if any(v is None for v in valores):
            self.advertencias.append("Algunos meses de la gráfica no se pudieron leer; complétalos antes de guardar.")
        self.confianza["historialConsumos"] = round(confianza, 2)

    # --------------------------------------------------------------- internos
    def _validar(self) -> None:
        self._validar_fechas()
        self._validar_lecturas()
        if self.historial and self.datos.get("consumoActual") is not None:
            actual = self.historial[-1]
            if actual.consumo is None:
                actual.consumo = self.datos["consumoActual"]
            elif actual.consumo != self.datos["consumoActual"]:
                self.advertencias.append(
                    "El consumo del mes en la gráfica no coincide con el consumo facturado; revísalo."
                )
                self.confianza["consumoActual"] = min(self.confianza.get("consumoActual", 1), 0.5)
                self.confianza["historialConsumos"] = min(self.confianza.get("historialConsumos", 1), 0.5)

    def _fecha_referencia(self) -> date | None:
        """Fecha más confiable del recibo para verificar las demás."""
        candidatas = [
            (self.confianza.get(c, 0), self.datos.get(c))
            for c in ("fechaLimitePago", "fechaEmision", "fechaSuspension", "periodoFin")
            if isinstance(self.datos.get(c), date)
        ]
        candidatas = [c for c in candidatas if c[0] >= 0.8]
        if candidatas:
            return max(candidatas, key=lambda c: c[0])[1]
        periodo = self.datos.get("periodoFacturado")
        if isinstance(periodo, tuple) and self.confianza.get("periodoFacturado", 0) >= 0.7:
            return date(periodo[0], periodo[1], 15)
        return None

    def _validar_fechas(self) -> None:
        """Corrige o marca fechas incoherentes con el resto del recibo.

        Un error típico del OCR es leer mal un dígito del año (2026 → 2025/2028).
        Si al cambiar solo el año la fecha queda cerca de la referencia, se corrige.
        """
        ref = self._fecha_referencia()
        if ref is None:
            return
        for campo, ventana in (
            ("fechaEmision", 45), ("fechaLimitePago", 45), ("fechaSuspension", 60),
            ("periodoInicio", 80), ("periodoFin", 50),
        ):
            fecha = self.datos.get(campo)
            if not isinstance(fecha, date) or abs((fecha - ref).days) <= ventana:
                continue
            corregida = None
            for anio in (ref.year, ref.year - 1, ref.year + 1):
                try:
                    candidata = fecha.replace(year=anio)
                except ValueError:
                    continue
                if abs((candidata - ref).days) <= ventana:
                    corregida = candidata
                    break
            if corregida:
                self.datos[campo] = corregida
                self.confianza[campo] = min(self.confianza.get(campo, 1), 0.6)
            else:
                self.confianza[campo] = min(self.confianza.get(campo, 1), 0.35)

        emision = self.datos.get("fechaEmision")
        limite = self.datos.get("fechaLimitePago")
        suspension = self.datos.get("fechaSuspension")
        inicio, fin = self.datos.get("periodoInicio"), self.datos.get("periodoFin")
        if emision and limite and limite < emision:
            self.advertencias.append("La fecha límite de pago es anterior a la fecha de emisión; revísalas.")
            self.confianza["fechaLimitePago"] = min(self.confianza.get("fechaLimitePago", 1), 0.4)
        if limite and suspension and suspension < limite:
            self.confianza["fechaSuspension"] = min(self.confianza.get("fechaSuspension", 1), 0.4)
        if inicio and fin:
            dias = (fin - inicio).days
            if not 15 <= dias <= 70:
                for campo in ("periodoInicio", "periodoFin"):
                    self.confianza[campo] = min(self.confianza.get(campo, 1), 0.4)
                self.datos.pop("diasFacturados", None) if self.datos.get("diasFacturados") == dias else None
            elif self.datos.get("diasFacturados") is None:
                self.poner("diasFacturados", dias, min(self.confianza["periodoInicio"], self.confianza["periodoFin"]))

    def _validar_lecturas(self) -> None:
        """Lectura actual - anterior debe ser igual al consumo (medidores sin factor de corrección)."""
        ant, act = self.datos.get("lecturaAnterior"), self.datos.get("lecturaActual")
        if ant is None or act is None:
            return
        consumo = self.datos.get("consumoActual")
        if act < ant:
            for campo in ("lecturaAnterior", "lecturaActual"):
                self.confianza[campo] = min(self.confianza.get(campo, 1), 0.4)
            self.advertencias.append("La lectura actual es menor que la anterior; revisa las lecturas del medidor.")
        if consumo is not None and (act < ant or abs((act - ant) - consumo) > 0.5):
            # Lecturas y consumo no cuadran: alguno de los tres está mal leído
            for campo in ("lecturaAnterior", "lecturaActual", "consumoActual"):
                self.confianza[campo] = min(self.confianza.get(campo, 1), 0.5)

    def _construir(self) -> ReciboExtraido:
        d = self.datos
        periodo = d.get("periodoFacturado")
        if isinstance(periodo, tuple):
            periodo = periodo_str(*periodo)
        consumo_actual = d.get("consumoActual")
        if consumo_actual is None and self.historial:
            consumo_actual = self.historial[-1].consumo
            if consumo_actual is not None:
                self.confianza["consumoActual"] = self.confianza.get("historialConsumos", 0.6)
        return ReciboExtraido(
            tipoServicio=EMPRESA_A_SERVICIO[self.empresa],
            empresa=self.empresa,
            identificador=Identificador(tipo=EMPRESA_A_IDENTIFICADOR[self.empresa], valor=d.get("identificador")),
            numeroFactura=d.get("numeroFactura"),
            titular=d.get("titular"),
            direccion=d.get("direccion"),
            barrio=d.get("barrio"),
            estrato=d.get("estrato"),
            periodoFacturado=periodo,
            periodoInicio=d.get("periodoInicio"),
            periodoFin=d.get("periodoFin"),
            fechaEmision=d.get("fechaEmision"),
            fechaLimitePago=d.get("fechaLimitePago"),
            fechaSuspension=d.get("fechaSuspension"),
            valorTotal=d.get("valorTotal"),
            unidadConsumo=EMPRESA_A_UNIDAD[self.empresa],
            consumoActual=consumo_actual,
            lecturaAnterior=d.get("lecturaAnterior"),
            lecturaActual=d.get("lecturaActual"),
            diasFacturados=d.get("diasFacturados"),
            historialConsumos=self.historial,
            conceptos=self.conceptos,
        )

    @staticmethod
    def _tiene(campo: str, recibo: ReciboExtraido) -> bool:
        if campo == "identificador":
            return bool(recibo.identificador.valor)
        return getattr(recibo, campo, None) is not None


def _ajuste_lineal(plantilla: list[float], observado: list[float]) -> tuple[float, float]:
    """Ajusta observado = a * plantilla + b, descartando anclas incoherentes."""
    t = np.array(plantilla)
    o = np.array(observado)
    for _ in range(2):
        if len(t) >= 2 and t.max() - t.min() > 0.15:
            a, b = np.polyfit(t, o, 1)
            if not 0.75 <= a <= 1.33:
                a, b = 1.0, float(np.median(o - t))
        else:
            a, b = 1.0, float(np.median(o - t))
        residuos = np.abs(a * t + b - o)
        if len(t) <= 2 or residuos.max() < 0.02:
            break
        peor = int(residuos.argmax())
        t, o = np.delete(t, peor), np.delete(o, peor)
    if abs(b) > 0.25:
        return 1.0, 0.0  # transformación absurda: se ignora
    return float(a), float(b)


# --------------------------------------------------------------------- validadores
def validar_fecha(texto: str) -> date | None:
    fechas = buscar_fechas(texto)
    return fechas[0] if fechas else None


def validar_pesos(texto: str) -> int | None:
    """Primer valor en pesos del texto. Tolera espacios metidos por el OCR ("344,54 6")."""
    for variante in (texto, re.sub(r"(?<=[\d.,])\s+(?=[\d.,])", "", texto)):
        for m in VALOR_PESOS.finditer(corregir_digitos_en_numeros(variante)):
            valor = parsear_pesos(m.group())
            if valor is not None:
                return valor
    m = re.search(r"\$\s?(\d+)", texto)
    return int(m.group(1)) if m else None


def validar_entero(minimo: int = 0, maximo: int = 10**9, digitos: tuple[int, int] = (1, 12)):
    def validar(texto: str) -> int | None:
        for m in re.finditer(r"\d+", corregir_digitos_en_numeros(texto).replace(" ", "")):
            if digitos[0] <= len(m.group()) <= digitos[1] and minimo <= int(m.group()) <= maximo:
                return int(m.group())
        return None

    return validar


def validar_numero(texto: str) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", corregir_digitos_en_numeros(texto))
    if not m:
        return None
    valor = m.group()
    if re.fullmatch(r"\d{1,3}[.,]\d{3}", valor):  # separador de miles
        valor = valor.replace(".", "").replace(",", "")
    return float(valor.replace(",", "."))


def corregir_digitos_en_numeros(texto: str) -> str:
    """Corrige letras confundidas solo dentro de secuencias que parecen números ($1O.5OO)."""
    return re.sub(r"[\d$][\dOoIlSB.,\s]*\d", lambda m: corregir_digitos(m.group()), texto)


def nombre_limpio(texto: str | None) -> str | None:
    """Limpia un nombre propio: solo letras, puntos y espacios, en mayúsculas."""
    if not texto:
        return None
    texto = re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ. ]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip().upper()
    return texto if len(texto) >= 5 else None


PATRON_DIRECCION = re.compile(
    r"^(CL|CLL|CALLE|CR|CRA|KR|KRA|CARRERA|TV|TR|TRANSV\w*|DG|DIAG\w*|AV|AVENIDA|MZ|MANZANA|C)\s?\d", re.I
)


def direccion_valida(texto: str | None) -> str | None:
    """Dirección limpia solo si empieza como una nomenclatura colombiana (CL 31A 39-15)."""
    direccion = direccion_limpia(texto)
    if not direccion or not PATRON_DIRECCION.match(direccion):
        return None
    # Quita basura final del OCR ("CL 31A 39-15 ID A" -> "CL 31A 39-15")
    return re.sub(r"(\s+[A-Z]{1,2})+$", "", direccion)


def direccion_limpia(texto: str | None) -> str | None:
    if not texto:
        return None
    texto = limpiar_linea(texto)
    texto = re.sub(r"[^A-Za-z0-9ÁÉÍÓÚÑáéíóúñ#.\- ]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip().upper()
    return texto if len(texto) >= 5 and re.search(r"\d", texto) else None
