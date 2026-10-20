# Contribuir

## Tests

`wa-rag/` usa scripts de unittest de stdlib: sin pytest, sin deps de test.
Córrelos desde el directorio:

```sh
cd wa-rag
python3 test_rag.py
python3 test_media_index.py
python3 test_archive_import.py
```

Decisión: scripts standalone en vez de pytest porque wa-rag es solo stdlib y
agregar un runner sería la primera dependencia de tests del repo sin ganancia.

El bridge tiene tests en Go:

```sh
cd bridge && go test ./...
```

El workflow `check` (`.github/workflows/check.yml`) compila el bridge, corre
sus tests, compila cada módulo de Python, revisa la sintaxis de los scripts
de Node y corre estos tests en cada PR.

`mcp-server/test_never_send.py` es el test del guardrail NEVER_SEND: importa
`main.py` y afirma que el set de tools MCP registradas es exactamente el set
de solo lectura:

```sh
cd mcp-server && uv run python test_never_send.py
```

Sin `uv` (es lo que hace CI), instala las deps y corre con el python del
entorno:

```sh
pip install "mcp[cli]>=1.6.0,<2" httpx requests
cd mcp-server
python3 test_never_send.py
```
