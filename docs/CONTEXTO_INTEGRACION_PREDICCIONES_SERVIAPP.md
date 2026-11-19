# Contexto y contrato de integración de predicciones ServiApp

> Documento de transferencia para continuar el trabajo con Gemini u otro asistente. Resume la conversación, el código de práctica existente, el contrato Spring Boot–FastAPI, el error `422` observado en producción y la futura sustitución por el modelo LSTM oficial descrito en `Infraestructura de prueba e integración del modelo LSTM ServiApp_Spring Boot y FastAPI.md`.
>
> **Importante:** este documento describe lo que se implementó y lo que se observó en el repositorio durante noviembre de 2026. No afirma que los cambios compilen o ya estén desplegados en Railway. La compilación no se pudo ejecutar en el entorno de Codex por restricciones de Maven y no había Python disponible para validar FastAPI.

## 1. Objetivo del sistema

ServiApp tiene varios componentes:

- **Spring Boot** es el backend principal, la web de ServiApp, gestiona usuarios y recibos y es el único servicio que accede a MongoDB.
- **FastAPI** ya procesa fotos de recibos con OpenCV/OCR y ahora contiene una integración experimental con modelos de práctica de TensorFlow.
- **Flutter/Postman** consumen una API para pedir predicciones. A futuro Flutter debe poder sustituir Postman sin cambiar el contrato.
- El modelo experimental actual fue practicado en Colab, en `serviapp_preparacion_mongodb_practica.ipynb`.
- El modelo LSTM descrito por el compañero es el modelo objetivo/oficial futuro. Es un modelo distinto y su contrato de inferencia difiere del modelo de práctica.

El principio de datos acordado: **al solicitar una predicción, Spring entrega a FastAPI el perfil mínimo del usuario aplicable al servicio solicitado y los consumos históricos de los recibos de ese mismo servicio.** FastAPI no consulta MongoDB.

## 2. Flujo implementado para el modelo de práctica

```text
Postman o Flutter
    │ POST /api/predicciones/{servicio}/meses{3|5}
    │ body: { "usuarioId": "..." }
    ▼
FastAPI (producción o local)
    │ GET /api/internal/ml/historico-consumo
    │ X-Internal-Api-Key + usuarioId + servicio + meses
    ▼
Spring Boot (Railway) ─── MongoDB
    │ responde perfil mínimo + N meses ordenados ascendentemente
    ▼
FastAPI valida y transforma (ETL de inferencia)
    │ carga modelo/scalers .keras/.pkl
    ▼
FastAPI responde predicción a Postman/Flutter
```

En la implementación de práctica, **FastAPI inicia la llamada interna a Spring** cuando recibe la solicitud de predicción. La ruta interna de Spring es un endpoint de datos, no llama al modelo. La aplicación Flutter/Postman solo debe conocer la ruta pública de FastAPI. La arquitectura propuesta por el documento del LSTM puede decidir posteriormente que Spring sea quien orqueste y llame a FastAPI; eso requeriría agregar una ruta pública/orquestadora de predicción en Spring y modificar la integración HTTP, no es el flujo que quedó codificado para la práctica.

## 3. Modelo de datos real de MongoDB

### 3.1 Usuario (`usuarios`)

El documento contiene datos generales, el documento embebido `predio` y un arreglo `servicios`. Campos relevantes:

```javascript
{
  _id: ObjectId("691e5a001d24e96b32580473"),
  predio: {
    estrato: 1,
    tipo_predio: "CASA"
  },
  servicios: [
    {
      tipo_servicio: "Energía",
      empresa: "Afinia",
      poliza: NumberLong("4116752"),
      habitantes_hogar: NumberLong("2")
    },
    {
      tipo_servicio: "Agua",
      empresa: "Aguas de Cartagena",
      poliza: NumberLong("25853646"),
      habitantes_hogar: NumberLong("2")
    }
  ]
}
```

