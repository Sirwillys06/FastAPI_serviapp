# FastAPI_Serviapp — Análisis de recibos con OpenCV + OCR

Servicio que recibe la **foto de un recibo de servicios públicos** de Cartagena y devuelve
sus datos en JSON. Lo usa la app móvil de ServiApp (Flutter) en la opción **Escanear recibo**.

| Empresa | Servicio | Identificador | Historial de consumo |
|---|---|---|---|
| Afinia | Energía (kWh) | NIC | 7 meses (gráfica) |
| Aguas de Cartagena | Agua (m³) | Póliza | 7 meses (gráfica de barras) |
| Surtigas | Gas (m³) | Contrato | Solo mes actual (el recibo no trae historial) |

## Flujo completo

```
Flutter (cámara) ──foto──▶ FastAPI /recibos/analizar ──JSON──▶ Flutter (pantalla de revisión)
                                                                    │ usuario corrige y confirma
                                                                    ▼
                                         Spring Boot /api/mobile/recibos ──▶ MongoDB Atlas (colección "recibos")
                                                                    │
                                     Modelo de minería (Python) ◀── /api/mineria/series
```

FastAPI **no guarda nada**: solo procesa la imagen. Spring Boot guarda lo que el usuario confirmó.

## Cómo funciona el procesamiento

1. **OpenCV – imagen** (`app/services/imagen.py`): corrige la orientación EXIF, detecta el borde de
   la hoja y corrige la perspectiva, endereza rotaciones (OSD de Tesseract), normaliza la
   iluminación (sombras) y sube el contraste.
2. **Códigos** (`app/services/codigos.py`): decodifica el código de barras GS1-128 del pie
   (valor a pagar y fecha límite) con zxing-cpp. Si la foto no tiene resolución suficiente, ubica
   las barras con gradientes de OpenCV y lee el texto impreso `(415)…(8020)…(3900)…(96)…`.
3. **OCR** (`app/services/ocr.py`): Tesseract en español, con posición de cada palabra.
4. **Detector** (`app/services/detector.py`): reconoce la empresa por palabras clave o por el GLN
   del código de barras.
5. **Extractores** (`app/extractores/`): un extractor por empresa combina tres fuentes por campo
   y se queda con la más confiable:
   - texto cerca de la etiqueta ("Total a Pagar", "Fecha emisión", …),
   - **plantilla**: zonas fijas del formato de cada empresa, ajustadas a la foto con "anclas",
   - código de barras.
6. **Gráficas** (`app/extractores/graficas.py`): segmenta los números con componentes conectados
   (Afinia) o detecta las barras y usa la proporción altura/valor para validar el OCR (Aguas).
7. **Validaciones cruzadas**: lecturas vs consumo, subtotales vs total, coherencia de fechas
   (corrige años mal leídos), y una **confianza 0..1 por campo**. La app resalta los campos con
   confianza < 0.6 para que el usuario los revise.

## Instalación (Windows)

1. Instalar [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) con el idioma **Spanish**.
   Si no queda en el PATH, definir `TESSERACT_CMD` en `.env` (ver `.env.example`).
2. Crear el entorno e instalar dependencias:

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
```

## Ejecutar

```bash
.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- `--host 0.0.0.0` permite que el celular (en la misma red Wi-Fi) llegue al PC.
- La primera vez Windows pregunta por el firewall: **permitir en redes privadas**.
- Documentación interactiva: http://localhost:8000/docs

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado del servicio y si encontró Tesseract |
| GET | `/recibos/empresas` | Empresas soportadas |
| POST | `/recibos/analizar` | `multipart/form-data` con `imagen` (archivo) y opcional `empresa` |

Errores (`detail.codigo`): `IMAGEN_INVALIDA` (400), `IMAGEN_MUY_GRANDE` (413),
`EMPRESA_NO_RECONOCIDA` (422; la app permite elegir la empresa y reintentar),
`OCR_NO_DISPONIBLE` (503), `API_KEY_INVALIDA` (401, solo si se configuró `SERVIAPP_API_KEY`).

El formato del JSON está documentado en [docs/CONTRATO_JSON.md](docs/CONTRATO_JSON.md).

## Herramientas de desarrollo

```bash
# Analizar fotos desde la consola (muestra el JSON; --lineas muestra el OCR con posiciones)
.venv\Scripts\python -m tools.analizar ruta\recibo.jpg [--empresa AFINIA] [--lineas]

# Pruebas
.venv\Scripts\python -m pytest
```

> Las fotos reales de recibos tienen datos personales: **no las subas al repositorio**.
> Las pruebas usan un recibo sintético con datos ficticios.
