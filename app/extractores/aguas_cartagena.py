"""Extractor del recibo de Aguas de Cartagena (acueducto, alcantarillado y aseo Pacaribe)."""

from __future__ import annotations

import re

from ..schemas.recibo import ConceptoValor, Empresa
from ..utils.fechas import buscar_mes_anio
from ..utils.numeros import VALOR_PESOS, parsear_pesos
from ..utils.texto import normalizar
from .base import (
    ExtractorBase,
    corregir_digitos_en_numeros,
    direccion_limpia,
    nombre_limpio,
    validar_entero,
    validar_fecha,
    validar_pesos,
)
from .graficas import leer_barras


def _validar_factura(texto: str) -> str | None:
    m = re.search(r"\b(\d{4,6}20\d{2}[A-Z0-9]{6,12})\b", normalizar(texto).replace(" ", ""))
    return m.group(1) if m else None


def _validar_domicilio(texto: str) -> str | None:
    texto = re.sub(r"(?i)domicilio", "", texto)
    return direccion_limpia(texto)


def _validar_nombre(texto: str) -> str | None:
    return nombre_limpio(re.sub(r"(?i)nombre\s*abonado", "", texto))


class ExtractorAguasCartagena(ExtractorBase):
    empresa = Empresa.AGUAS_DE_CARTAGENA

    ANCLAS = {
        "DOCUMENTO EXPEDIDO PARA SERVICIOS": (0.060, 0.044),
        "ULTIMO DIA DE PAGO SIN RECARGO": (0.605, 0.044),
        "OTROS COMPONENTES DE LA TARIFA": (0.113, 0.304),
        "ASEO PACARIBE": (0.079, 0.465),
        "ACUEDUCTO CARGO FIJO": (0.465, 0.464),
        "Aprovechamiento Resolucion": (0.085, 0.488),
    }
    ZONAS = {
        "factura": (0.175, 0.112, 0.31, 0.130),
        "periodo": (0.37, 0.112, 0.43, 0.130),
        "emision": (0.575, 0.112, 0.70, 0.130),
        "nombre": (0.175, 0.134, 0.45, 0.151),
        "domicilio": (0.585, 0.134, 0.93, 0.151),
        "ultimo_dia": (0.62, 0.068, 0.82, 0.090),
        "poliza": (0.53, 0.172, 0.63, 0.196),
        "lectura_anterior": (0.46, 0.252, 0.59, 0.266),
        "lectura_actual": (0.62, 0.252, 0.75, 0.266),
        "consumo_mes": (0.79, 0.252, 0.93, 0.266),
        "fecha_lectura_anterior": (0.46, 0.275, 0.59, 0.290),
        "fecha_lectura_actual": (0.62, 0.275, 0.75, 0.290),
        "grafica": (0.735, 0.310, 0.955, 0.378),
        "suspension": (0.69, 0.378, 0.86, 0.394),
        "subtotal_aseo": (0.33, 0.524, 0.44, 0.540),
        "subtotal_acueducto": (0.84, 0.532, 0.94, 0.548),
        "total": (0.80, 0.772, 0.95, 0.792),
    }

    def extraer_campos(self) -> None:
        self._poliza()
        self.poner_total(["TOTAL A PAGAR"], max_dx=0.45)
        self.completar_con_zona("valorTotal", "total", validar_pesos)
        self.poner_fecha_limite_gs1()
        self._fechas()
        self._datos_usuario()
        self._lecturas()
        self._historial()
        self._conceptos()

    # ---------------------------------------------------------------- campos
    def _poliza(self) -> None:
        votos: dict[str, float] = {}

        def votar(valor, conf):
            if valor:
                votos[str(valor)] = votos.get(str(valor), 0) + conf

        for etiqueta in ("POLIZA", "Póliza", "Cliente"):
            votar(*self.regex_cerca(etiqueta, r"(?<!\d)(\d{5,9})(?!\d)", max_dx=0.25, max_dy=0.03, umbral=0.8))
        for _, m in self.p.buscar_regex(r"P[O0]LIZA\W*(\d{5,9})\b"):
            votar(m.group(1), 0.8)
        votar(*self.leer_zona("poliza", validar_entero(digitos=(5, 9)), lista_blanca="0123456789"))
        if votos:
            mejor = max(votos, key=votos.get)
            self.poner("identificador", mejor, min(0.98, 0.45 + 0.2 * votos[mejor]))

    def _fechas(self) -> None:
        f, c = self.fecha_cerca(["Fecha de Emision", "Fecha de Emisión"], max_dx=0.3)
        self.poner("fechaEmision", f, c)
        f, c = self.fecha_cerca(["ULTIMO DIA DE PAGO SIN RECARGO", "DIA DE PAGO SIN RECARGO"], max_dy=0.05, umbral=0.75)
        self.poner("fechaLimitePago", f, c)
        f, c = self.fecha_cerca(["Suspension a partir de", "Suspensión a partir"], max_dx=0.25)
        self.poner("fechaSuspension", f, c)
        self.completar_con_zona("fechaEmision", "emision", validar_fecha)
        self.completar_con_zona("fechaLimitePago", "ultimo_dia", validar_fecha)
        self.completar_con_zona("fechaSuspension", "suspension", validar_fecha)

        for texto, conf, _ in self.textos_cerca(["Periodo"], max_dx=0.15, debajo=False, umbral=0.85):
            mes = buscar_mes_anio(texto)
            if mes:
                self.poner("periodoFacturado", mes, 0.55 + 0.4 * conf)
                break
        for _, m in self.p.buscar_regex(r"PERIODO\W*(20\d{2})\s?/\s?(\d{2})"):
            self.poner("periodoFacturado", (int(m.group(1)), int(m.group(2))), 0.85)
        self.completar_con_zona("periodoFacturado", "periodo", buscar_mes_anio)
        if "periodoFacturado" not in self.datos and self.datos.get("fechaEmision"):
            e = self.datos["fechaEmision"]
            self.poner("periodoFacturado", (e.year, e.month), 0.5)

        for _, m in self.p.buscar_regex(r"FACTURA DE VENTA\W*([0-9A-Z]{12,22})"):
            self.poner("numeroFactura", _validar_factura(m.group(1)), 0.75)
        self.completar_con_zona("numeroFactura", "factura", _validar_factura)

    def _datos_usuario(self) -> None:
        for texto, conf, _ in self.textos_cerca(["Nombre abonado"], max_dx=0.4, debajo=False):
            nombre = _validar_nombre(texto)
            if nombre:
                self.poner("titular", nombre, 0.45 + 0.45 * conf)
                break
        self.completar_con_zona("titular", "nombre", _validar_nombre)

        for texto, conf, _ in self.textos_cerca(["Domicilio"], max_dx=0.4, debajo=False):
            direccion = _validar_domicilio(texto)
            if direccion:
                self.poner("direccion", direccion, 0.45 + 0.45 * conf)
                break
        self.completar_con_zona("direccion", "domicilio", _validar_domicilio)
        # El barrio aparece al final del domicilio: "C32 39 13 ALCIBIA"
        m = re.search(r"\d\s+([A-ZÑ][A-ZÑ ]{3,})$", self.datos.get("direccion") or "")
        if m:
            self.poner("barrio", m.group(1).strip(), self.confianza["direccion"] * 0.9)

        estrato, conf = self.regex_cerca("Estrato", r"(?<!\d)([1-6])(?!\d)", max_dx=0.1, max_dy=0.03, umbral=0.9)
        if estrato:
            self.poner("estrato", int(estrato), conf * 0.8)

    def _lecturas(self) -> None:
        lectura = validar_entero(0, 999999, digitos=(1, 6))
        for campo, zona, etiqueta in (
            ("lecturaAnterior", "lectura_anterior", "Lectura anterior"),
            ("lecturaActual", "lectura_actual", "Lectura actual"),
            ("consumoActual", "consumo_mes", "Consumo mes"),
        ):
            for ref in self.p.buscar(etiqueta, umbral=0.75):
                for linea in self.p.debajo(ref, max_dy=0.03, holgura_x=0.02):
                    valor = lectura(linea.texto)
                    if valor is not None and not re.search(r"\d-\d", linea.texto):
                        self.poner(campo, float(valor), 0.45 + 0.45 * linea.conf)
                        break
            valor, conf = self.leer_zona(zona, lectura, lista_blanca="0123456789")
            self.poner(campo, float(valor) if valor is not None else None, conf)

        ant, act = self.datos.get("lecturaAnterior"), self.datos.get("lecturaActual")
        consumo = self.datos.get("consumoActual")
        if ant is not None and act is not None and act >= ant:
            if consumo is None:
                self.poner("consumoActual", act - ant, 0.7)
            elif consumo == act - ant:
                # Lecturas y consumo coinciden: los tres datos quedan confirmados
                for campo in ("lecturaAnterior", "lecturaActual", "consumoActual"):
                    self.confianza[campo] = max(self.confianza[campo], 0.95)
            else:
                for campo in ("lecturaAnterior", "lecturaActual"):
                    self.confianza[campo] = min(self.confianza[campo], 0.5)

        f_ant, c1 = self.fecha_cerca(["Fecha lectura anterior"], derecha=False, max_dy=0.03, umbral=0.75)
        f_act, c2 = self.fecha_cerca(["Fecha lectura actual"], derecha=False, max_dy=0.03, umbral=0.75)
        self.poner("periodoInicio", f_ant, c1)
        self.poner("periodoFin", f_act, c2)
        self.completar_con_zona("periodoInicio", "fecha_lectura_anterior", validar_fecha)
        self.completar_con_zona("periodoFin", "fecha_lectura_actual", validar_fecha)
        f_ant, f_act = self.datos.get("periodoInicio"), self.datos.get("periodoFin")
        if f_ant and f_act and 15 <= (f_act - f_ant).days <= 70:
            self.poner("diasFacturados", (f_act - f_ant).days, min(self.confianza["periodoInicio"], self.confianza["periodoFin"]))

    def _historial(self) -> None:
        zona = self.zona("grafica")
        valores, _, conf = leer_barras(self.p, zona, esperadas=7, maximo=999)
        if not valores:
            self.advertencias.append("No se pudo leer la gráfica de consumo de agua; ingresa los meses manualmente.")
            return
        self._verificar_meses_grafica(zona)
        self.poner_historial(valores, self.datos.get("periodoFacturado"), confianza=conf)
        if len(valores) != 7:
            self.advertencias.append(
                f"Se leyeron {len(valores)} de 7 meses en la gráfica de consumo; completa los que falten."
            )

    def _verificar_meses_grafica(self, zona) -> None:
        """Lee las etiquetas MM/AA bajo las barras (02/26 ... 08/26) para confirmar el periodo."""
        x0, y0, x1, y1 = zona
        franja = (x0, y0 + (y1 - y0) * 0.75, x1, y1 + 0.008)
        meses = []
        for p in sorted(self.p.palabras_region(franja, psm=11, lista_blanca="0123456789/"), key=lambda p: p.x0):
            if re.fullmatch(r"\d{2}/\d{2}", p.texto):
                mes = buscar_mes_anio(p.texto)
                if mes:
                    meses.append(mes)
        if not meses:
            return
        periodo = self.datos.get("periodoFacturado")
        if periodo is None:
            self.poner("periodoFacturado", meses[-1], 0.6)
        elif meses[-1] != periodo:
            self.advertencias.append("El último mes de la gráfica no coincide con el periodo facturado.")

    def _conceptos(self) -> None:
        subtotales = []
        for ref in self.p.buscar("SUBTOTAL", umbral=0.85):
            for texto in [ref.texto] + [l.texto for l in self.p.a_la_derecha(ref, max_dx=0.4)]:
                encontrados = [parsear_pesos(m.group()) for m in VALOR_PESOS.finditer(corregir_digitos_en_numeros(texto))]
                encontrados = [v for v in encontrados if v and v >= 100]
                if encontrados:
                    subtotales.append((ref.x0, encontrados[-1]))
                    break
        subtotales.sort()
        aseo = subtotales[0][1] if len(subtotales) >= 2 else None
        acueducto = subtotales[-1][1] if len(subtotales) >= 2 else None
        total = self.datos.get("valorTotal")
        if aseo is None or acueducto is None or (total and aseo + acueducto != total):
            aseo, _ = self.leer_zona("subtotal_aseo", validar_pesos)
            acueducto, _ = self.leer_zona("subtotal_acueducto", validar_pesos)
        if aseo and acueducto and (total is None or abs(aseo + acueducto - total) <= 10):
            self.conceptos = [
                ConceptoValor(concepto="Aseo", valor=aseo),
                ConceptoValor(concepto="Acueducto y alcantarillado", valor=acueducto),
            ]
            if total is not None:
                # Los subtotales suman el total: el valor queda confirmado por dos fuentes
                self.confianza["valorTotal"] = max(self.confianza.get("valorTotal", 0), 0.97)
