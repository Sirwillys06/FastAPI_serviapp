"""Extractor del recibo de gas de Surtigas (incluye financiación Brilla)."""

from __future__ import annotations

import re

from ..schemas.recibo import ConceptoValor, Empresa
from ..utils.fechas import buscar_mes_anio
from ..utils.texto import limpiar_linea, normalizar
from .base import (
    ExtractorBase,
    direccion_valida,
    nombre_limpio,
    validar_entero,
    validar_fecha,
    validar_numero,
    validar_pesos,
)


def _validar_barrio(texto: str) -> str | None:
    texto = re.sub(r"(?i)^\s*barrio\W*", "", texto)
    barrio = re.sub(r"\(.*$", "", limpiar_linea(texto)).strip()
    barrio = re.sub(r"[^A-ZÑ ]", "", normalizar(barrio)).strip()
    barrio = re.sub(r"\s*CARTAGENA$", "", barrio).strip()  # "OLAYA HERRERA CENTRAL ( CARTAGENA )"
    return barrio if len(barrio) >= 4 else None


def _validar_estrato(texto: str) -> int | None:
    m = re.search(r"ESTRATO\s*([1-6])", normalizar(texto))
    return int(m.group(1)) if m else None


def _validar_factura(texto: str) -> str | None:
    m = re.search(r"\b(STG\s?\d{6,10})\b", normalizar(texto))
    return m.group(1).replace(" ", "") if m else None


