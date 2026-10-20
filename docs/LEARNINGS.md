# Lecciones aprendidas: whatsapp-agent-kit

### 2026-09-29: whatsmeow necesita el directPath con la query firmada

- **Problema**: `/api/download` devolvía 403 en cada descarga de media, incluso
  en mensajes recién llegados.
- **Causa**: `extractDirectPathFromURL` en `bridge/main.go` quitaba los params
  `?ccb&oh&oe`, pero whatsmeow arma `https://<host><directPath>&hash=...` y
  espera la firma dentro del directPath.
- **Arreglo**: conservar la query string en el directPath.
- **Lección**: la descarga de media nunca había funcionado desde que salió la
  feature. Un health check que ejercite una descarga real lo habría cachado.

### 2026-09-29: el bridge guarda timestamps como TEXT, no epoch

- **Problema**: los filtros de rango `timestamp >= <epoch>` devolvían todas las
  filas.
- **Causa**: el bridge en Go escribe `time.Time` como TEXT
  (`2026-09-29 10:57:41-06:00`); en SQLite, TEXT > INTEGER siempre.
- **Arreglo**: comparar lexicográficamente el prefijo de fecha ISO
  (`timestamp >= 'YYYY-MM-DD'`).
- **Lección**: revisar la afinidad de columna del schema del bridge antes de
  escribir predicados de rango. Las comparaciones por epoch solo sirven en las
  columnas epoch propias de `rag_media`.

### 2026-09-30: qwen3-vl piensa de todos modos, presupuestar el thinking

- **Problema**: los captions de visión devolvían `""` o parecían colgados aun
  con `think:false` y `/no_think`.
- **Causa**: ollama <=0.35 ignora ambas flags para qwen3-vl; el modelo siempre
  emite ~200 tokens de thinking antes de responder, y `num_predict` cuenta el
  thinking.
- **Arreglo**: `num_predict: 600` en el payload del caption. Una respuesta
  truncada al thinking sale vacía, el registro se salta y la siguiente pasada
  de extract lo reintenta.
- **Lección**: con modelos de visión en ollama no confiar en flags que
  desactivan la respuesta. Medir `eval_count` contra `thinking` y dimensionar
  `num_predict` para think + respuesta.

### 2026-09-30: un solo slot en llama-server + request zombie = stall silencioso

- **Problema**: tras reiniciar un cliente atascado, cada caption posterior se
  quedaba colgado con conexión abierta mientras `llama-server` seguía
  inactivo (<1% de CPU) en `encoding mtmd batch`.
- **Causa**: el request abandonado seguía ocupando el único slot de inferencia
  (n_slots=1); el runner lo espera en vez de procesar la cola.
- **Arreglo**: matar el proceso cliente que tenía la conexión ESTABLISHED; el
  encoder se desbloquea de inmediato (~188 ms por imagen).
- **Lección**: cuando un runner de ollama se queda quieto con trabajo en cola,
  revisar `lsof -p <pid>` por un cliente muerto ocupando el slot antes de
  reiniciar nada.

### 2026-09-30: la RX 6700 XT necesita Vulkan (o ROCm con paciencia) en ollama

- **Problema**: ollama en el servidor corría todos los modelos en CPU:
  "offloaded 0/13 layers to GPU".
- **Causa**: el discovery de GPU por ROCm se cuelga ~56 s en gfx1031 (SKU no
  soportado) y expira, así que la tarjeta nunca se registra.
- **Arreglo**: `ollama.service` a nivel usuario con `OLLAMA_VULKAN=1` y
  `OLLAMA_LLM_LIBRARY=vulkan`: RADV ve la tarjeta en 0.6 s. Tras completar el
  primer cache JIT, el discovery de ROCm también empezó a funcionar; hoy sirve
  cualquiera de los dos backends.
- **Lección**: en RDNA2 no soportado, Vulkan es el camino determinista. Un log
  de "model loads fine" no significa que la GPU esté en uso: revisar
  `PROCESSOR` en `ollama ps`.

### 2026-09-30: los screenshots largos atascan los encoders de visión

- **Problema**: un solo screenshot de WhatsApp de ~20k px de alto congeló el
  captioning por más de 10 min.
- **Arreglo**: bajar a <=1568 px el lado largo (`sips -Z 1568` en Mac, scale de
  ffmpeg en otra parte) a un archivo temporal antes del base64.
- **Lección**: acotar siempre las dimensiones de la imagen antes de mandarla a
  un modelo de visión. El costo del encoder escala con píxeles, no con el
  tamaño del archivo.

### 2026-10-10: deps sin cota en libs jóvenes

- **Problema**: `pip install "mcp[cli]>=1.6.0"` resolvió a mcp 2.x en CI;
  FastMCP se renombró a MCPServer y el import tronó.
- **Causa**: `>=` sin cota superior en una lib pre-1.0 de hecho.
- **Arreglo**: `mcp[cli]>=1.6.0,<2` en pyproject, workflow y CONTRIBUTING.
- **Lección**: los deps `>=` sueltos son bombas. Pin a `<major+1`.
