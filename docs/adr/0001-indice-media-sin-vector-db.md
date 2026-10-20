# ADR 0001: Índice de media en SQLite, sin vector DB aparte

## Estado

Aceptado: 2026-09-29

## Contexto

El historial de WhatsApp incluye imágenes, audios y documentos cuyo contenido
no es texto. Hace falta buscarlos por significado (ej. "el flyer del evento",
"el audio donde me piden X"). La pregunta abierta era si eso requería una base
vectorial dedicada (Qdrant, Weaviate, pgvector sobre Postgres, sqlite-vec).

Hechos del deployment:

- `messages.db` tiene ~30k mensajes; la fracción media es pequeña (cientos, no
  millones).
- `rag.db` ya guarda embeddings como blobs y hace ranking híbrido FTS5+vector
  en Python puro, sin latencia perceptible a esta escala.
- Todo el pipeline es local-only por diseño; agregar un servicio más suma un
  proceso que mantener, backups que coordinar y una superficie extra de
  fallo/drift.
- Disco en el servidor: ~50 GB libres, 91% usado. Cada media indexado materializa el
  archivo.

## Decisión

Extender el esquema derivado existente, no introducir una base vectorial.

1. `rag.db` gana `rag_media` + `rag_media_fts` + `rag_media_state`: un doc por
   mensaje con media, con `modality`, `file_path`, `caption`, `status`,
   `model` y `embedding` (mismo espacio 768-dim de `nomic-embed-text`).
2. Media -> texto buscable con modelos locales: `qwen3-vl:8b` captiona
   imágenes, `faster-whisper` transcribe audios, `pdftotext` extrae texto de
   documentos. El texto derivado es lo que se indexa y se busca.
3. Downloads vía `/api/download` del bridge, bajo el mismo allowlist/denylist
   aplicado en SQL antes del fetch. Ventana de `WA_MEDIA_MAX_AGE_DAYS` (14) y
   piso de `WA_MEDIA_MIN_FREE_GB` (5).
4. Si la escala alguna vez rompe el scan lineal de blobs, la frontera ya está
   dibujada: cambiar solo la lectura de embeddings a `sqlite-vec` es un swap
   de módulo, no una migración de arquitectura.

## Consecuencias

- Búsqueda por significado sobre imágenes, audios y documentos sin servicios
  nuevos ni datos saliendo de la máquina.
- Caption/transcripción cuestan CPU local en indexación; la búsqueda sigue
  siendo barata.
- Media histórica (>14 días) queda fuera: sus URLs ya caducaron de todos
  modos.
- El crecimiento de archivos descargados queda acotado por el piso de disco;
  una política de retención del store sigue pendiente como trabajo separado.
