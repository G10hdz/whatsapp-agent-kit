#!/usr/bin/env python3
"""MCP server acotado a grupos de trabajo. Este es el unico servidor que se
registra en agentes de terceros (Claude Code, Codex, etc).

Diferencia con main.py, que es para uso local sobre el numero personal:

  - main.py expone list_messages, list_chats, get_chat, get_message_context y
    download_media. Todos leen messages.db directo, sin pasar por rag.py, asi
    que ningun filtro del RAG los alcanza. Por eso este archivo NO importa
    whatsapp.py: lo que no se importa no se puede llamar.
  - Aqui solo vive search_messages_semantic, atado a un indice construido con
    WA_RAG_CHATS / WA_RAG_EXCLUDE_CHATS. Los chats personales no estan en el
    archivo, asi que ninguna consulta los puede devolver.

El contenido que devuelve este servidor SALE de la maquina hacia el modelo que
lo consulta. Esa es la diferencia con el perfil personal, y es a proposito. El
allowlist es el unico control: no hay nada mas atras. Ver NEVER_SEND.md.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

import rag
import redact

# El indice de trabajo vive aparte del personal. Sin esto apuntaria al default
# de rag.py, que es el indice completo.
WORK_INDEX_DB = os.environ.get("WA_RAG_WORK_INDEX_DB", "").strip()

# Enmascara JIDs y telefonos en la salida sin romper las citas por message_id.
REDACT = os.environ.get("WA_RAG_WORK_REDACT", "1") != "0"


def _check_scope() -> None:
    """Sin allowlist explicito este servidor no arranca.

    rag.search(small_sync=True) reindexa en cada consulta usando rag.CHATS. Si
    la variable llegara vacia, ese sync meteria TODOS los chats al indice de
    trabajo y los expondria al agente. Fallar al arrancar es preferible a
    descubrirlo despues por los resultados.
    """
    if not WORK_INDEX_DB:
        raise SystemExit("WA_RAG_WORK_INDEX_DB no esta definido")
    if not rag.CHATS:
        raise SystemExit("WA_RAG_CHATS no esta definido; sin alcance no se arranca")
    # El indice tiene que declarar con que alcance se construyo. Comparar rutas
    # no sirve: el lanzador iguala WA_RAG_INDEX_DB a este a proposito, para que
    # el small_sync escriba aqui. Y una ruta que se ve bien puede apuntar a un
    # indice sin filtrar. Se le pregunta al archivo, no al nombre.
    idx = rag.open_index(WORK_INDEX_DB)
    try:
        row = idx.execute(
            "SELECT value FROM rag_meta WHERE key = 'chat_filter'"
        ).fetchone()
    finally:
        idx.close()
    if not row or not row[0].split("|")[0]:
        raise SystemExit(
            f"{WORK_INDEX_DB} no declara alcance: es un indice sin filtrar "
            "(el personal), no se expone"
        )


mcp = FastMCP("whatsapp-work")


@mcp.tool()
def search_messages_semantic(
    query: str,
    chat_jid: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = 8,
) -> Dict[str, Any]:
    """Busca en los grupos de trabajo de WhatsApp (FTS5 + semantico).

    Devuelve ventanas de conversacion en orden cronologico con citas
    (message_id, sender, timestamp, content). El alcance esta fijado al
    indice de trabajo: chats personales y conversaciones directas no estan
    incluidos y no se pueden consultar desde aqui.

    Args:
        query: Pregunta en lenguaje natural o palabras clave (no vacia).
        chat_jid: JID opcional para acotar a un grupo.
        after: Cota inferior ISO-8601 opcional (inclusiva).
        before: Cota superior ISO-8601 opcional (inclusiva).
        limit: Maximo de ventanas a devolver (1-20, default 8).
    """
    try:
        res = rag.search(
            query,
            chat_jid=chat_jid,
            after=after,
            before=before,
            limit=limit,
            idx_path=WORK_INDEX_DB,
            small_sync=True,
        )
    except rag.RagError as e:
        return {"error": str(e), "results": []}
    if REDACT:
        res["results"] = redact.redact_windows(res["results"])
    return res


if __name__ == "__main__":
    _check_scope()
    mcp.run(transport="stdio")
