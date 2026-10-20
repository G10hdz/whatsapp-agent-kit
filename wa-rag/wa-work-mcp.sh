#!/bin/bash
# Lanzador del MCP acotado a grupos de trabajo. Registra este en agentes de
# terceros o en la nube; main.py da acceso completo al historial y esta
# pensado para un modelo local.
#
# El alcance vive aqui y no en el agente: si estas variables faltan o cambian,
# main_work.py se niega a arrancar en vez de servir de mas.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"

# Via el symlink estable ~/.local/share/wa/ -> store del bridge vivo. Si el
# bridge se mueve, se repunta el symlink y este archivo no cambia.
export WA_RAG_SOURCE_DB="$HOME/.local/share/wa/messages.db"
export WA_RAG_WORK_INDEX_DB=$HOME/.local/share/wa-rag-work/rag-work.db
export WA_RAG_INDEX_DB="$WA_RAG_WORK_INDEX_DB"

# Alcance: solo grupos. Los personales NO se listan aqui, viven en el denylist
# compartido, que es la unica fuente de verdad de ese limite. Duplicarlos aqui
# crearia una segunda lista que se desincroniza en silencio.
# Cambiar el denylist obliga a reconstruir: rag.py sync --rebuild
export WA_RAG_CHATS=groups
export WA_RAG_DENY_FILE=$HOME/.config/wa-scope/never-index.jids

cd "$HERE"
# python3 del sistema: uv no esta en el PATH de un shell no interactivo, que es
# como los agentes lanzan sus servidores MCP.
exec python3 main_work.py
