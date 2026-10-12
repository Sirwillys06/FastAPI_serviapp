# Contrato JSON de recibos escaneados

Documento para quien construye el **modelo de minería / predicción de consumo**.
Explica qué datos produce el escáner de recibos, dónde quedan guardados y cómo obtenerlos.

## 1. ¿De dónde salen los datos?

1. El usuario toma la foto en la app → **FastAPI** (`POST /recibos/analizar`) la procesa con
   OpenCV + OCR y devuelve el JSON de la sección 2.
2. El usuario **revisa y corrige** los datos en la app y los confirma.
3. **Spring Boot** guarda el recibo confirmado en MongoDB Atlas, colección **`recibos`**.

Los datos que llegan a la base de datos ya fueron validados por el usuario, por eso son la fuente
recomendada para el modelo (no la respuesta cruda de FastAPI).

## 2. Recibo (formato común)

```json
{
  "tipoServicio": "ENERGIA",              // ENERGIA | AGUA | GAS
  "empresa": "AFINIA",                    // AFINIA | AGUAS_DE_CARTAGENA | SURTIGAS
  "identificador": { "tipo": "NIC", "valor": "1146251" },  // NIC | POLIZA | CONTRATO
  "numeroFactura": "22402609004100",
  "titular": "NOMBRE DEL TITULAR",
  "direccion": "CL 31A 39-15",
  "barrio": "ALCIBIA",
  "estrato": 1,                           // 1..6 o null (comercial / no impreso)
  "periodoFacturado": "2026-09",          // mes facturado, YYYY-MM
  "periodoInicio": "2026-08-10",          // fechas ISO (YYYY-MM-DD) o null
  "periodoFin": "2026-09-08",
  "fechaEmision": "2026-09-10",
  "fechaLimitePago": "2026-09-17",
  "fechaSuspension": "2026-09-18",        // fecha de corte
  "valorTotal": 427630,                   // pesos (entero)
  "unidadConsumo": "kWh",                 // kWh (energía) | m3 (agua y gas)
  "consumoActual": 502.0,                 // consumo del mes facturado
  "lecturaAnterior": null,
  "lecturaActual": null,
  "diasFacturados": 29,
  "historialConsumos": [                  // del más antiguo al actual
    { "periodo": "2026-03", "consumo": 453.0, "esActual": false },
    { "periodo": "2026-04", "consumo": 444.0, "esActual": false },
    { "periodo": "2026-05", "consumo": 488.0, "esActual": false },
    { "periodo": "2026-06", "consumo": 531.0, "esActual": false },
    { "periodo": "2026-07", "consumo": 466.0, "esActual": false },
    { "periodo": "2026-08", "consumo": 490.0, "esActual": false },
    { "periodo": "2026-09", "consumo": 502.0, "esActual": true }
  ],
  "conceptos": [                          // desglose del total cuando el recibo lo trae
    { "concepto": "Aseo", "valor": 196688 }
  ]
}
```

### Qué trae cada empresa

| Campo | Afinia (energía) | Aguas de Cartagena (agua) | Surtigas (gas) |
|---|---|---|---|
| Historial | 6 meses + actual | 6 meses + actual | **Solo el mes actual** |
| Lecturas del medidor | No | Sí | Sí |
| Conceptos | — | Aseo / Acueducto y alcantarillado | Gas / Brilla / Otros / Meses anteriores |

> En Surtigas el `valorTotal` puede incluir cuotas de **Brilla** (créditos), que no dependen del
> consumo. Para modelar el gasto del servicio usa el concepto "Gas y servicios inherentes".

### Campos extra guardados en MongoDB

| Campo | Descripción |
|---|---|
| `usuarioId` | Usuario de ServiApp dueño del recibo |
| `fechaEscaneo` | Momento en que se guardó |
| `confianzaOcr` | Confianza 0..1 del OCR por campo (antes de la revisión del usuario) |
| `camposCorregidos` | Campos que el usuario cambió a mano. Si un campo está aquí, su valor es el del usuario |
| `versionExtractor` | Versión del extractor de FastAPI |

En MongoDB los nombres de campo están en `snake_case` (`periodo_facturado`, `historial_consumos`, …);
la API de Spring los entrega en `camelCase` como en este documento.

## 3. API para minería (Spring Boot)

Protegida con la cabecera `X-API-KEY`. El valor se define en la variable de entorno
`MINERIA_API_KEY` del despliegue de Spring (Railway). Sin esa variable el endpoint responde 503.

### `GET /api/mineria/series?tipoServicio=ENERGIA`

**Lo que necesita el modelo de predicción.** Une todos los recibos de un mismo servicio
(usuario + empresa + identificador) en una sola serie mensual sin meses repetidos:

```json
[
  {
    "usuarioId": "665f…",
    "tipoServicio": "ENERGIA",
    "empresa": "AFINIA",
    "tipoIdentificador": "NIC",
    "identificador": "1146251",
    "estrato": 1,
    "barrio": "ALCIBIA",
    "unidadConsumo": "kWh",
    "recibosEscaneados": 2,
    "totalMeses": 8,
    "ultimoPeriodo": "2026-10",
    "serie": [
      { "periodo": "2026-03", "consumo": 453.0, "fuente": "GRAFICA",   "valorTotal": null,   "diasFacturados": null, "reciboId": "…" },
      { "periodo": "2026-09", "consumo": 502.0, "fuente": "FACTURADO", "valorTotal": 427630, "diasFacturados": 29,   "reciboId": "…" }
    ]
  }
]
```

- `fuente = FACTURADO`: consumo del mes facturado de un recibo (dato más confiable).
- `fuente = GRAFICA`: leído de la gráfica histórica impresa en un recibo posterior.
- Si hay varios valores para un mes: gana `FACTURADO`; entre gráficas, el recibo más reciente.
- `tipoServicio` es opcional (`ENERGIA`, `AGUA`, `GAS`); sin él devuelve todas las series.

### `GET /api/mineria/recibos?tipoServicio=AGUA`

Todos los recibos completos (formato de la sección 2 + campos extra), para análisis exploratorio.

## 4. Ejemplo en Python

```python
import pandas as pd
import requests

BASE = "https://springclassroomproject-serviapp-development.up.railway.app"
HEADERS = {"X-API-KEY": "<MINERIA_API_KEY>"}

series = requests.get(f"{BASE}/api/mineria/series", params={"tipoServicio": "ENERGIA"},
                      headers=HEADERS, timeout=30).json()

filas = [
    {
        "servicio": f"{s['empresa']}-{s['identificador']}",
        "estrato": s["estrato"],
        "periodo": pd.Period(p["periodo"], freq="M"),
        "consumo": p["consumo"],
        "fuente": p["fuente"],
    }
    for s in series
    for p in s["serie"]
]
df = pd.DataFrame(filas).sort_values(["servicio", "periodo"])

# Una columna por servicio, un renglón por mes (lista para modelos de series de tiempo)
tabla = df.pivot(index="periodo", columns="servicio", values="consumo")
print(tabla.tail())
```