En Java, `UsuarioModel` mapea `predio` como `PredioModel` y `servicios` como `List<ServicioModel>`. `ServicioModel` mapea el campo Mongo `habitantes_hogar` en su propiedad Java `habitantes`, y `poliza` como número `long`. Los nombres de servicios guardados en el perfil están en español con mayúsculas/minúsculas y pueden incluir tilde (`Energía`); deben normalizarse para compararse con `ENERGIA`.

### 3.2 Recibo (`recibos`)

Ejemplo real simplificado:

```javascript
{
  _id: ObjectId("6adbd78a34735c14b1ed4ff7"),
  usuario_id: "691e5a001d24e96b32580473",
  tipo_servicio: "ENERGIA",
  identificador: { tipo: "NIC", valor: "4116752" },
  periodo_facturado: "2026-10",
  unidad_consumo: "kWh",
  consumo_actual: 178,
  historial_consumos: [
    { periodo: "2026-05", consumo: 197, esActual: false },
    { periodo: "2026-06", consumo: 191, esActual: false },
    { periodo: "2026-07", consumo: 181, esActual: false },
    { periodo: "2026-08", consumo: 229, esActual: false },
    { periodo: "2026-09", consumo: 188, esActual: false },
    { periodo: "2026-10", consumo: 178, esActual: true }
  ]
}
```

Spring `ReciboModel` mapea estos nombres Mongo mediante `@Field`: `usuario_id`, `tipo_servicio`, `identificador`, `periodo_facturado`, `unidad_consumo`, `consumo_actual` e `historial_consumos`. `historial_consumos` contiene muestras mensuales leídas de la gráfica de la factura; una misma factura puede aportar varios meses. Por eso **número de documentos de recibos y número de meses de consumo no son lo mismo**.

La factura relevante debe pertenecer al `usuario_id` y al servicio solicitado. Además, para evitar mezclar cuentas del mismo tipo, la póliza/NIC/contrato del último recibo se compara con `servicios[].poliza` del usuario.

## 4. Contrato interno vigente de Spring Boot

### Solicitud de FastAPI a Spring

```http
GET {SPRING_BOOT_URL}/api/internal/ml/historico-consumo?usuarioId={id}&servicio={AGUA|ENERGIA}&meses={3|5}
X-Internal-Api-Key: {ML_INTERNAL_API_KEY}
```

Ejemplo:

```http
GET https://serviapp.up.railway.app/api/internal/ml/historico-consumo?usuarioId=691e5a001d24e96b32580473&servicio=ENERGIA&meses=3
X-Internal-Api-Key: <secreto>
```

La clave se compara en Spring con `MessageDigest.isEqual`. Si `ML_INTERNAL_API_KEY` no está configurada en Spring, la ruta responde `503`; si la cabecera falta o no coincide, responde `401`. Esta ruta tiene una cadena de Spring Security propia, sin sesión y sin login, porque su autenticación se hace en el controlador mediante la clave interna.

### Respuesta exitosa esperada

```json
{
  "usuarioId": "691e5a001d24e96b32580473",
  "servicio": "ENERGIA",
  "cantidadMesesSolicitados": 3,
  "datosUsuario": {
    "estrato": 1,
    "tipoPredio": "CASA",
    "habitantes": 2
  },
  "historialRecibos": [
    { "reciboId": "...", "periodo": "2026-08", "consumo": 229.0, "unidad": "kWh" },
    { "reciboId": "...", "periodo": "2026-09", "consumo": 188.0, "unidad": "kWh" },
    { "reciboId": "6adbd78a34735c14b1ed4ff7", "periodo": "2026-10", "consumo": 178.0, "unidad": "kWh" }
  ]
}
```

Reglas del contrato:

