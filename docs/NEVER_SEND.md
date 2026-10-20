# Guardrail NEVER-SEND: asistente personal

> El asistente corre sobre un número **personal** y lee mensajes privados.
> Invariante no negociable: **NUNCA envía nada.** No es que el modelo "decida
> no enviar": la capacidad no debe existir en el código.

## Qué se removió (capa MCP, la que ve el agente)

En `mcp-server/main.py` se eliminaron:

- El registro `@mcp.tool()` de `send_message`, `send_file`, `send_audio_message`.
- Sus imports desde `whatsapp.py` (ya ni se referencian).

**Tools MCP que quedan (todas read-only):** search_contacts, list_messages,
list_chats, get_chat, get_direct_chat_by_contact, get_contact_chats,
get_last_interaction, get_message_context, search_messages_semantic,
download_media.

`download_media` se conserva: descarga a disco media **recibida**, es
lectura/retrieval, no envío.

## Cómo verificar el guardrail

```sh
cd mcp-server
uv run python test_never_send.py   # importa main.py y afirma el set exacto de tools
```

El test corre en CI (`check` workflow): importa `main.py` y falla si el set de
tools registradas difiere del set read-only documentado.

## Guardrail estructural en las tres capas

No es solo cosmético del MCP: la capacidad de envío está amputada en cada capa.

- `bridge/main.go`: eliminados `/api/send`, `sendWhatsAppMessage`,
  `SendMessageRequest/Response` y TODA llamada a `client.SendMessage`.
  Recepción/`StoreMessage`/ingesta intactas. La API HTTP escucha solo en
  127.0.0.1. Si reaparece alguna de esas cadenas en `main.go`, es regresión:

  ```sh
  grep -rnE "SendMessage|/api/send|sendWhatsAppMessage" bridge/main.go   # = 0
  grep -nE "def send_" mcp-server/whatsapp.py                            # = 0
  ```

- `mcp-server/whatsapp.py`: eliminadas `send_message`, `send_file`,
  `send_audio_message`. `download_media` (retrieval de media recibida)
  conservado.
- `mcp-server/audio.py`: eliminado completo; convertía audio a Opus para
  mandar notas de voz.

- Base del server: `lharries/whatsapp-mcp` (bridge en Go con whatsmeow y
  servidor MCP en Python). Cumple *SQLite local + MCP + local-first*.

## Invariantes que faltan configurar en un deploy nuevo

- authState del bridge en su propio directorio (`store/whatsapp.db`), separado
  del auth del bot de grupo (`grupo-bot/auth-*`). Una sola sesión por cuenta:
  dos procesos sobre el mismo store se expulsan en bucle.
- Cliente MCP apuntando a modelo **local** (Ollama en loopback) para todo lo
  que toque contenido de mensajes.
- Auditoría adversarial pendiente: verificar que ninguna dep de whatsmeow ni
  import residual reintroduce una ruta de envío.
