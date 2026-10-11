# REQUIREMENTS

Que necesita cada pieza y como instalarlo. Todo corre local; lo unico que sale
de la maquina es la sesion de WhatsApp misma (y Cerebras si usas ese helper).

## Tabla rapida

| Pieza | Runtime | Deps externas | Servicios |
| --- | --- | --- | --- |
| `bridge/` | Go 1.22+ + gcc (CGO) | ninguna | whatsmeow -> WhatsApp |
| `mcp-server/` | Python >= 3.11 | `mcp[cli]>=1.6.0,<2`, `httpx`, `requests` | bridge corriendo en :8080 |
| `wa-rag/` | Python >= 3.11 (stdlib core) | Ollama (`nomic-embed-text`, `qwen3-vl:8b`), faster-whisper, poppler | Ollama en loopback |
| `grupo-bot/` | Node >= 20.6 | `baileys`, `@hapi/boom`, `qrcode-terminal` | numero desechable |
| `deploy/` | systemd --user | lingering habilitado | Linux |
| learning stack (opcional) | `nlm` CLI | `uv tool install notebooklm-mcp-cli` | cuenta Google NotebookLM |

---

## bridge/ — ingesta whatsmeow a SQLite

**Necesita:** Go >= 1.22 y un compilador C (`gcc`). `go-sqlite3` exige CGO:
un binario con `CGO_ENABLED=0` arranca y muere al abrir `whatsapp.db`.

```sh
cd bridge
CGO_ENABLED=1 go build -o whatsapp-bridge .
./whatsapp-bridge          # primer arranque: imprime QR, vincula el numero
```

La sesion queda en `store/whatsapp.db`, los mensajes en `store/messages.db`.
API HTTP local en `http://localhost:8080` (la usa `mcp-server` para descargar
media). Compila EN la maquina donde va a correr: no cross-compiles con CGO.

## mcp-server/ — tools read-only para tu agente

**Necesita:** Python >= 3.11. Deps: `mcp[cli]>=1.6.0,<2`, `httpx`, `requests`.

```sh
cd mcp-server
uv sync                    # o: python3 -m venv .venv && pip install "mcp[cli]>=1.6.0,<2" httpx requests
python3 test_never_send.py # verifica el guardrail ANTES de registrarlo
uv run main.py             # stdio server
```

Lee `../bridge/store/messages.db` (ruta relativa en `whatsapp.py`) y llama al
bridge en `localhost:8080` para media. Opcional: `CEREBRAS_API_KEY` solo para
`llm_cerebras.py` (no es requerido por los tools).

**Registrarlo en un cliente MCP** (stdio):

```sh
claude mcp add whatsapp -- uv --directory /ruta/al/repo/mcp-server run main.py
# o equivalente en tu cliente: command = python3 main.py, cwd = mcp-server/
```

## wa-rag/ — indice FTS5 + vector + media->texto

**Core** (`rag.py`, tests): Python stdlib pura, sin deps. El embedder real
habla con Ollama en loopback.

```sh
ollama pull nomic-embed-text   # embeddings
ollama pull qwen3-vl:8b        # captions de imagenes (media_index)
python3 -m venv wa-rag/.venv   # OJO: en distros PEP 668 (Pop!_OS, Debian 12+)
wa-rag/.venv/bin/pip install faster-whisper   # transcripts de audio
sudo apt install poppler-utils # pdftotext, para documentos
```

Knobs por env (todos opcionales, defaults razonables):

| Var | Que hace |
| --- | --- |
| `WA_RAG_SOURCE_DB` | messages.db origen (apunta al symlink estable) |
| `WA_RAG_INDEX_DB` | rag.db destino |
| `WA_RAG_CHATS` | `groups` = solo grupos; vacio = todo |
| `WA_RAG_DENY_FILE` | denylist de JIDs, uno por linea (ver `never-index.jids.example`) |
| `WA_MEDIA_MAX_AGE_DAYS` | default 14: URLs de media caducan, no indexes historia muerta |
| `WA_MEDIA_MIN_FREE_GB` | default 5: frena antes de llenar el disco |

