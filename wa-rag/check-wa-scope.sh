#!/bin/bash
# Verifica que ningun consumidor de WhatsApp lea un chat del denylist.
#
# rag.py aplica el denylist solo; agent-hq mantiene su propio allowlist en otro
# archivo y no lo conoce. Este script es el puente: falla si el allowlist de
# agent-hq incluye un JID vetado. Correr tras tocar cualquiera de las dos listas.
#
#   ./check-wa-scope.sh   -> exit 0 si todo cuadra, 1 si hay solape
set -uo pipefail

DENY_FILE="${WA_RAG_DENY_FILE:-$HOME/.config/wa-scope/never-index.jids}"
HQ_ENV="${AGENT_HQ_WA_ENV:-$HOME/.config/agent-hq/wa-project-chats.env}"

if [ ! -r "$DENY_FILE" ]; then
    echo "denylist ilegible: $DENY_FILE" >&2
    exit 1
fi

# Un JID por linea, sin comentarios ni vacios.
denied=$(sed 's/#.*//' "$DENY_FILE" | tr -d '[:blank:]' | grep -v '^$' | sort -u)

if [ -z "$denied" ]; then
    echo "denylist vacio: $DENY_FILE" >&2
    exit 1
fi

status=0

if [ -r "$HQ_ENV" ]; then
    hq=$(grep -E '^WA_PROJECT_CHAT_JIDS=' "$HQ_ENV" | cut -d= -f2- \
         | tr -d '"' | tr ',' '\n' | tr -d '[:blank:]' | grep -v '^$' | sort -u)
    overlap=$(comm -12 <(echo "$denied") <(echo "$hq"))
    if [ -n "$overlap" ]; then
        echo "agent-hq ingiere chats vetados:" >&2
        echo "$overlap" >&2
        status=1
    fi
else
    echo "aviso: no pude leer $HQ_ENV, no verifique agent-hq" >&2
fi

# El indice de trabajo no debe contener ningun JID vetado.
WORK_INDEX="${WA_RAG_WORK_INDEX_DB:-$HOME/.local/share/wa-rag-work/rag-work.db}"
if [ -r "$WORK_INDEX" ] && command -v sqlite3 >/dev/null; then
    quoted=$(echo "$denied" | sed "s/.*/'&'/" | paste -sd, -)
    n=$(sqlite3 "$WORK_INDEX" \
        "SELECT COUNT(*) FROM rag_documents WHERE chat_jid IN ($quoted);" 2>/dev/null)
    if [ "${n:-0}" != "0" ]; then
        echo "el indice de trabajo contiene $n mensajes de chats vetados" >&2
        status=1
    fi
fi

[ "$status" = 0 ] && echo "ok: ningun consumidor toca los chats vetados"
exit "$status"
