"""Extractor del recibo de energía de Afinia (Caribemar de la Costa)."""

from __future__ import annotations

import re

from ..schemas.recibo import Empresa
from ..utils.fechas import buscar_fechas
from ..utils.texto import normalizar
from .base import (
    ExtractorBase,
    direccion_limpia,
    direccion_valida,
    nombre_limpio,
    validar_entero,
    validar_fecha,
    validar_pesos,
)
from .graficas import leer_fila_numeros


def _validar_barrio(texto: str) -> str | None:
    barrio = re.sub(r"[^A-ZÑ ]", "", normalizar(texto)).strip()
    return barrio if len(barrio) >= 4 and barrio != "CARTAGENA" else None


def _validar_estrato(texto: str) -> int | None:
    m = re.search(r"ESTRATO\s*([1-6])", normalizar(texto))
    return int(m.group(1)) if m else None


def _validar_periodo(texto: str):
    fechas = buscar_fechas(texto)
    return tuple(sorted(fechas[:2])) if len(fechas) >= 2 else None


class ExtractorAfinia(ExtractorBase):
    empresa = Empresa.AFINIA

    ANCLAS = {
        "Total a Pagar": (0.655, 0.063),
        "Titular de Pago": (0.071, 0.111),
        "Usuario o suscriptor": (0.071, 0.148),
        "Fecha emision": (0.661, 0.163),
        "Periodo facturado": (0.075, 0.247),
        "Otras Entidades": (0.287, 0.265),
    }
    ZONAS = {
        "nic": (0.74, 0.025, 0.86, 0.045),
        "total": (0.84, 0.058, 0.94, 0.080),
        "fecha_pago": (0.85, 0.096, 0.94, 0.114),
        "suspension": (0.85, 0.118, 0.94, 0.135),
        "emision": (0.85, 0.156, 0.94, 0.178),
        "documento": (0.82, 0.175, 0.94, 0.189),
        "titular": (0.065, 0.123, 0.34, 0.140),
        "direccion": (0.345, 0.113, 0.60, 0.125),
        "barrio": (0.345, 0.124, 0.60, 0.134),
        "estrato": (0.065, 0.192, 0.30, 0.207),
        "periodo": (0.40, 0.240, 0.62, 0.256),
        "grafica": (0.03, 0.43, 0.43, 0.54),
    }

    def extraer_campos(self) -> None:
        self._nic()
        self.poner_total(["Total a Pagar", "Total Mes", "Total mes"], max_dx=0.3)
        self.completar_con_zona("valorTotal", "total", validar_pesos)
        self.poner_fecha_limite_gs1()
        self._fechas()
        self._titular_direccion()
        self._factura()
        self._historial()

    # --------------------------------------------------------------- campos
    def _nic(self) -> None:
        candidatos: dict[str, float] = {}

        def votar(valor: str | None, conf: float) -> None:
            if valor and 5 <= len(valor) <= 10:
                candidatos[valor] = candidatos.get(valor, 0) + conf

        valor, conf = self.regex_cerca(["NIC"], r"(\d{6,10})", max_dx=0.35, max_dy=0.02, umbral=1.0)
        votar(valor, conf)
        for _, m in self.p.buscar_regex(r"\bNIC\b\W*(?:\(.*?\))?\W*(\d{6,10})"):
            votar(m.group(1), 0.8)
        valor, conf = self.leer_zona("nic", validar_entero(digitos=(6, 10)), lista_blanca="0123456789")
        votar(str(valor) if valor else None, conf * 0.8)

        # "ID de Cobro(s): 1146251338 - 81" y la referencia del código de barras empiezan con el NIC
        id_cobro = [m.group(1) for _, m in self.p.buscar_regex(r"ID\.?\s*DE\s*COBROS?\W*(\d{9,12})")]
        if self.gs1 and self.gs1.referencia:
            id_cobro.append(self.gs1.referencia.lstrip("0"))
        for valor in list(candidatos):
            if any(c.startswith(valor) for c in id_cobro):
                candidatos[valor] += 0.5
        if candidatos:
            mejor = max(candidatos, key=candidatos.get)
            self.poner("identificador", mejor, min(0.98, 0.5 + 0.25 * candidatos[mejor]))
        elif id_cobro:
            # Sin etiqueta legible: el NIC son los 7 primeros dígitos del ID de cobro
            self.poner("identificador", id_cobro[0][:7], 0.45)

    def _fechas(self) -> None:
        f, c = self.fecha_cerca(["Fecha emision", "Fecha emisión"], max_dx=0.3)
        self.poner("fechaEmision", f, c)
        f, c = self.fecha_cerca(["Fecha de pago oportuno", "pago oportuno"], max_dx=0.35, max_dy=0.03)
        self.poner("fechaLimitePago", f, c)
        f, c = self.fecha_cerca(["Suspension a partir de", "Suspensión a partir"], max_dx=0.35)
        self.poner("fechaSuspension", f, c)
        self.completar_con_zona("fechaEmision", "emision", validar_fecha)
        self.completar_con_zona("fechaLimitePago", "fecha_pago", validar_fecha)
        self.completar_con_zona("fechaSuspension", "suspension", validar_fecha)

        periodo, conf = None, 0.0
        for texto, c, _ in self.textos_cerca(["Periodo facturado"], max_dx=0.5, max_dy=0.02):
            periodo = _validar_periodo(texto)
            if periodo:
                conf = 0.6 + 0.35 * c
                break
        if not periodo:
            periodo, conf = self.leer_zona("periodo", _validar_periodo)
        if periodo:
            inicio, fin = periodo
            self.poner("periodoInicio", inicio, conf)
            self.poner("periodoFin", fin, conf)
            self.poner("diasFacturados", (fin - inicio).days, conf)

        fin = self.datos.get("periodoFin")
        emision = self.datos.get("fechaEmision")
        if fin:
            self.poner("periodoFacturado", (fin.year, fin.month), self.confianza["periodoFin"])
        elif emision:
            self.poner("periodoFacturado", (emision.year, emision.month), self.confianza["fechaEmision"] * 0.9)

    def _titular_direccion(self) -> None:
        titular, conf, _ = self.texto_debajo(["Titular de Pago", "Titular", "Usuario o suscriptor"], max_dy=0.025)
        self.poner("titular", nombre_limpio(titular), conf)
        self.completar_con_zona("titular", "titular", nombre_limpio)

        ref = self.p.buscar("Direccion de suministro", umbral=0.75)
        if ref:
            debajo = self.p.debajo(ref[0], max_dy=0.06, holgura_x=0.02)
            if debajo:
                direccion, conf = self.releer_linea(debajo[0], direccion_valida)
                if direccion:
                    self.poner("direccion", direccion, conf)
                else:
                    self.poner("direccion", direccion_limpia(debajo[0].texto), 0.4)
            if len(debajo) > 1:
                barrio = _validar_barrio(debajo[1].texto)
                self.poner("barrio", barrio, 0.4 + 0.4 * debajo[1].conf)
        self.completar_con_zona("direccion", "direccion", direccion_valida)
        self.completar_con_zona("barrio", "barrio", _validar_barrio)

        estrato, conf = self.regex_cerca(["Estrato", "Clasificacion"], r"ESTRATO\s*([1-6])", max_dy=0.03, umbral=0.75)
        if estrato:
            self.poner("estrato", int(estrato), conf)
        for _, m in self.p.buscar_regex(r"RESID\w*\.?\s*ESTRATO\s*([1-6])"):
            self.poner("estrato", int(m.group(1)), 0.75)
        self.completar_con_zona("estrato", "estrato", _validar_estrato)

    def _factura(self) -> None:
        for _, m in self.p.buscar_regex(r"(?:N\.?\s*FACTURA|DOCUMENTO EQUIVALENTE N\w*\.?)\W*(\d{10,16})"):
            self.poner("numeroFactura", m.group(1), 0.8)
        valor, conf = self.regex_cerca(
            ["Documento equivalente No", "N. factura"], r"(?<!\d)(\d{12,16})(?!\d)", max_dx=0.3, debajo=False, umbral=0.7
        )
        self.poner("numeroFactura", valor, conf * 0.9)
        valor, conf = self.leer_zona("documento", validar_entero(digitos=(12, 16)), lista_blanca="0123456789")
        self.poner("numeroFactura", str(valor) if valor else None, conf * 0.9)

    def _historial(self) -> None:
        ref = self.p.buscar("Consumo de los ultimos", umbral=0.75) or self.p.buscar("ultimos 6 meses", umbral=0.75)
        prom = self.p.buscar("Promedio Consumo Diario", umbral=0.75)
        if ref:
            r = ref[0]
            zona = (max(0.0, r.x0 - 0.03), r.y1, max(r.x1 + 0.12, r.x0 + 0.40), r.y1 + 0.13)
            if prom and r.y1 < prom[0].y0 < r.y1 + 0.2:
                zona = (zona[0], zona[1], zona[2], prom[0].y0 + 0.002)
        elif prom:
            p = prom[0]
            zona = (max(0.0, p.x0 - 0.05), p.y0 - 0.11, p.x0 + 0.40, p.y0 + 0.002)
        else:
            zona = self.zona("grafica")

        valores, conf = leer_fila_numeros(self.p, zona, minimo=1, maximo=99999, esperados=7)
        if not valores:
            self.advertencias.append("No se pudo leer la gráfica de consumos; ingresa los meses manualmente.")
            return
        if len(valores) < 7:
            self.advertencias.append(
                f"Se leyeron {len(valores)} de 7 meses en la gráfica de consumo; completa los que falten."
            )
        self.poner_historial(valores, self.datos.get("periodoFacturado"), confianza=conf)
        if valores[-1] is not None:
            self.poner("consumoActual", valores[-1], conf)