`main_work.py` + `wa-work-mcp.sh`: segundo servidor MCP **acotado a grupos de
trabajo**, es el unico que se registra en agentes de terceros. El alcance vive
en el launcher por env; si falta, el server se niega a arrancar.
`check-wa-scope.sh` verifica que ningun consumidor toque chats del denylist.

## grupo-bot/ — bot de grupo (Baileys, numero desechable)

**Necesita:** Node >= 20.6 (usa `--env-file-if-exists`). Un numero
**desechable**, jamas el personal.

```sh
cd grupo-bot
npm install
cp .env.example .env       # llena WA_PHONE (solo digitos con lada, sin +)
npm start                  # imprime codigo de vinculacion (o QR con WA_PAIR_MODE=qr)
```

- OJO Mexico: el JID usa `521` + 10 digitos, no `52`. Se verifica en
  `auth-*/creds.json` tras vincular.
- Sin `GRUPO_JID` corre en modo descubrimiento: lista los grupos con su JID.
- Con `GRUPO_JID=...@g.us` manda un "hola" una vez por proceso.
- `useMultiFileAuthState` es prototipo (IO-heavy); para 24/7 migra creds a DB.

## deploy/ — systemd --user en Linux

**Necesita:** systemd con user services y lingering:

```sh
sudo loginctl enable-linger $USER
install -Dm644 deploy/systemd/wa-bridge.service ~/.config/systemd/user/wa-bridge.service
install -Dm644 deploy/systemd/wa-grupo.service  ~/.config/systemd/user/wa-grupo.service
install -Dm644 deploy/systemd/wa-bridge.service.d/restart.conf \
  ~/.config/systemd/user/wa-bridge.service.d/restart.conf
install -Dm644 deploy/systemd/wa-rag-sync.service ~/.config/systemd/user/wa-rag-sync.service
install -Dm644 deploy/systemd/wa-rag-sync.timer   ~/.config/systemd/user/wa-rag-sync.timer
systemctl --user daemon-reload
systemctl --user enable --now wa-bridge.service wa-grupo.service wa-rag-sync.timer
```

Las units asumen el checkout en `~/projects/whatsapp-agent-kit`; si lo pones
en otra ruta, ajusta `WorkingDirectory`/`ExecStart`. Crea la indireccion:

```sh
mkdir -p ~/.local/share/wa
ln -s ~/projects/whatsapp-agent-kit/bridge/store/messages.db ~/.local/share/wa/messages.db
ln -s ~/projects/whatsapp-agent-kit/bridge/store/whatsapp.db ~/.local/share/wa/whatsapp.db
```

Los consumidores leen el symlink, no el store directo: si el bridge se mueve
solo repuntas el link. En `wa-grupo.service` corrige la ruta de `node`
(`which node`) — la que viene es un ejemplo nvm.

## Opcional: pipeline de aprendizaje (NotebookLM + hub web)

Si tambien armas el learning stack (audiolibros, flashcards, hub movil):

```sh
uv tool install notebooklm-mcp-cli   # CLI `nlm` para NotebookLM
nlm login                            # auth con tu cuenta Google
```

- `nlm` = CLI de NotebookLM (una libreta por tema, audio overviews que se
  vuelven audiolibros). Se instala como tool de `uv`, no con pip suelto.
- Hub web: `tailscale serve` para exponer sitios estaticos solo dentro de tu
  tailnet (nada publico). Ver el repo `learning-surface` para el patron.
- Ollama en Linux con GPU RDNA2 no soportada por ROCm: arranca con
  `OLLAMA_VULKAN=1`/`OLLAMA_LLM_LIBRARY=vulkan` (ver docs/LEARNINGS.md).

## Datos que NO van a git

`.env`, `auth-*/` (sesiones Baileys), `bridge/store/` (sesion + mensajes),
`wa-rag/never-index.jids` real (tu denylist personal), `.venv/`. El
`.gitignore` del repo ya los cubre; no los commitees ni aunque el repo sea
privado.
