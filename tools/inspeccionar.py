"""
Depuración: muestra etiquetas encontradas y evidencias de un campo.

    .venv\\Scripts\\python.exe -m tools.inspeccionar aguas_01 fechas.emision
    .venv\\Scripts\\python.exe -m tools.inspeccionar aguas_01 --buscar "DOMICILIO"

Requiere haber ejecutado antes `tools.evaluar` (usa samples/.cache).
"""

import argparse
import json

from app.layout import build_lines, find_label, words_from_reading
from app.receipt_parser import collect_evidence, detect_provider
from tools.evaluar import CACHE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recibo")
    parser.add_argument("campo", nargs="?")
    parser.add_argument("--buscar", help="expresión regular de etiqueta")
    args = parser.parse_args()

    ocr = json.loads((CACHE / f"{args.recibo}.ocr.json").read_text(encoding="utf-8"))
    readings = [reading for reading in ocr["readings"] if reading["text"].strip()]
    words = {reading["name"]: words_from_reading(reading) for reading in readings}
    lines = {name: build_lines(items) for name, items in words.items()}

    print("zonas:", [(region["name"], region["box"]) for region in ocr.get("regions", [])])

    if args.buscar:
        for name, reading_lines in lines.items():
            for match in find_label(reading_lines, args.buscar):
                print(f"{name:45} {match.box} | {match.line.text}")
        return

    provider, _ = detect_provider([r for r in readings if not r["name"].startswith("zona:")])
    spec = next(s for s in provider.fields if s.key == args.campo or f"{s.key}.valor" == args.campo)

    for rule in spec.labels:
        print(f"\nregla {rule.pattern} ({rule.direction})")

        for name, reading_lines in lines.items():
            for match in find_label(reading_lines, rule.pattern):
                print(f"  {name:45} {match.box} | {match.line.text}")

    evidence = collect_evidence(provider, words, lines, ocr["shape"][1])

    for key, items in evidence.items():
        if key.startswith(args.campo):
            print(f"\n{key}:")

            for item in items:
                print(f"  {item.value!r:25} {item.source:45} {item.raw!r} conf={item.confidence} {item.note or ''}")


if __name__ == "__main__":
    main()
