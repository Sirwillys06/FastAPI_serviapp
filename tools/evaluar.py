"""
Mide la extracción campo por campo contra valores esperados.

Uso (desde la raíz del proyecto):

    .venv\\Scripts\\python.exe -m tools.evaluar            # OCR completo
    .venv\\Scripts\\python.exe -m tools.evaluar --reusar   # reutiliza el OCR guardado
    .venv\\Scripts\\python.exe -m tools.evaluar --reusar --pases full_gray_sparse,full_normalized

`samples/esperado.json` contiene los valores leídos a mano de cada foto
(null = el recibo no trae ese dato). El histórico se escribe del más
reciente al más antiguo (posición 1, 2, ...).

Categorías por campo:
  correcto         valor igual al esperado
  error_marcado    valor distinto, pero marcado como dudoso o confianza baja
  error_sin_aviso  valor distinto presentado como fiable (lo más grave)
  faltante         null, aunque el recibo trae el dato
  inventado        el recibo no trae el dato y se devolvió un valor
  no_aplica        el recibo no trae el dato y se devolvió null
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from app.receipt_parser import build_receipt


ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
CACHE = SAMPLES / ".cache"


def get_path(data, path):
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def classify(expected, value, review):
    if expected is None:
        return "no_aplica" if value is None else "inventado"
    if value is None:
        return "faltante"
    if value == expected:
        return "correcto"

    flagged = review.get("estado") != "propuesto" or review.get("confianza") == "baja"

    return "error_marcado" if flagged else "error_sin_aviso"


def load_ocr(image: Path, reuse: bool) -> dict:
    cache_file = CACHE / f"{image.stem}.ocr.json"

    if reuse and cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    from app.pipeline import run_ocr

    result = run_ocr(image.read_bytes())
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reusar", action="store_true", help="usar el OCR guardado en samples/.cache")
    parser.add_argument("--pases", help="solo estas pasadas de página completa (las zonas se conservan)")
    parser.add_argument("--json", action="store_true", help="imprimir la respuesta de cada recibo")
    parser.add_argument("--solo", help="evaluar solo este archivo")
    args = parser.parse_args()

    expected_all = json.loads((SAMPLES / "esperado.json").read_text(encoding="utf-8"))
    totals = Counter()
    point_totals = Counter()

    for filename, expected in expected_all.items():
        if filename.startswith("_") or (args.solo and args.solo != filename):
            continue

        image = SAMPLES / filename

        if not image.exists():
            print(f"(falta {filename})")
            continue

        ocr = load_ocr(image, args.reusar)

        if args.pases:
            keep = set(args.pases.split(","))
            ocr = {
                **ocr,
                "readings": [
                    reading
                    for reading in ocr["readings"]
                    if reading["name"].startswith("zona:") or reading["name"] in keep
                ],
            }

        receipt = build_receipt(ocr)
        data = receipt["datos"]
        reviews = receipt["revision"]["campos"]

        if args.json:
            receipt["evidencia"].pop("lecturas", None)
            print(json.dumps(receipt, ensure_ascii=False, indent=2))

        print(
            f"\n=== {filename}  ({ocr.get('elapsed_seconds')} s, "
            f"{len(ocr['readings'])} lecturas, {len(ocr.get('regions', []))} zonas)"
        )

        for key, value_expected in expected.items():
            if key == "consumo.historico":
                continue

            value = get_path(data, key)
            review = reviews.get(key, {})

            if key in ("consumo.estado_historico", "empresa"):
                verdict = "correcto" if value == value_expected else "faltante"
            else:
                verdict = classify(value_expected, value, review)

            totals[verdict] += 1
            mark = {"correcto": "OK ", "no_aplica": " - ", "faltante": "-- "}.get(verdict, "XX ")
            extra = ""

            if verdict not in ("correcto", "no_aplica"):
                extra = f"  esperado={value_expected!r}"
                alternatives = [item["valor"] for item in review.get("alternativas", [])]

                if alternatives:
                    extra += f"  alternativas={alternatives}"

            state = f"[{review.get('estado', '')} {review.get('confianza', '')}]".replace(" ]", "]")
            print(f"  {mark}{key:26} {value!r:28} {state:22} {verdict}{extra}")

        expected_points = expected.get("consumo.historico") or []
        points = {point["posicion"]: point["valor"] for point in data["consumo"]["historico"]}
        line = []

        for position, value_expected in enumerate(expected_points, start=1):
            value = points.get(position)

            if value is None:
                point_totals["faltante"] += 1
                line.append(f"{position}:--")
            elif value == value_expected:
                point_totals["correcto"] += 1
                line.append(f"{position}:{value}")
            else:
                point_totals["incorrecto"] += 1
                line.append(f"{position}:{value}!={value_expected}")

        extra_points = [position for position in points if position > len(expected_points)]

        if extra_points:
            point_totals["sobrantes"] += len(extra_points)

        print(f"  historico (1 = actual): {'  '.join(line)}")

    print("\n=== Resumen de campos:", dict(totals))
    print("=== Puntos del histórico:", dict(point_totals))


if __name__ == "__main__":
    main()