1. `servicio` solo acepta `AGUA` o `ENERGIA` en esta integración, aunque Mongo tenga recibos `GAS`. No hay modelo de gas implementado.
2. `meses` solo acepta `3` o `5`.
3. `datosUsuario.estrato` y `tipoPredio` vienen de `usuario.predio`; `habitantes` viene del elemento correspondiente de `usuario.servicios`.
4. El perfil y los recibos son del `usuarioId` solicitado; no se envían nombre, correo, contraseña, dirección ni objetos Mongo completos.
5. Los recibos consultados se filtran por usuario y tipo (`usuario_id`, `tipo_servicio`). Después se asocia el servicio embebido por tipo y, preferentemente, por coincidencia de identificador/póliza.
6. Spring consolida `historial_consumos` y el dato facturado (`periodo_facturado`, `consumo_actual`) por mes. Los periodos duplicados se deduplican y el valor facturado prevalece sobre la lectura de gráfica del mismo mes.
7. Deben enviarse los últimos N meses en orden cronológico ascendente: antiguo primero, reciente al final. FastAPI presupone ese orden.
8. La respuesta contiene solo los N meses pedidos, no todos los meses que el recibo pudiera incluir.

### Errores previstos

| HTTP | Significado |
|---|---|
| `400` | `servicio` inválido o `meses` distinto de 3/5. |
| `401` | Falta o no coincide `X-Internal-Api-Key`. |
| `404` | `usuarioId` no existe. |
| `422` | No hay recibos del servicio, no se puede asociar un servicio del usuario, faltan datos del hogar o no hay N meses históricos. |
| `503` | Spring no tiene `ML_INTERNAL_API_KEY` configurada; ruta deshabilitada. |
| `500` | Error interno real; el stack trace debe quedar en los logs de Spring. |

La página HTML `error/500` de Thymeleaf es para la web. `MlHistoricoController` fue añadido al `ReciboApiErrores` (`@RestControllerAdvice`) para que las excepciones de esta ruta se devuelvan como JSON y no se conviertan silenciosamente en una página HTML. Ese cambio debe estar desplegado en Spring para que los errores sean legibles por FastAPI.

## 5. Error `422` observado y corrección

Durante las pruebas en Railway, el log de FastAPI mostró solicitudes como:

```text
GET https://serviapp.up.railway.app/api/internal/ml/historico-consumo?...&servicio=AGUA&meses=3
HTTP/1.1 422 Unprocessable Entity
```

FastAPI sí estaba contactando Spring. El problema en la primera versión del controlador Spring fue validar `recibosServicio.size() < meses`: eso exigía tres documentos Mongo distintos para solicitar tres meses. El recibo real de ejemplo tenía un solo documento con seis meses en `historial_consumos` y `consumo_actual`.

La implementación actual debe contar **periodos mensuales consolidados**, no documentos de recibo. Antes de publicar, comprobar que la versión desplegada de Spring incluye esa lógica. Si vuelve un `422`, inspeccionar el cuerpo JSON de FastAPI/Spring: debe incluir el motivo, por ejemplo `Historial mensual insuficiente: disponibles X de 3 meses`. Comprobar también:

- el campo `usuario_id` en los recibos coincide exactamente con el ID recibido (string); `_id` del usuario es ObjectId pero se pasa como texto;
- `tipo_servicio` está en el valor esperado (`AGUA` o `ENERGIA`);
- `historial_consumos` quedó mapeado como `List<ConsumoMensual>` y cada item tiene `periodo` y `consumo`;
- hay coincidencia de `identificador.valor` del recibo con `poliza` del servicio embebido;
- el servicio del perfil tiene `habitantes_hogar > 0` y el predio tiene `tipo_predio`.

En el ejemplo real, el usuario `691e5a001d24e96b32580473` tiene recibo de energía con identificador `4116752`, que coincide con su servicio de Energía, estrato 1, casa y 2 habitantes. El recibo incluye seis meses (mayo–octubre de 2026), por tanto Spring puede devolver los últimos tres (agosto–octubre) o cinco (junio–octubre) para energía si ese documento está en la base consultada por producción. El ejemplo no prueba por sí solo la existencia de recibos de agua para ese usuario; se deben revisar en Mongo los recibos cuyo `usuario_id` y `tipo_servicio` coinciden.

## 6. API pública actual de FastAPI (modelo de práctica)

Las rutas son **POST**, no GET. El cuerpo contiene solo el ID del usuario; FastAPI obtiene de Spring el perfil y los recibos.

