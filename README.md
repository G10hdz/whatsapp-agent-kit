# whatsapp-agent-kit

Kit para correr agentes sobre WhatsApp, local-first. Piezas:

```
bridge/       bridge Go (whatsmeow): vincula un numero y vuelca chats/mensajes
              a SQLite (store/whatsapp.db + store/messages.db). API HTTP local
              en :8080 para download de media. Capacidad de envio REMOVIDA —
              ver docs/NEVER_SEND.md.
mcp-server/   servidor MCP en Python (FastMCP): 10 tools read-only sobre el
              historial + busqueda semantica via wa-rag.
wa-rag/       indice de recuperacion: FTS5 + embeddings (nomic-embed-text via
              Ollama loopback) en un solo rag.db, mas captions de imagen,
              transcripts de audio y texto de documentos.
grupo-bot/    bot de grupo con Baileys para un numero DESECHABLE (fase 1:
              vincular, descubrir JID, mandar un hola).
deploy/       units systemd --user y layout canonico en el servidor Linux.
docs/         NEVER_SEND, ADR del indice de media y learnings del deploy real.
```

## Flujo

```
numero real ──vincula──> bridge (Go) ──escribe──> store/messages.db
                                                      │        ▲
                                              wa-rag indexa    │ /api/download
                                                      ▼        │
                                              rag.db (FTS5+vec)
                                                      ▲
mcp-server lee ambos y expone tools a tu agente ──────┘
numero desechable ──vincula──> grupo-bot (Baileys) ──> postea al grupo
```

Regla de oro: **una sesion por cuenta**. El bridge y el bot corren en una sola
maquina; levantar dos procesos sobre el mismo auth expulsa la sesion en bucle.

## Empezar

1. Lee `REQUIREMENTS.md` — runtimes, modelos y servicios por pieza.
2. `deploy/README.md` — layout en el servidor, units y compilacion.
3. `grupo-bot/README.md` — vincular el numero desechable y mandar el hola.
4. `docs/NEVER_SEND.md` — por que el bridge personal no puede enviar nada.

Basado en `lharries/whatsapp-mcp` (bridge Go + server Python) mas un indice
RAG propio y un bot de grupo en Baileys.
