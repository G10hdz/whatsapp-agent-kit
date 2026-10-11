# Despliegue en Linux

Layout canonico en el servidor:

```
~/projects/whatsapp-agent-kit/
├── grupo-bot/                           # bot de grupo (Baileys, numero desechable)
│   ├── wa-connect.mjs
│   └── auth-*/                          # sesion Baileys — jamas compartir con el bridge
└── bridge/                              # bridge whatsmeow (numero real)
    ├── main.go  go.mod  go.sum
    ├── whatsapp-bridge                  # binario: compilar EN el servidor con CGO_ENABLED=1
    └── store/                           # whatsapp.db (sesion) + messages.db (ingesta)

~/.local/share/wa/                       # indireccion estable para consumidores
├── messages.db -> ~/projects/whatsapp-agent-kit/bridge/store/messages.db
└── whatsapp.db -> ~/projects/whatsapp-agent-kit/bridge/store/whatsapp.db
```

Los consumidores (mcp-server, wa-rag, otros agentes) leen
`~/.local/share/wa/messages.db`, no el store directo. Si el bridge se mueve,
se repunta el symlink y ningun consumidor cambia. Una ruta de datos vieja no
falla: devuelve una copia congelada sin error en los logs.

## Units

| Unit | Proceso | WorkingDirectory |
| --- | --- | --- |
| `wa-grupo.service` | `node wa-connect.mjs` | `~/projects/whatsapp-agent-kit/grupo-bot` |
| `wa-bridge.service` (+ `wa-bridge.service.d/restart.conf`) | `./whatsapp-bridge` | `~/projects/whatsapp-agent-kit/bridge` |
| `wa-rag-sync.service` + `.timer` | `rag.py sync` + `media_index.py sync` cada 30 min | `~/projects/whatsapp-agent-kit/wa-rag` |

Instalacion completa en `REQUIREMENTS.md` (seccion deploy).

## Compilar el bridge

En el servidor (con gcc):

```bash
cd ~/projects/whatsapp-agent-kit/bridge
CGO_ENABLED=1 go build -o whatsapp-bridge .
```

`go-sqlite3` exige cgo: un binario cross-compiled con `CGO_ENABLED=0` arranca
y muere al abrir `whatsapp.db` ("go-sqlite3 requires cgo to work").

## Reglas

- Una sola sesion por cuenta: `wa-bridge` (numero real) y `wa-grupo`
  (desechable) corren solo en el servidor, nunca en la laptop a la vez. Dos
  procesos sobre el mismo store/auth expulsan la sesion en bucle.
- El bridge nunca envia: el codigo de envio esta amputado del fuente
  (`docs/NEVER_SEND.md`). Si aparece `/api/send` o `client.SendMessage` en
  `main.go`, es regresion.
- Sesion muerta (401/logout remoto) = `whatsmeow_device` vacio en
  `store/whatsapp.db`: el bridge pide QR al arrancar. Re-parear con el QR del
  journal, o copiar un store sano.