```http
POST https://fastapiserviapp-production.up.railway.app/api/predicciones/energia/meses3
Content-Type: application/json

{ "usuarioId": "691e5a001d24e96b32580473" }
```

También están definidas:

- `POST /api/predicciones/energia/meses5`
- `POST /api/predicciones/agua/meses3`
- `POST /api/predicciones/agua/meses5`

`GET` en estas rutas da `405 Method Not Allowed`, como apareció en los logs compartidos. `GET /health` sirve para verificar disponibilidad; `/docs` muestra OpenAPI.

Respuesta exitosa de práctica, aproximada:

```json
{
  "usuarioId": "691e5a001d24e96b32580473",
  "servicio": "ENERGIA",
  "ventanaMesesSolicitada": 3,
  "ventanaUsadaPorModelo": 3,
  "prediccionSiguienteMes": {
    "periodoEstimado": "2026-11",
    "consumoEstimado": 180.2,
    "unidad": "kWh"
  },
  "historicoAnalizado": [
    { "periodo": "2026-08", "consumo": 229.0 },
    { "periodo": "2026-09", "consumo": 188.0 },
    { "periodo": "2026-10", "consumo": 178.0 }
  ],
  "metadatosModelo": {
    "versionModelo": "energia_tensorflow_v1",
    "algoritmo": "TensorFlow Sequential"
  }
}
```

El valor de predicción anterior es ilustrativo, no un resultado de la base real.

## 7. Modelo experimental de práctica (Colab)

Fuente: `serviapp_preparacion_mongodb_practica.ipynb`, especialmente celdas de entrenamiento, simulación y exportación.

### Variables de entrada y entrenamiento

El notebook entrena modelos **Dense/Sequential**, uno para agua y otro para energía. No es el LSTM oficial. La lista de features es:

```python
FEATURES_COLS = [
    "estrato", "habitantes_hogar", "es_casa",
    "mes_sin", "mes_cos", "mes_t_3", "mes_t_2", "mes_t_1"
]
```

Hay ocho columnas y un target `consumo`. Cada servicio tiene un `StandardScaler` para X y otro para y. En inferencia se hace `scaler_x.transform`, `model.predict`, y `scaler_y.inverse_transform`; **nunca `fit` ni `fit_transform` en producción**.

### Cómo se construye la entrada

Para los últimos tres consumos mensuales cronológicos `[t-2, t-1, t]`, la entrada es:

```text
[estrato,
 habitantes,
 1 si CASA; 0 si otro tipo,
 sin(2π * mes_objetivo / 12),
 cos(2π * mes_objetivo / 12),
 consumo_t-2,
 consumo_t-1,
 consumo_t]
```

`mes_objetivo` es el mes siguiente al último periodo observado. Aunque el endpoint permita solicitar 5 meses para incluir más historia en el contrato/respuesta, el modelo de práctica utiliza los últimos 3, porque su matriz se entrenó con tres columnas de lags.

### Artefactos existentes

Fueron movidos desde la carpeta raíz `modelos_serviapp_prueba2` a `FastAPI_serviapp/app/ml_artifacts`:

```text
modelo_agua.keras
modelo_energia.keras
scaler_x_agua.pkl
scaler_x_energia.pkl
scaler_y_agua.pkl
scaler_y_energia.pkl
```

El notebook registra versiones de exportación `tensorflow==2.21.0` y `scikit-learn==1.6.1`; se añadieron a `FastAPI_serviapp/requirements.txt`, junto con `httpx` y `joblib`. FastAPI carga los modelos de forma perezosa y en caché. El Dockerfile existente copia `app/`, por lo que esos artefactos quedan en la imagen.

### Diferencia conceptual: `meses=3|5` vs. features

`meses` es una ventana que solicita el cliente para controlar la disponibilidad y cantidad de historia entregada. No selecciona un modelo 3m/5m. El modelo Dense actual siempre requiere 3 lags. En el futuro la semántica debe ajustarse a la definida por el modelo oficial (que declara cinco consumos exactos).

## 8. Integración futura del LSTM oficial del compañero

El documento `Infraestructura de prueba e integración del modelo LSTM ServiApp_Spring Boot y FastAPI.md` define un modelo único `ServiApp_LSTM` que recibe tres entradas:

