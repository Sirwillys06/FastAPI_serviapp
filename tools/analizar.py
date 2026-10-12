"""Analiza una o varias fotos de recibos desde la consola y muestra el JSON.

Uso:
    python -m tools.analizar ruta/recibo.jpg [--empresa AFINIA] [--lineas]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.recibo import Empresa  # noqa: E402
from app.services.analizador import analizar_recibo  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("imagenes", nargs="+", type=Path)
    parser.add_argument("--empresa", choices=[e.value for e in Empresa])
    parser.add_argument("--lineas", action="store_true", help="Muestra las líneas OCR con su posición")
    args = parser.parse_args()

    for ruta in args.imagenes:
        print(f"===== {ruta.name}")
        resultado = analizar_recibo(ruta.read_bytes(), Empresa(args.empresa) if args.empresa else None)
        print(json.dumps(resultado.model_dump(mode="json"), ensure_ascii=False, indent=2))
        if args.lineas:
            from app.services.imagen import preprocesar
            from app.services.ocr import PaginaOcr

            pagina = PaginaOcr(preprocesar(ruta.read_bytes()).gris)
            for linea in pagina.lineas:
                print(f"{linea.y0:.3f} {linea.x0:.3f}-{linea.x1:.3f} {linea.conf:.2f} {linea.texto}")


if __name__ == "__main__":
    main()
