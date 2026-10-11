# wa-rag

Indice de recuperacion sobre el historial de WhatsApp y el servidor MCP acotado
que lo expone a agentes.

Lee `messages.db` del bridge whatsmeow en solo lectura y escribe un indice
aparte (`rag.db`): FTS5 mas embeddings de `nomic-embed-text` via Ollama en
loopback. El contenido no sale de la maquina durante la indexacion.

## Archivos

| Archivo | Que es |
| --- | --- |
| `rag.py` | Indexacion y busqueda hibrida (turn-group + FTS/vector). CLI: `sync`, `search` |
| `media_index.py` | Media -> texto buscable con modelos locales. CLI: `sync`, `search` |
| `main_work.py` | Servidor MCP acotado a grupos de trabajo. Un solo tool |
| `wa-work-mcp.sh` | Lanzador del anterior, con el alcance fijado |
| `check-wa-scope.sh` | Verifica que ningun consumidor lea un chat vetado |
| `redact.py` | Enmascara JIDs y telefonos conservando las citas |
| `test_rag.py` | 44 tests, stdlib, embedder falso, sin red ni datos reales |
| `test_media_index.py` | Tests de media_index, mismos fixtures, extractores inyectados |

## Dos perfiles

**Personal:** todos los chats, indice en `~/.local/share/personal-assistant/`.
Uso local unicamente.

**Trabajo:** solo grupos menos el denylist, indice en
`~/.local/share/wa-rag-work/`. Es el unico que se registra en agentes de
terceros, via `wa-work-mcp.sh`.

La separacion es fisica, no un filtro: los chats personales no estan en el
archivo que ven los agentes, asi que ningun bug de filtro los puede devolver.
`main_work.py` tampoco importa `whatsapp.py`, de modo que los tools que leen
`messages.db` directo no son alcanzables desde ahi.

## Chunking (turn-group)

Un doc del indice = turno de conversacion, no un mensaje suelto:
mismo chat, gap <= `WA_RAG_TURN_GAP_SECONDS` (default 300), max
`WA_RAG_TURN_MAX_MSGS` (12) y `WA_RAG_TURN_MAX_CHARS` (1500). Embed y FTS
ven el bloque (`sender: texto`); las citas expanden a `message_id` por
miembro. Cambiar estos knobs exige `sync --rebuild`.

## Media (imagen, audio, documento)

`media_index.py` convierte media en texto buscable y lo indexa en el MISMO
`rag.db`, en tablas aparte (`rag_media`, `rag_media_fts`):

| Modality | Extractor | Modelo |
| --- | --- | --- |
| image | caption | `qwen3-vl:8b` via Ollama (loopback) |
| audio | transcript | `faster-whisper` local (venv `.venv`) |
| document | text layer | `pdftotext` (poppler) |
| video | ninguno aun | skip en v1 |

Los embeddings salen del mismo `nomic-embed-text`, asi que una caption compite
con mensajes en el mismo espacio vectorial. No hay vector DB aparte: blobs en
SQLite alcanzan a esta escala (ver `docs/adr/0001`).

Guardas antes de indexar:

- El scope (`WA_RAG_CHATS`/`WA_RAG_DENY_FILE`) filtra en SQL **antes** del
  download: un chat vetado ni siquiera pide el archivo al bridge.
- `WA_MEDIA_MAX_AGE_DAYS` (14): las URLs de media de WhatsApp caducan; el
  historico viejo es churn inutil.
- `WA_MEDIA_MIN_FREE_GB` (5): cada media indexado materializa un archivo;
  frena antes de llenar el disco.
- Watermark por modelo (`rag_media_state`): reintentar no re-procesa; cambiar
  modelo o `EXTRACTOR_VERSION` crea una columna nueva sin tocar la vieja.

Corre via `wa-rag-sync.timer` cada 30 min, o a mano:

```bash
.venv/bin/python media_index.py sync --limit 20
.venv/bin/python media_index.py search "flyer del evento"
```

## Archivo historico (backup del telefono)

`archive_import.py` indexa media copiado directo del telefono (ADB pull o
equivalente), sin pasar por el bridge. Dos fases para que la extraccion pesada
corra donde viven los archivos:

```bash
# extract: donde estan los archivos (Mac o Pop), escribe JSONL resumible
python archive_import.py extract /ruta/media --kind voice-notes --out vn.jsonl
python archive_import.py extract /ruta/media --kind audio --out audio.jsonl
python archive_import.py extract /ruta/media --kind documents --out docs.jsonl
python archive_import.py extract /ruta/media --kind images --out images.jsonl

# ingest: en Pop, embebe y persiste en el mismo rag.db
.venv/bin/python archive_import.py ingest vn.jsonl
```

- `message_id` = `arch:<sha256>`: re-correr no duplica; identico contenido bajo
  otro nombre se dedupica en extract.
- Los registros caen en `rag_media` con `chat_jid = archive:<kind>` y el
  filename en la caption como provenance.
- `--kind images` baja la imagen a <=1568 px antes del caption (screenshots
  largos y fotos de muchos MP atascan el encoder visual).
- `qwen3-vl` piensa ~200 tokens antes de responder aunque `think:false` se
  ignore en ollama <=0.35; `num_predict` va en 600 para que la respuesta salga.
  Si se agota el budget la respuesta queda vacia, no se escribe el registro y
  se reintenta en la proxima pasada.
- `WA_MEDIA_VISION_URL` permite apuntar el extract a otro Ollama (p. ej. un
  tunel ssh a Pop para usar su GPU desde la Mac).

Video sigue fuera del pipeline.

## Alcance

```
WA_RAG_CHATS=groups        # vacio = todos; "groups" = solo @g.us; o lista de JIDs
WA_RAG_DENY_FILE=~/.config/wa-scope/never-index.jids
```

El denylist gana sobre cualquier allowlist. Ver `never-index.jids.example`; el
archivo real no va a git.

Cambiar el alcance obliga a `sync --rebuild`: la firma queda guardada en
`rag_meta` y el sync se niega a continuar sobre un indice que ya no cubre lo que
dice cubrir.

## Correr

```bash
python3 -m unittest test_rag -q

WA_RAG_SOURCE_DB=/ruta/messages.db WA_RAG_INDEX_DB=/ruta/rag.db \
WA_RAG_CHATS=groups WA_RAG_DENY_FILE=~/.config/wa-scope/never-index.jids \
python3 rag.py sync
```

Requiere Ollama en loopback con `nomic-embed-text`. `rag.py` rechaza cualquier
endpoint de embeddings que no resuelva a loopback.

## Nota de despliegue

El bridge vivo corre en el servidor desde `~/projects/whatsapp-agent-kit/bridge`.
Los consumidores leen el symlink estable `~/.local/share/wa/messages.db`, no el
store directo. Apuntar `WA_RAG_SOURCE_DB` a una copia congelada no falla:
indexa mensajes viejos y las consultas salen stale sin un solo error en el log.