1. Historial de **exactamente cinco** consumos, forma `(N, 5, 1)`; cronológico ascendente.
2. Contexto `[estrato, habitantes]`, forma `(N, 2)`.
3. Servicio codificado one-hot dentro de FastAPI: `AGUA -> [1,0,0]`, `ENERGIA -> [0,1,0]`, `GAS -> [0,0,1]`.

El modelo oficial y sus artefactos esperados en el documento del compañero son `modelo_lstm_mejor.keras`, `escaladores_consumo.joblib` y `escalador_contexto.joblib` (validar con el dueño del modelo los nombres/versiones exactos, estructura del joblib y firmas de entrada/salida antes de sustituirlos). FastAPI debe hacer inferencia únicamente: validación, transform, one-hot, construcción de tensores, `predict`, inverse transform y respuesta. No debe entrenar ni recalcular scalers en producción.

### Spring: qué preservar/cambiar para LSTM

El contrato actual Spring ya entrega las cuatro piezas base necesarias: servicio, historial con periodos, estrato y habitantes. Para el LSTM oficial:

- exigir que el endpoint devuelva **exactamente los últimos 5 meses**; no aceptar 3 para inferencia oficial;
- mantener orden ascendente y distinguir claramente periodos/consumos;
- seguir consolidando desde `historial_consumos` más valores facturados, deduplicando por periodo y dando prioridad a `FACTURADO` frente a `GRAFICA` si se agrega el campo `fuente`;
- continuar resolviendo el servicio de hogar correcto por tipo e identificador/póliza;
- no calcular one-hot, escaladores ni tensores en Java;
- no enviar documento Usuario/Recibo completo ni campos sensibles;
- si solo se puede formar menos de cinco meses, responder JSON `422` indicando disponibles/requeridos.

El contrato interno `GET ...?meses=5` puede permanecer y FastAPI solicita 5 al modelo oficial, o se puede fijar siempre a cinco y retirar el parámetro. Evitar que se mezclen ambas semánticas sin documentación.

### FastAPI: qué cambiar para LSTM

- Sustituir la carga de dos modelos Dense (`modelo_agua.keras`, `modelo_energia.keras`) por el modelo LSTM único oficial y sus escaladores.
- Cambiar el preprocesador de ocho features Dense a las tres entradas/tensores del LSTM descritos por el dueño del modelo.
- Exigir cinco consumos; devolver `422` si la respuesta Spring no contiene exactamente cinco periodos utilizables.
- Aplicar scaler de consumo/contexto según el pipeline real de entrenamiento. El documento menciona escaladores por servicio y de contexto; confirmar API y parámetros exactos en los artefactos/notebook oficial, y nunca inferirlos a partir del modelo de práctica.
- Convertir servicio a one-hot en FastAPI.
- Invertir la predicción al dominio real con el escalador correcto y responder con consumo/unidad/periodo objetivo según el contrato acordado.
- Mantener la ruta pública si se desea compatibilidad, pero decidir si se conserva `/meses5` o si la ruta oficial unificada será `/api/predicciones/{servicio}`. `meses3` no debería aparentar funcionar con un modelo que necesita cinco valores.
- No borrar o sobrescribir artefactos de práctica hasta verificar la predicción oficial.

### Diferencia importante de orquestación

El documento oficial describe un flujo donde Spring consulta Mongo, construye solicitud para FastAPI, recibe el resultado y lo devuelve a Flutter/web. La prueba actual se inició desde Postman hacia FastAPI, y FastAPI solicita los datos a Spring. Antes de migrar, acordar con el compañero cuál flujo será el definitivo:

**A. Cliente → Spring → FastAPI → Spring → cliente (documento del compañero):** recomendado para que Flutter solo hable con Spring y la clave interna Spring–FastAPI no se exponga.

**B. Cliente/Postman → FastAPI → Spring (implementación experimental actual):** útil para pruebas directas; obliga a proteger FastAPI públicamente y Spring recibe la clave interna únicamente desde FastAPI.

