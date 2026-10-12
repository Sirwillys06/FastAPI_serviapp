"""Detecta a qué empresa pertenece el recibo."""

from __future__ import annotations

from ..schemas.recibo import Empresa
from ..utils.texto import contiene_aprox, normalizar
from .codigos import DatosGs1

# GLN (código GS1 de la empresa) que aparece en el código de barras (415)
GLN_EMPRESAS = {
    "7709998169074": Empresa.AFINIA,
    "7709998014633": Empresa.AGUAS_DE_CARTAGENA,
    "7709998002777": Empresa.SURTIGAS,
}

PALABRAS_CLAVE: dict[Empresa, list[tuple[str, float]]] = {
    Empresa.AFINIA: [
        ("AFINIA", 3), ("CARIBEMAR", 3), ("GRUPO EPM", 1.5), ("AFINIAPP", 2), ("NIC", 1),
        ("KWH", 2), ("ALUMBRADO", 1), ("ENERGIA", 1),
    ],
    Empresa.AGUAS_DE_CARTAGENA: [
        ("AGUAS DE CARTAGENA", 3), ("ACUEDUCTO", 2), ("ALCANTARILLADO", 2), ("PACARIBE", 2),
        ("POLIZA", 1.5), ("ACUACAR", 2), ("CHAMBACU", 0.5),
    ],
    Empresa.SURTIGAS: [
        ("SURTIGAS", 3), ("PROMIGAS", 3), ("BRILLA", 2), ("CONSUMO DE GAS", 2),
        ("NO. DE CONTRATO", 1), ("REVISION PERIODICA", 1.5), ("NEQUI", 0.5),
    ],
}


def detectar_empresa(texto: str, gs1: DatosGs1 | None = None) -> tuple[Empresa | None, float]:
    """Devuelve la empresa más probable y una confianza 0..1."""
    if gs1 and gs1.gln in GLN_EMPRESAS:
        return GLN_EMPRESAS[gs1.gln], 0.99

    texto_n = normalizar(texto)
    puntajes: dict[Empresa, float] = {}
    for empresa, claves in PALABRAS_CLAVE.items():
        puntaje = 0.0
        for clave, peso in claves:
            if len(clave) <= 4:
                # Palabras cortas: coincidencia exacta de palabra para evitar falsos positivos
                if f" {clave} " in f" {texto_n} ":
                    puntaje += peso
            elif contiene_aprox(texto_n, clave, 0.82):
                puntaje += peso
        puntajes[empresa] = puntaje

    mejor = max(puntajes, key=puntajes.get)
    total = sum(puntajes.values())
    if puntajes[mejor] < 2.5:
        return None, 0.0
    return mejor, round(min(0.98, puntajes[mejor] / total), 2)
