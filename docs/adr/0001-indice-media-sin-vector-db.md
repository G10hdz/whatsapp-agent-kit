# ADR 0001: Indice de media en SQLite, sin vector DB aparte

## Estado

Aceptado — 2026-09-29

## Contexto

El historial de WhatsApp incluye imagenes, audios y documentos cuyo contenido
no es texto. Hace falta buscarlos por significado (ej. "el flyer del evento",
"el audio donde me piden X"). La pregunta abierta era si eso requeria una base
vectorial dedicada (Qdrant, Weaviate, pgvector sobre Postgres, sqlite-vec).

Hechos del deployment:

- `messages.db` tiene ~30k mensajes; la fraccion media es pequena (cientos, no
  millones).
- `rag.db` ya guarda embeddings como blobs y hace ranking hibrido FTS5+vector
  en Python puro, sin latencia perceptible a esta escala.
- Todo el pipeline es local-only por diseno; agregar un servicio mas suma un
  proceso que mantener, backups que coordinar y una superficie extra de
  fallo/drift.
- Disco en el servidor: ~50 GB libres, 91% usado. Cada media indexado materializa el
  archivo.

## Decision

Extender el esquema derivado existente, no introducir una base vectorial.

1. `rag.db` gana `rag_media` + `rag_media_fts` + `rag_media_state`: un doc por
   mensaje con media, con `modality`, `file_path`, `caption`, `status`,
   `model` y `embedding` (mismo espacio 768-dim de `nomic-embed-text`).
2. Media -> texto buscable con modelos locales: `qwen3-vl:8b` captiona
   imagenes, `faster-whisper` transcribe audios, `pdftotext` extrae texto de
   documentos. El texto derivado es lo que se indexa y se busca.
3. Downloads via `/api/download` del bridge, bajo el mismo allowlist/denylist
   aplicado en SQL antes del fetch. Ventana de `WA_MEDIA_MAX_AGE_DAYS` (14) y
   piso de `WA_MEDIA_MIN_FREE_GB` (5).
4. Si la escala alguna vez rompe el scan lineal de blobs, la frontera ya esta
   dibujada: cambiar solo la lectura de embeddings a `sqlite-vec` es un swap
   de modulo, no una migracion de arquitectura.

## Consecuencias

- Busqueda por significado sobre imagenes, audios y documentos sin servicios
  nuevos ni datos saliendo de la maquina.
- Caption/transcripcion cuestan CPU local en indexacion; la busqueda sigue
  siendo barata.
- Media historica (>14 dias) queda fuera: sus URLs ya caducaron de todos
  modos.
- El crecimiento de archivos descargados queda acotado por el piso de disco;
  una politica de retencion del store sigue pendiente como trabajo separado.