No cambiar la dirección de llamadas como efecto secundario de sustituir el modelo. Es un cambio de integración aparte.

## 9. Variables de entorno / despliegue Railway

La misma clave secreta debe configurarse en ambos servicios para el flujo experimental actual:

```env
# Railway Spring Boot
ML_INTERNAL_API_KEY=<secreto aleatorio largo>

# Railway FastAPI
SPRING_BOOT_URL=https://serviapp.up.railway.app
ML_INTERNAL_API_KEY=<el mismo secreto>
SPRING_TIMEOUT_SECONDS=15
```

FastAPI local para probar contra Spring de producción:

```env
SPRING_BOOT_URL=https://serviapp.up.railway.app
ML_INTERNAL_API_KEY=<el mismo secreto configurado en Spring>
```

No incluir el secreto en el repositorio, en Postman, en el cuerpo de solicitud ni en una captura/log. La variable `SERVIAPP_API_KEY` es distinta: protege actualmente las rutas de OCR (`/recibos`); no reemplaza `ML_INTERNAL_API_KEY`.

Secuencia al publicar un cambio del contrato Spring:

1. Compilar Spring localmente (`mvnw.cmd -DskipTests compile` o Maven Wrapper equivalente).
2. Publicar/fusionar rama y esperar despliegue Railway de Spring.
3. Revisar logs de Spring, validar respuesta JSON directa del endpoint interno desde una instancia con la clave, y comprobar cantidad/orden del historial.
4. Probar la ruta POST de FastAPI con usuario real que tenga cinco meses disponibles.
5. Revisar logs de FastAPI y Spring para distinguir error de HTTP, validación del contrato y error de carga/predicción del modelo.

Durante el trabajo anterior, la compilación no se pudo realizar desde Codex porque Maven intentó crear su caché en `C:\.m2\repository` y recibió acceso denegado; no se confirmó la compilación ni el despliegue de los cambios desde ese entorno.

## 10. Pruebas manuales con Postman

Solicitud a FastAPI:

```http
POST https://fastapiserviapp-production.up.railway.app/api/predicciones/energia/meses3
Content-Type: application/json

{
  "usuarioId": "691e5a001d24e96b32580473"
}
```

Para el modelo de práctica: elegir 3 o 5 meses; el usuario debe tener esa cantidad de periodos consolidados. Cambiar `energia` por `agua` para el otro modelo. Solo `energia` y `agua` son compatibles hoy.

Diagnóstico por status:

- `200`: predicción calculada.
- `405`: se usó GET en una ruta que requiere POST.
- `401`: clave FastAPI/credenciales internas configuradas de forma incorrecta (según el endpoint que responda).
- `404`: usuario inexistente o ruta mal escrita.
- `422`: revisar el JSON de detalle; suele indicar historial mensual insuficiente, servicio/póliza sin asociación o datos faltantes.
- `503`: FastAPI no puede llegar a Spring, falta clave o falta TensorFlow/artefacto.
- `500`: inspeccionar stack trace de Spring y FastAPI; el log de Railway Network (requestId, ruta, status, duración) prueba el tránsito, pero no expone la excepción Java.

Para verificar recibos de un usuario en MongoDB Compass, colección `recibos`, filtro:

```json
{ "usuario_id": "691e5a001d24e96b32580473", "tipo_servicio": "ENERGIA" }
```

Los nombres de campo en la colección son `snake_case`, por ejemplo `usuario_id`, `tipo_servicio`, `periodo_facturado`, `consumo_actual`, `historial_consumos`.

## 11. Archivos relevantes del repositorio

### Spring Boot

- `Spring.ClassroomProject-SERVIAPP/src/main/java/com/example/ServiApp/controller/entities/MlHistoricoController.java`: endpoint interno y ensamblaje del historial.
- `.../repository/ReciboRepository.java`: consultas de recibos por usuario/servicio, incluyendo overload paginado.
- `.../controller/security/ReciboApiErrores.java`: respuesta JSON para errores del controlador interno y logging de errores inesperados.
- `.../config/SecurityConfiguration.java`: cadena stateless y orden de seguridad para `/api/internal/ml/**`.
- `.../resources/application.properties`: propiedad `serviapp.ml.api-key=${ML_INTERNAL_API_KEY:}`.
- `.../model/UsuarioModel.java`, `ServicioModel.java`, `PredioModel.java`, `ReciboModel.java`: mapeos Mongo.
- `.../services/ServicioHogarService.java`: normalización/selección de servicio y lógica de identificador existente.

