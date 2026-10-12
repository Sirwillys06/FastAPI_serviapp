"""
Compara configuraciones de relectura de zonas con los recibos de prueba.

Para cada configuración (variante de imagen, PSM, escala) relee todas las
zonas planificadas una vez; luego evalúa cada par de configuraciones
combinando sus lecturas con las pasadas de página completa guardadas.

    .venv\\Scripts\\python.exe -m tools.experimento_zonas

Requiere haber ejecutado antes `tools.evaluar` (usa samples/.cache).
"""

import itertools
import json
import time

from app import zones
from app.image_processing import prepare_receipt_image
from app.layout import build_lines, words_from_reading
from app.ocr_service import OcrSession, read_regions
from app.receipt_parser import build_receipt, detect_provider
from tools.evaluar import CACHE, SAMPLES, classify, get_path


CONFIGS = {
    "gris6": ("gray", 6, 40),
    "gris11": ("gray", 11, 40),
    "norm6": ("normalized", 6, 40),
    "norm11": ("normalized", 11, 40),
    "gris6_x60": ("gray", 6, 60),
    "gris11_x60": ("gray", 11, 60),
    "norm11_x60": ("normalized", 11, 60),
}


def score(receipt, expected):
    good = bad = 0

    for key, value in expected.items():
        if key in ("consumo.historico", "consumo.estado_historico", "empresa"):
            continue

        verdict = classify(value, get_path(receipt["datos"], key), receipt["revision"]["campos"].get(key, {}))
        good += verdict in ("correcto", "no_aplica")
        bad += verdict in ("error_sin_aviso", "inventado")

    points = {p["posicion"]: p["valor"] for p in receipt["datos"]["consumo"]["historico"]}
    good += sum(points.get(i + 1) == v for i, v in enumerate(expected.get("consumo.historico", [])))

    return good, bad


def main():
    expected_all = json.loads((SAMPLES / "esperado.json").read_text(encoding="utf-8"))
    per_config = {name: {} for name in CONFIGS}
    seconds = {name: 0.0 for name in CONFIGS}
    bases = {}

    for filename, expected in expected_all.items():
        if filename.startswith("_"):
            continue

        ocr = json.loads((CACHE / f"{filename.rsplit('.', 1)[0]}.ocr.json").read_text(encoding="utf-8"))
        base = [r for r in ocr["readings"] if not r["name"].startswith("zona:")]
        bases[filename] = {**ocr, "readings": base}
        gray = prepare_receipt_image((SAMPLES / filename).read_bytes())
        provider, _ = detect_provider([r for r in base if r["text"].strip()])
        lines = {r["name"]: build_lines(words_from_reading(r)) for r in base if r["text"].strip()}

        for name, read in CONFIGS.items():
            regions = zones.plan_regions(provider, lines, gray.shape)

            for region in regions:
                region["reads"] = [read]

            started = time.monotonic()
            readings = read_regions(OcrSession(), gray, regions)
            seconds[name] += time.monotonic() - started

            for reading in readings:
                reading["name"] = f"{reading['name']}@{name}"

            per_config[name][filename] = readings

    def evaluate(names):
        good = bad = 0

        for filename, expected in expected_all.items():
            if filename.startswith("_"):
                continue

            readings = list(bases[filename]["readings"])

            for name in names:
                readings += per_config[name][filename]

            g, b = score(build_receipt({**bases[filename], "readings": readings}), expected)
            good += g
            bad += b

        return good, bad

    print("solo página completa:", evaluate([]))
    results = []

    for size in (1, 2):
        for names in itertools.combinations(CONFIGS, size):
            good, bad = evaluate(names)
            cost = sum(seconds[name] for name in names)
            results.append((good, -bad, -cost, names))

    for good, bad, cost, names in sorted(results, reverse=True)[:12]:
        print(f"{'+'.join(names):28} aciertos={good:3}  errores_sin_aviso={-bad}  segundos_zonas={-cost:5.1f}")


if __name__ == "__main__":
    main()
