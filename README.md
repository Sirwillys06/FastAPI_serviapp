# ServiApp OCR (FastAPI)

Servicio que recibe la foto de un recibo de servicios públicos de Cartagena
(Afinia, Surtigas, Aguas de Cartagena) y devuelve datos estructurados para
que el usuario los confirme en Flutter.

## Ejecutar

```powershell
$env:TESSERACT_CMD = "D:\Tesseract OCR\tesseract.exe"
$env:OCR_LANG = "spa"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

- `GET /health`
- `POST /v1/extractions` — imagen JPEG/PNG en el campo multipart `file`.
  - `?detalle=compacto` (por defecto): datos, revisión y evidencia por campo.
  - `?detalle=completo`: añade todas las palabras OCR con coordenadas.
    Pensado para que Spring Boot lo guarde junto a la imagen en S3.

Un solo recibo se procesa a la vez por proceso; una petición simultánea
recibe 503.

## Cómo funciona

1. `image_processing`: valida, orienta y escala la imagen; variantes gris,
   normalizada y adaptativa.
2. `ocr_service`: 4 pasadas de página completa (`OCR_PASES` permite elegir).
3. `zones`: busca las etiquetas de la empresa detectada y relee de forma
   enfocada la zona de cada una, además de la gráfica de consumo.
4. `receipt_parser`: para cada campo toma el valor a la derecha o debajo de
   su etiqueta (`layout`), agrupa evidencias y elige candidato
   (`candidates`), lee la gráfica por coordenadas (`charts`) y aplica
   validaciones cruzadas (período de ~1 mes, fechas cercanas entre sí,
   orden emisión ≤ pago ≤ suspensión).
5. `providers`: reglas por empresa (etiquetas, dirección, formatos).

Reglas que no se rompen: no se completan cifras, no se inventan meses, no se
convierten desconocidos en cero, no se presenta un número de factura que no
cumpla el formato completo, y una barra ilegible queda con `valor: null` en
su posición.

## Respuesta

```jsonc
{
  "archivo": "recibo.jpg",
  "version_esquema": "2.0",
  "estado": "pendiente_revision",        // Spring lo cambia a "confirmado"
  "datos": {
    "empresa": "Afinia", "servicio": "energia",
    "identificador": {"tipo": "NIC", "valor": "1146251"},
    "numero_factura": "22402609004100",
    "direccion_suministro": "CL 31A 39-15",
    "periodo": {"mes": null, "inicio": "2026-08-10", "fin": "2026-09-08"},
    "fechas": {"emision": "...", "pago_oportuno": "...", "suspension": "..."},
    "valores": {"moneda": "COP", "total_facturado": 427630},
    "consumo": {
      "unidad": "kWh", "actual": 502,
      "historico": [                      // posicion 1 = período actual
        {"posicion": 1, "valor": 502, "unidad": "kWh", "periodo": null, "es_actual": true},
        {"posicion": 6, "valor": null, "unidad": "kWh", "periodo": null, "es_actual": false}
      ],
      "estado_historico": "parcial"       // completo | parcial | declarado_sin_historico | no_extraido
    },
    "adicional": {"promedio_consumo_diario": 15.61}
  },
  "revision": {
    "requiere_confirmacion": true,
    "campos_pendientes": ["fechas.suspension", "consumo.historico"],
    "campos": {
      "fechas.emision": {
        "estado": "dudoso",               // propuesto | dudoso | incompleto | no_encontrado | no_aplica
        "confianza": "baja",              // alta | media | baja (orden, no probabilidad)
        "respaldo": {"lecturas": 2, "ubicaciones": 1, "notas": ["fecha_incoherente"]},
        "alternativas": [{"valor": "...", "lecturas": 1, "ubicaciones": 1}],
        "motivo": "La fecha está lejos de las demás fechas del recibo."
      }
    },
    "advertencias": ["..."]
  },
  "evidencia": {"campos": {}, "lecturas": [], "grafica": {}, "tiempo_segundos": 25.1}
}
```

Flutter debería mostrar `datos` como campos editables y resaltar los de
`revision.campos_pendientes`, ofreciendo `alternativas`. Para el histórico,
mostrar los puntos con `posicion` ≤ 6. El total facturado no indica pago.

## Medir antes de cambiar

`samples/` (ignorado por git: son fotos con datos personales) contiene las
fotos y `esperado.json` con los valores leídos a mano.

```powershell
.\.venv\Scripts\python.exe -m tools.evaluar             # OCR + medición
.\.venv\Scripts\python.exe -m tools.evaluar --reusar    # solo parser, usa el OCR guardado
.\.venv\Scripts\python.exe -m tools.inspeccionar aguas_01 fechas.emision
.\.venv\Scripts\python.exe -m tools.experimento_zonas   # compara configuraciones de zonas
.\.venv\Scripts\python.exe -m pytest -q tests
```

`tools.evaluar` separa `error_sin_aviso` (valor incorrecto presentado como
fiable, lo más grave) de `error_marcado` (incorrecto pero marcado como dudoso).
Cuidado: `--pases` con `--reusar` conserva las zonas planificadas con todas
las pasadas y puede sobrestimar; para decidir, ejecutar sin `--reusar` y con
`OCR_PASES`.