### FastAPI

- `FastAPI_serviapp/app/api/routes/predicciones.py`: rutas POST, cliente de Spring, schemas, preprocesamiento e inferencia Dense de práctica.
- `FastAPI_serviapp/app/core/config.py`: URL, clave y timeout Spring.
- `FastAPI_serviapp/app/main.py`: registro del router.
- `FastAPI_serviapp/requirements.txt`: TensorFlow, scikit-learn, joblib, httpx y OCR.
- `FastAPI_serviapp/.env.example`: variables para desarrollo.
- `FastAPI_serviapp/app/ml_artifacts/`: artefactos Dense de práctica.
- `FastAPI_serviapp/docs/CONTRATO_JSON.md`: contrato anterior de OCR/minería; tiene endpoints `/api/mineria/**` de otra finalidad y otra clave (`MINERIA_API_KEY`). No confundirlo con el endpoint nuevo de predicción por usuario.
- `serviapp_preparacion_mongodb_practica.ipynb`: pipeline/modelos Dense experimentales.
- `Infraestructura de prueba e integración del modelo LSTM ServiApp_Spring Boot y FastAPI.md`: especificación del modelo oficial del compañero.

## 12. Tareas recomendadas para quien continúe

1. Revisar `MlHistoricoController` contra los mapeos reales y desplegar Spring; confirmar que la respuesta del usuario `691e5a001d24e96b32580473` contiene los últimos tres consumos correctos de ENERGIA y/o AGUA.
2. Si vuelve `422`, usar el mensaje JSON y logs de Spring; validar `findByUsuarioIdAndTipoServicioOrderByPeriodoFacturadoDesc`, normalización de servicio, `identificador.valor`/`poliza` y mapeo de `historial_consumos`.
3. Añadir una prueba de integración/unitaria para consolidación y deduplicación de periodos, incluyendo prioridad del valor facturado, asociación a póliza y listas cortas. (No consta que esas pruebas se hayan escrito/ejecutado.)
4. Validar instalación TensorFlow/artefactos en el runtime real de Railway y que el JSON exitoso pase Pydantic y produzca una inferencia.
5. Cuando se integre el LSTM, pedir/verificar artefactos y pipeline oficiales y reemplazar las features de práctica según sección 8; no reutilizar los scalers Dense.
6. Acordar arquitectura de orquestación cliente→Spring→FastAPI versus cliente→FastAPI→Spring antes de conectar Flutter.
7. Actualizar documentación pública/API para que describa un único flujo vigente y evitar que la ruta vieja `/api/mineria/series` se confunda con predicción por usuario.

## 13. Límites de lo que está confirmado

- Los logs compartidos sí confirmaron que FastAPI en Railway pudo hacer la petición HTTP a Spring y recibió `500`, luego `422`; no confirmaron una predicción `200`.
- El ejemplo Mongo confirma que un recibo de energía puede incluir seis meses de historia y que `usuario_id` coincide con el usuario dado. No confirma qué recibos de agua están guardados para ese usuario.
- La implementación de consolidar `historial_consumos` se aplicó en el workspace, pero no se pudo compilar/desplegar desde este entorno. Verificar que está presente en el commit y Railway ejecuta esa versión.
- El modelo de práctica y los artefactos existen en el workspace. No se ejecutó una inferencia de producción verificada en esta sesión.
- El documento del compañero describe el LSTM oficial, pero no prueba por sí solo que sus artefactos entrenados estén presentes ni que el pipeline escrito coincida exactamente con el código de entrenamiento. Confirmar shapes, orden, escaladores, one-hot y versiones junto con el dueño del modelo.

