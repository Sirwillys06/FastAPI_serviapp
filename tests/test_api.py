"""Pruebas de la API con un recibo sintético (datos ficticios)."""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from app.core.config import get_settings
from app.main import app

client = TestClient(app)

requiere_tesseract = pytest.mark.skipif(get_settings().tesseract_cmd is None, reason="Tesseract no instalado")


def _fuente(tamano: int):
    for nombre in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(nombre, tamano)
        except OSError:
            continue
    return ImageFont.load_default(size=tamano)


def _recibo_afinia_sintetico() -> bytes:
    """Dibuja un recibo con la estructura de Afinia y datos inventados."""
    img = Image.new("RGB", (1700, 2200), "white")
    d = ImageDraw.Draw(img)
    grande, normal = _fuente(56), _fuente(34)
    d.text((80, 60), "afinia Grupo EPM", font=grande, fill="black")
    d.text((80, 140), "Caribemar de la Costa S.A.S E.S.P.", font=normal, fill="black")
    d.text((1100, 60), "NIC: 7654321", font=grande, fill="black")
    d.text((1100, 160), "Total a Pagar: $123.450", font=normal, fill="black")
    d.text((80, 260), "Titular de Pago", font=normal, fill="black")
    d.text((80, 310), "JUAN PEREZ PRUEBA", font=normal, fill="black")
    d.text((700, 260), "Direccion de suministro", font=normal, fill="black")
    d.text((700, 310), "CL 10 20-30", font=normal, fill="black")
    d.text((1100, 260), "Fecha emision: 10/09/2026", font=normal, fill="black")
    d.text((1100, 330), "Suspension a partir de: 18/09/2026", font=normal, fill="black")
    d.text((80, 420), "Estrato/Clasificacion", font=normal, fill="black")
    d.text((80, 470), "Resid. Estrato 2", font=normal, fill="black")
    d.text((80, 580), "Periodo facturado   10/08/2026 - 08/09/2026", font=normal, fill="black")
    d.text((80, 800), "Consumo de los ultimos 6 meses (kWh)", font=normal, fill="black")
    for i, valor in enumerate((150, 160, 170, 180, 190, 200, 210)):
        d.rectangle((90 + i * 105, 900, 160 + i * 105, 1050), outline="black", width=3)
        d.text((92 + i * 105, 1070), str(valor), font=normal, fill="black")
    d.text((80, 1150), "Promedio Consumo Diario (kWh): 7,00", font=normal, fill="black")
    d.text((80, 1300), "Cuidarnos es una tarea compartida. Reporta al 115 o por AfiniApp", font=normal, fill="black")
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["estado"] == "ok"


def test_empresas():
    r = client.get("/recibos/empresas")
    assert {e["empresa"] for e in r.json()} == {"AFINIA", "AGUAS_DE_CARTAGENA", "SURTIGAS"}


def test_archivo_que_no_es_imagen():
    r = client.post("/recibos/analizar", files={"imagen": ("x.jpg", b"no soy una imagen", "image/jpeg")})
    assert r.status_code == 400
    assert r.json()["detail"]["codigo"] == "IMAGEN_INVALIDA"


@requiere_tesseract
def test_imagen_sin_recibo():
    img = Image.new("RGB", (1200, 1600), "white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    r = client.post("/recibos/analizar", files={"imagen": ("blanco.png", buffer.getvalue(), "image/png")})
    assert r.status_code == 422
    assert r.json()["detail"]["codigo"] == "EMPRESA_NO_RECONOCIDA"


@requiere_tesseract
def test_analiza_recibo_afinia_sintetico():
    r = client.post("/recibos/analizar", files={"imagen": ("afinia.jpg", _recibo_afinia_sintetico(), "image/jpeg")})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    recibo = cuerpo["recibo"]
    assert recibo["empresa"] == "AFINIA"
    assert recibo["tipoServicio"] == "ENERGIA"
    assert recibo["identificador"] == {"tipo": "NIC", "valor": "7654321"}
    assert recibo["valorTotal"] == 123450
    assert recibo["fechaEmision"] == "2026-09-10"
    assert recibo["fechaSuspension"] == "2026-09-18"
    assert recibo["periodoFacturado"] == "2026-09"
    assert recibo["diasFacturados"] == 29
    assert recibo["titular"] == "JUAN PEREZ PRUEBA"
    assert recibo["direccion"] == "CL 10 20-30"
    assert recibo["estrato"] == 2
    consumos = [m["consumo"] for m in recibo["historialConsumos"]]
    assert consumos == [150, 160, 170, 180, 190, 200, 210]
    assert recibo["historialConsumos"][-1] == {"periodo": "2026-09", "consumo": 210, "esActual": True}
    assert recibo["historialConsumos"][0]["periodo"] == "2026-03"
    assert recibo["consumoActual"] == 210
