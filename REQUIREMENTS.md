# Requisitos

Qué necesita cada pieza y cómo instalarla. Todo corre local. Lo único que sale
de la máquina es el tráfico de la sesión de WhatsApp y, si tu agente usa un
modelo en la nube, lo que el agente consulte. El kit no incluye clientes de
LLM en la nube.

## Lo mínimo

| Pieza | Runtime | Deps externas | Servicios |
| --- | --- | --- | --- |
| `bridge/` | Go 1.25+ y un compilador C (CGO) | ninguna | whatsmeow -> WhatsApp |
| `mcp-server/` | Python 3.11+ | `mcp[cli]>=1.6.0,<2`, `httpx`, `requests` | bridge corriendo en 127.0.0.1:8080 |

## Extras opcionales

| Pieza | Runtime | Deps externas | Servicios |
| --- | --- | --- | --- |
| `wa-rag/` | Python 3.11+ (stdlib en el core) | Ollama (`nomic-embed-text`, `qwen3-vl:8b`), faster-whisper, poppler | Ollama en loopback |
| `grupo-bot/` | Node 22.9+ | `baileys`, `@hapi/boom`, `qrcode-terminal` | número desechable |
| `deploy/` | systemd --user | lingering habilitado | Linux |

---

## bridge/: ingesta de whatsmeow a SQLite

**Necesita:** Go 1.25+ y un compilador C (`xcode-select --install` en macOS,
`build-essential` en Debian/Ubuntu). `go-sqlite3` exige CGO: un binario con
`CGO_ENABLED=0` arranca y muere al abrir `whatsapp.db`.

```sh
cd bridge
CGO_ENABLED=1 go build -o whatsapp-bridge .
./whatsapp-bridge          # primer arranque: imprime QR, vincula el número
```

La sesión queda en `store/whatsapp.db`, los mensajes en `store/messages.db`.
La API HTTP escucha solo en `http://127.0.0.1:8080` (la usa `mcp-server` para
descargar media). Compila EN la máquina donde va a correr: no cross-compiles
con CGO.

## mcp-server/: tools read-only para tu agente

**Necesita:** Python 3.11+. Deps: `mcp[cli]>=1.6.0,<2`, `httpx`, `requests`.

```sh
cd mcp-server
uv sync                            # o sin uv: python3 -m venv .venv && .venv/bin/pip install "mcp[cli]>=1.6.0,<2" httpx requests
uv run python test_never_send.py   # verifica el guardrail ANTES de registrarlo
uv run main.py                     # stdio server
```

Lee `../bridge/store/messages.db` (ruta relativa en `whatsapp.py`) y llama al
bridge en `127.0.0.1:8080` para media.

**Registrarlo en un cliente MCP** (stdio):

```sh
claude mcp add whatsapp -- uv --directory /ruta/al/repo/mcp-server run main.py
# o equivalente en tu cliente: command = uv run main.py, cwd = mcp-server/
```

> Privacidad: `main.py` expone todo el historial al modelo que use el agente.
> Se recomienda un modelo local. Para agentes de terceros o en la nube existe
> el perfil acotado `wa-rag/wa-work-mcp.sh`.

## wa-rag/: índice FTS5 + vector + media a texto

**Core** (`rag.py`, tests): Python stdlib pura, sin deps. El embedder real
habla con Ollama en loopback.

```sh
ollama pull nomic-embed-text   # embeddings
ollama pull qwen3-vl:8b        # captions de imágenes (media_index)
python3 -m venv wa-rag/.venv   # en distros PEP 668 (Debian 12+) pip al sistema está bloqueado
wa-rag/.venv/bin/pip install faster-whisper   # transcripts de audio
sudo apt install poppler-utils # pdftotext, para documentos
```

Knobs por env (todos opcionales, defaults razonables):

| Var | Qué hace |
| --- | --- |
| `WA_RAG_SOURCE_DB` | messages.db origen (apunta al symlink estable) |
| `WA_RAG_INDEX_DB` | rag.db destino |
| `WA_RAG_CHATS` | `groups` = solo grupos; vacío = todo |
| `WA_RAG_DENY_FILE` | denylist de JIDs, uno por línea (ver `never-index.jids.example`) |
| `WA_MEDIA_MAX_AGE_DAYS` | default 14: las URLs de media caducan, no indexa historia muerta |
| `WA_MEDIA_MIN_FREE_GB` | default 5: frena antes de llenar el disco |

`main_work.py` + `wa-work-mcp.sh`: segundo servidor MCP **acotado a grupos de
trabajo**, pensado para agentes de terceros o en la nube. El alcance vive en
el launcher por env; si falta, el server se niega a arrancar.
Si otro programa lee el mismo `messages.db` con su propia allowlist, revisa
que no incluya ningún JID del denylist.

Ollama en Linux con GPU AMD RDNA2 no soportada por ROCm: arranca con
`OLLAMA_VULKAN=1` (ver docs/LEARNINGS.md).

## grupo-bot/: plantilla de bot de grupo (Baileys, número desechable)

**Necesita:** Node 22.9+ (usa `--env-file-if-exists`). Un número
**desechable**, jamás el personal.

```sh
cd grupo-bot
npm install
cp .env.example .env       # llena WA_PHONE (solo dígitos con lada, sin +)
npm start                  # imprime código de vinculación (o QR con WA_PAIR_MODE=qr)
```

- OJO México: el JID usa `521` + 10 dígitos, no `52`. Se verifica en
  `auth-*/creds.json` tras vincular.
- Sin `GRUPO_JID` corre en modo descubrimiento: lista los grupos con su JID.
- `GRUPO_JID` acepta una lista separada por comas; manda el "hola" a cada
  grupo con una pausa de 3 segundos.
- `useMultiFileAuthState` es prototipo (IO-heavy); para 24/7 migra creds a DB.

## deploy/: systemd --user en Linux

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
en otra ruta, ajusta `WorkingDirectory`/`ExecStart`. Crea la indirección:

```sh
mkdir -p ~/.local/share/wa
ln -s ~/projects/whatsapp-agent-kit/bridge/store/messages.db ~/.local/share/wa/messages.db
ln -s ~/projects/whatsapp-agent-kit/bridge/store/whatsapp.db ~/.local/share/wa/whatsapp.db
```

Los consumidores leen el symlink, no el store directo: si el bridge se mueve
solo repuntas el link. En `wa-grupo.service` corrige la ruta de `node`
(`which node`); la que viene es un ejemplo nvm.

## Datos que NO van a git

`.env`, `auth-*/` (sesiones Baileys), `bridge/store/` (sesión + mensajes),
`wa-rag/never-index.jids` real (tu denylist personal), `.venv/`. El
`.gitignore` del repo ya los cubre; no los commitees ni aunque el repo sea
privado.