class ExtractorSurtigas(ExtractorBase):
    empresa = Empresa.SURTIGAS

    ANCLAS = {
        "CONCEPTOS FACTURADOS": (0.281, 0.181),
        "OTROS CONCEPTOS": (0.319, 0.204),
        "MESES ANTERIORES": (0.458, 0.204),
        "CONSUMO DE GAS": (0.317, 0.476),
        "SIN CONSUMOS ANTERIORES": (0.299, 0.550),
        "CONSUMO TOTAL FACTURADO": (0.049, 0.572),
        "Tu Revision": (0.852, 0.394),
    }
    ZONAS = {
        "contrato": (0.72, 0.072, 0.94, 0.112),
        "titular": (0.025, 0.128, 0.42, 0.178),
        "vencimiento": (0.43, 0.148, 0.68, 0.172),
        "suspension": (0.69, 0.148, 0.95, 0.172),
        "total": (0.57, 0.215, 0.75, 0.248),
        "gas": (0.06, 0.255, 0.16, 0.275),
        "brilla": (0.19, 0.255, 0.29, 0.275),
        "otros": (0.32, 0.255, 0.42, 0.275),
        "meses_anteriores": (0.46, 0.255, 0.56, 0.275),
        "consumo_mes": (0.18, 0.500, 0.27, 0.520),
        "consumo_facturado": (0.18, 0.565, 0.27, 0.585),
        "consumo_m3": (0.74, 0.519, 0.81, 0.535),
        "consumo_facturado_m3": (0.74, 0.553, 0.81, 0.570),
        "lectura_actual": (0.66, 0.484, 0.73, 0.500),
        "fecha_actual": (0.73, 0.484, 0.81, 0.500),
        "lectura_anterior": (0.66, 0.500, 0.73, 0.517),
        "fecha_anterior": (0.73, 0.500, 0.81, 0.517),
        "dias": (0.74, 0.567, 0.81, 0.585),
        "direccion": (0.70, 0.618, 0.96, 0.634),
        "barrio": (0.70, 0.643, 0.96, 0.660),
        "estrato": (0.85, 0.688, 0.96, 0.704),
        "mes_facturado": (0.85, 0.719, 0.96, 0.734),
        "emision": (0.85, 0.731, 0.96, 0.747),
        "factura": (0.13, 0.741, 0.24, 0.756),
        "contrato_pie": (0.655, 0.962, 0.72, 0.980),
        "total_pie": (0.80, 0.962, 0.89, 0.980),
    }

    def extraer_campos(self) -> None:
        self._contrato()
        self.poner_total(["TOTAL A PAGAR"], max_dx=0.25, max_dy=0.05)
        self.completar_con_zona("valorTotal", "total", validar_pesos)
        self.completar_con_zona("valorTotal", "total_pie", validar_pesos)
        self.poner_fecha_limite_gs1()
        self._fechas()
        self._titular()
        self._predio()
        self._consumo()
        self._conceptos()

    # ---------------------------------------------------------------- campos
    def _contrato(self) -> None:
        votos: dict[str, float] = {}

        def votar(valor, conf):
            if valor:
                votos[str(valor)] = votos.get(str(valor), 0) + conf

        for etiqueta in ("No. de contrato", "contrato"):
            votar(*self.regex_cerca(etiqueta, r"(?<!\d)(\d{6,9})(?!\d)", max_dx=0.35, max_dy=0.04, umbral=0.8))
        numero = validar_entero(digitos=(6, 9))
        votar(*self.leer_zona("contrato", numero, lista_blanca="0123456789"))
        votar(*self.leer_zona("contrato_pie", numero, lista_blanca="0123456789"))
        if votos:
            mejor = max(votos, key=votos.get)
            self.poner("identificador", mejor, min(0.98, 0.4 + 0.25 * votos[mejor]))

    def _fechas(self) -> None:
        f, c = self.fecha_cerca(["FECHA DE VENCIMIENTO"], derecha=False, max_dy=0.04)
        self.poner("fechaLimitePago", f, c)
        f, c = self.fecha_cerca(["SUSPENSION A PARTIR DE"], derecha=False, max_dy=0.04, umbral=0.75)
        self.poner("fechaSuspension", f, c)
        f, c = self.fecha_cerca(["Fecha de emision"], max_dx=0.45, debajo=False, umbral=0.75)
        self.poner("fechaEmision", f, c)
        self.completar_con_zona("fechaLimitePago", "vencimiento", validar_fecha)
        self.completar_con_zona("fechaSuspension", "suspension", validar_fecha)
        self.completar_con_zona("fechaEmision", "emision", validar_fecha)

        for texto, conf, _ in self.textos_cerca(["Mes facturado"], max_dx=0.45, debajo=False, umbral=0.8):
            mes = buscar_mes_anio(texto)
            if mes:
                self.poner("periodoFacturado", mes, 0.55 + 0.4 * conf)
                break
        self.completar_con_zona("periodoFacturado", "mes_facturado", buscar_mes_anio)
        if "periodoFacturado" not in self.datos and self.datos.get("fechaEmision"):
            e = self.datos["fechaEmision"]
            self.poner("periodoFacturado", (e.year, e.month), 0.5)

        for _, m in self.p.buscar_regex(r"\b(STG\s?\d{6,10})\b"):
            self.poner("numeroFactura", m.group(1).replace(" ", ""), 0.8)
        self.completar_con_zona("numeroFactura", "factura", _validar_factura)

    def _titular(self) -> None:
        """El nombre está en letras grandes (1 o 2 líneas) bajo el logo."""
        mejores = []
        for texto in self.p.leer_region(self.zona("titular"), psm=6):
            partes = [nombre_limpio(l) for l in texto.splitlines()]
            nombre = " ".join(p for p in partes if p and len(p) > 2)
            if nombre:
                mejores.append(nombre)
        if mejores:
            # La variante con más letras suele ser la más completa
            self.poner("titular", max(mejores, key=len), 0.7)

    def _predio(self) -> None:
        for texto, conf, _ in self.textos_cerca(["Dir. de Predio"], max_dx=0.45, debajo=False, umbral=0.75):
            direccion = direccion_valida(re.sub(r"(?i)dir\W*de\W*predio\W*", "", texto))
            if direccion:
                self.poner("direccion", direccion, 0.45 + 0.45 * conf)
                break
        self.completar_con_zona("direccion", "direccion", direccion_valida)
        self.completar_con_zona("barrio", "barrio", _validar_barrio)
        for _, m in self.p.buscar_regex(r"ESTRATO\s*([1-6])\b"):
            self.poner("estrato", int(m.group(1)), 0.85)
        self.completar_con_zona("estrato", "estrato", _validar_estrato)

    def _consumo(self) -> None:
        digitos = "0123456789.,"
        entero = validar_entero(0, 999999, digitos=(1, 6))
        # El consumo aparece en cuatro casillas: se vota entre todas
        votos: dict[float, int] = {}
        for zona in ("consumo_mes", "consumo_facturado", "consumo_m3", "consumo_facturado_m3"):
            valor, _ = self.leer_zona(zona, validar_numero, lista_blanca=digitos)
            if valor is not None:
                votos[valor] = votos.get(valor, 0) + 1
        if votos:
            consumo = max(votos, key=votos.get)
            self.poner("consumoActual", consumo, 0.4 + 0.15 * votos[consumo])
        for campo, zona in (("lecturaActual", "lectura_actual"), ("lecturaAnterior", "lectura_anterior")):
            valor, conf = self.leer_zona(zona, validar_numero, lista_blanca=digitos)
            self.poner(campo, valor, conf)
        dias, conf = self.leer_zona("dias", entero, lista_blanca="0123456789")
        if dias is not None and 1 <= dias <= 70:
            self.poner("diasFacturados", dias, conf)

        ant, act = self.datos.get("lecturaAnterior"), self.datos.get("lecturaActual")
        if ant is not None and act is not None and act >= ant and "consumoActual" not in self.datos:
            self.poner("consumoActual", act - ant, 0.6)

        self.completar_con_zona("periodoFin", "fecha_actual", validar_fecha)
        self.completar_con_zona("periodoInicio", "fecha_anterior", validar_fecha)

        # Surtigas no imprime gráfica histórica ("SIN CONSUMOS ANTERIORES"): solo el mes actual
        consumo = self.datos.get("consumoActual")
        if consumo is not None:
            self.poner_historial([consumo], self.datos.get("periodoFacturado"), confianza=self.confianza["consumoActual"])
        self.advertencias.append(
            "El recibo de gas no trae historial de consumos anteriores; solo se registra el mes actual."
        )

    def _conceptos(self) -> None:
        """Valores de la cabecera: Gas | Brilla | Otros conceptos | Meses anteriores."""
        nombres = {
            "gas": "Gas y servicios inherentes",
            "brilla": "Brilla",
            "otros": "Otros conceptos",
            "meses_anteriores": "Meses anteriores",
        }
        conceptos = []
        for zona, nombre in nombres.items():
            valor, _ = self.leer_zona(zona, validar_pesos, lista_blanca="$0123456789.,")
            if valor is None:
                return
            conceptos.append(ConceptoValor(concepto=nombre, valor=valor))
        total = self.datos.get("valorTotal")
        if total is None or sum(c.valor for c in conceptos) == total:
            self.conceptos = conceptos
            if total is not None:
                self.confianza["valorTotal"] = max(self.confianza.get("valorTotal", 0), 0.97)
