#!/usr/bin/env python3
"""Local WhatsApp RAG: FTS5 + semantic embeddings via loopback Ollama.

Reads the canonical bridge DB read-only, writes a separate rag.db with FTS5
and embeddings. Does not modify messages.db, does not touch the Go bridge,
does not expose HTTP, and never sends content to a remote embedder.

Usage:
    python rag.py sync [--all] [--rebuild]
    python rag.py search "query" [--chat JID] [--after ISO] [--before ISO] [--limit N]

Privacy: logs only counts, watermarks, durations and errors. Never logs
message content, JIDs, phone numbers or full prompts.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# --- Config (env-overridable, except OLLAMA_EMBED_URL which is strict) ---
_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SOURCE_DB = os.path.join(_HERE, "..", "bridge", "store", "messages.db")
_DEFAULT_INDEX_DB = os.path.join(_HERE, "..", "bridge", "store", "rag.db")

SOURCE_DB = os.environ.get("WA_RAG_SOURCE_DB") or _DEFAULT_SOURCE_DB
INDEX_DB = os.environ.get("WA_RAG_INDEX_DB") or _DEFAULT_INDEX_DB
EMBED_MODEL = os.environ.get("WA_RAG_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = int(os.environ.get("WA_RAG_EMBED_DIM", "768"))
EMBED_MAX_CHARS = int(os.environ.get("WA_RAG_EMBED_MAX_CHARS", "6000"))
BATCH_SIZE = int(os.environ.get("WA_RAG_BATCH_SIZE", "64"))
FTS_CANDIDATES = int(os.environ.get("WA_RAG_FTS_CANDIDATES", "30"))
VECTOR_CANDIDATES = int(os.environ.get("WA_RAG_VECTOR_CANDIDATES", "30"))
FINAL_LIMIT_DEFAULT = int(os.environ.get("WA_RAG_FINAL_LIMIT", "8"))
WINDOW_MESSAGES = int(os.environ.get("WA_RAG_WINDOW_MESSAGES", "6"))
MAX_GAP_SECONDS = int(os.environ.get("WA_RAG_MAX_GAP_SECONDS", "1800"))
# Turn-group at index: same chat, gap cap, size caps. Embed/FTS see the block.
TURN_GAP_SECONDS = int(os.environ.get("WA_RAG_TURN_GAP_SECONDS", "300"))
TURN_MAX_MSGS = int(os.environ.get("WA_RAG_TURN_MAX_MSGS", "12"))
TURN_MAX_CHARS = int(os.environ.get("WA_RAG_TURN_MAX_CHARS", "1500"))
CHUNK_STRATEGY = "turn_v1"
RRF_K = int(os.environ.get("WA_RAG_RRF_K", "60"))
SMALL_SYNC_ROWS = int(os.environ.get("WA_RAG_SMALL_SYNC_ROWS", "1000"))
# Alcance del indice. Vacio = todos los chats (perfil personal). "groups" = solo
# grupos (@g.us). Tambien acepta lista de JIDs separados por coma.
CHATS = os.environ.get("WA_RAG_CHATS", "").strip()
EXCLUDE_CHATS = os.environ.get("WA_RAG_EXCLUDE_CHATS", "").strip()
# Denylist compartido: chats que ningun consumidor puede indexar, nunca. Es una
# fuente de verdad aparte del allowlist porque los allowlists difieren por
# proposito (cada consumidor puede mantener el suyo) pero el limite de
# privacidad es el mismo para todos. Un JID aqui gana sobre cualquier
# allowlist. Un JID por linea, "#" comenta.
DENY_FILE = os.environ.get("WA_RAG_DENY_FILE", "").strip()

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class RagError(Exception):
    """Public-facing RAG error (message is safe to surface)."""


# --- Ollama embedder (loopback only) ---
def embed_url() -> str:
    """Resolve the embed endpoint, rejecting any non-loopback host."""
    raw = os.environ.get("OLLAMA_EMBED_URL", "http://127.0.0.1:11434/api/embed")
    m = re.match(r"^(https?)://([^/:]+)(?::\d+)?(?:/.*)?$", raw)
    if not m:
        # Allow the test-only fake: scheme, but never anything else non-HTTP.
        if raw.startswith("fake:"):
            return raw
        raise RagError(f"OLLAMA_EMBED_URL must be http(s)://host[:port], got {raw!r}")
    scheme, host = m.group(1), m.group(2)
    if host not in _LOOPBACK_HOSTS:
        raise RagError(
            f"OLLAMA_EMBED_URL must resolve to loopback (one of {sorted(_LOOPBACK_HOSTS)}); "
            f"got host={host!r}"
        )
    return raw


def embed_batch(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    """Embed a batch of texts via Ollama's /api/embed. Returns normalized vectors."""
    url = embed_url()
    # nomic-embed-text corta en 2048 tokens. Un solo mensaje largo devuelve 400
    # para todo el lote y deja el watermark atorado en ese rowid para siempre.
    # Solo se trunca lo que va al embedder; FTS y rag_documents guardan completo.
    texts = [t[:EMBED_MAX_CHARS] for t in texts]
    if url.startswith("fake:"):
        return [_fake_embedding(t) for t in texts]
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()[:200]
        raise RagError(f"embedder rejected batch (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise RagError(f"cannot reach embedder at {url}: {e.reason}") from e
    except (ValueError, KeyError) as e:
        raise RagError(f"invalid embedder response: {e}") from e
    embeddings = body.get("embeddings") or body.get("data") or []
    if len(embeddings) != len(texts):
        raise RagError(
            f"embedder returned {len(embeddings)} vectors for {len(texts)} texts"
        )
    out: list[list[float]] = []
    for vec in embeddings:
        if len(vec) != EMBED_DIM:
            raise RagError(
                f"embedder returned dim {len(vec)} != expected {EMBED_DIM}"
            )
        out.append(_normalize(vec))
    return out


def _fake_embedding(text: str) -> list[float]:
    """Deterministic embedding from sha256: for tests only, never production.

    Hashes produce stable buckets so identical/related text clusters; semantic
    quality is intentionally weak but adequate for retrieval-mechanics tests.
    """
    # ponytail: deterministic-but-bucketed fake; sufficient for unit tests.
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    # Expand seed deterministically to EMBED_DIM floats
    buf = bytearray(seed)
    while len(buf) < EMBED_DIM * 4:
        buf += hashlib.sha256(bytes(buf[-32:])).digest()
    vec = list(array.array("f"))
    for i in range(EMBED_DIM):
        chunk = buf[i * 4 : i * 4 + 4]
        v = int.from_bytes(chunk, "little") / 2 ** 32 - 0.5
        vec.append(v)
    # Add simple shared-component bias per token so related texts align a bit.
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    for tok in set(tokens):
        h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], "little")
        vec[h % EMBED_DIM] += 1.0
    return _normalize(vec)


def _normalize(vec: Iterable[float]) -> list[float]:
    out = [float(v) for v in vec]
    norm = math.sqrt(sum(v * v for v in out))
    if not math.isfinite(norm) or norm == 0.0:
        raise RagError("embedding has zero or non-finite norm")
    return [v / norm for v in out]


# --- Vector serialization (stdlib array, float32) ---
def vec_to_blob(vec: list[float]) -> bytes:
    if len(vec) != EMBED_DIM:
        raise RagError(f"vector dim {len(vec)} != expected {EMBED_DIM}")
    if not all(math.isfinite(v) for v in vec):
        raise RagError("vector contains non-finite values")
    arr = array.array("f", vec)
    return arr.tobytes()


def blob_to_vec(blob: bytes) -> list[float]:
    if len(blob) != EMBED_DIM * 4:
        raise RagError(f"blob size {len(blob)} != dim {EMBED_DIM} * 4 bytes")
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# --- Connection helpers ---
def open_source(path: str = SOURCE_DB) -> sqlite3.Connection:
    """Open the canonical bridge DB strictly read-only."""
    if not os.path.exists(path):
        raise RagError(f"source db not found: {path}")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA query_only = ON")
    return conn


def open_index(path: str = INDEX_DB) -> sqlite3.Connection:
    """Open (creating if needed) the rag.db index."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rag_documents (
            doc_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            source_rowid INTEGER NOT NULL,
            message_id   TEXT NOT NULL,
            chat_jid     TEXT NOT NULL,
            sender       TEXT,
            timestamp    INTEGER NOT NULL,
            content      TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            model        TEXT NOT NULL,
            embedding    BLOB NOT NULL,
            members_json TEXT,
            UNIQUE(chat_jid, message_id, model)
        );
        CREATE INDEX IF NOT EXISTS idx_rag_source_rowid
            ON rag_documents(model, source_rowid);
        CREATE INDEX IF NOT EXISTS idx_rag_chat_time
            ON rag_documents(chat_jid, timestamp);

        CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
            content,
            sender,
            chat_jid UNINDEXED,
            message_id UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TABLE IF NOT EXISTS rag_state (
            model            TEXT PRIMARY KEY,
            last_source_rowid INTEGER NOT NULL DEFAULT 0,
            updated_at       INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rag_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rag_documents)").fetchall()}
    if "members_json" not in cols:
        conn.execute("ALTER TABLE rag_documents ADD COLUMN members_json TEXT")
    conn.commit()


# --- Preprocessing (conservative; preserve emojis/urls/punctuation/accentes) ---
_WS_RE = re.compile(r"\s+")


def preprocess(text: str) -> str:
    if text is None:
        return ""
    return _WS_RE.sub(" ", text.strip())


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Sync ---
def _get_watermark(conn: sqlite3.Connection, model: str) -> int:
    row = conn.execute(
        "SELECT last_source_rowid FROM rag_state WHERE model = ?", (model,)
    ).fetchone()
    return int(row[0]) if row else 0


def _set_watermark(
    conn: sqlite3.Connection, model: str, rowid: int, ts: int
) -> None:
    conn.execute(
        """
        INSERT INTO rag_state(model, last_source_rowid, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(model) DO UPDATE SET
            last_source_rowid = excluded.last_source_rowid,
            updated_at = excluded.updated_at
        """,
        (model, rowid, ts),
    )


def _check_chat_filter(
    conn: sqlite3.Connection, signature: str, rebuild: bool
) -> None:
    """Refuse to append to an index built under a different chat filter.

    Widening the filter would leave old rows missing; narrowing it would leave
    already-indexed chats in the file. Either way the index no longer matches
    what it claims to cover, and a stale index fails silently at query time.
    """
    row = conn.execute(
        "SELECT value FROM rag_meta WHERE key = 'chat_filter'"
    ).fetchone()
    if row is not None and row[0] != signature and not rebuild:
        raise RagError(
            f"index was built with chat filter {row[0]!r}, now {signature!r}; "
            "re-run with --rebuild"
        )
    conn.execute(
        "INSERT INTO rag_meta(key, value) VALUES ('chat_filter', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (signature,),
    )
    conn.commit()


def load_deny_jids(path: str = "") -> list[str]:
    """Lee el denylist compartido. Un JID por linea, "#" comenta.

    Un archivo configurado pero ilegible es un error, no una lista vacia: fallar
    abierto aqui significa indexar chats personales sin que nada lo diga.
    """
    path = (path or DENY_FILE).strip()
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as e:
        raise RagError(f"no puedo leer el denylist {path}: {e}") from e
    out = []
    for line in raw.splitlines():
        jid = line.split("#", 1)[0].strip()
        if jid:
            out.append(jid)
    return out


def _chat_filter_sql(chats: str, exclude_chats: str) -> tuple[str, list[str]]:
    """Build the chat allowlist/denylist clause.

    chats: "" indexes every chat; "groups" only group chats (@g.us); anything
    else is read as a comma-separated JID list.
    exclude_chats: comma-separated JIDs dropped after the allowlist applies.

    El denylist compartido se aplica aqui, el unico punto por donde pasan todas
    las consultas al source, para que ningun caller lo pueda saltar.
    """
    sql = ""
    params: list[str] = []
    chats = chats.strip()
    if chats == "groups":
        sql += "AND chat_jid LIKE '%@g.us' "
    elif chats:
        jids = [c.strip() for c in chats.split(",") if c.strip()]
        sql += f"AND chat_jid IN ({','.join('?' * len(jids))}) "
        params += jids
    excluded = [c.strip() for c in exclude_chats.split(",") if c.strip()]
    excluded = list(dict.fromkeys(excluded + load_deny_jids()))
    if excluded:
        sql += f"AND chat_jid NOT IN ({','.join('?' * len(excluded))}) "
        params += excluded
    return sql, params



def _format_turn_content(members: list[dict[str, Any]]) -> str:
    lines = []
    for m in members:
        body = (m.get("content") or "").strip()
        if not body:
            continue
        sender = (m.get("sender") or "").strip()
        lines.append(f"{sender}: {body}" if sender else body)
    return "\n".join(lines)


def _turn_from_members(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a contiguous same-chat run into one index document."""
    if not members:
        raise RagError("empty turn")
    first, last = members[0], members[-1]
    payload = [
        {
            "message_id": m["message_id"],
            "sender": m.get("sender") or "",
            "timestamp": int(m["timestamp"]),
            "content": m.get("content") or "",
        }
        for m in members
    ]
    return {
        "rowid": last["rowid"],
        "message_id": first["message_id"],
        "chat_jid": first["chat_jid"],
        "sender": first.get("sender") or "",
        "timestamp": int(first["timestamp"]),
        "content": _format_turn_content(payload),
        "members": payload,
    }


def _can_append_to_turn(cur: list[dict[str, Any]], msg: dict[str, Any]) -> bool:
    prev = cur[-1]
    if msg["chat_jid"] != prev["chat_jid"]:
        return False
    gap = abs(int(msg["timestamp"]) - int(prev["timestamp"]))
    if gap > TURN_GAP_SECONDS:
        return False
    if len(cur) >= TURN_MAX_MSGS:
        return False
    trial = _format_turn_content(
        [
            {
                "sender": m.get("sender") or "",
                "content": m.get("content") or "",
            }
            for m in cur
        ]
        + [{"sender": msg.get("sender") or "", "content": msg.get("content") or ""}]
    )
    return len(trial) <= TURN_MAX_CHARS


def _absorb_messages(
    pending: dict[str, list[dict[str, Any]]],
    msgs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Fold raw messages into completed turns + still-open turns per chat."""
    ready: list[dict[str, Any]] = []
    open_turns = {k: list(v) for k, v in pending.items()}
    for msg in msgs:
        chat = msg["chat_jid"]
        cur = open_turns.get(chat)
        if not cur:
            open_turns[chat] = [msg]
            continue
        if _can_append_to_turn(cur, msg):
            cur.append(msg)
            continue
        ready.append(_turn_from_members(cur))
        open_turns[chat] = [msg]
    return ready, open_turns


def _flush_pending(
    pending: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    turns = [_turn_from_members(v) for v in pending.values() if v]
    turns.sort(key=lambda t: t["rowid"])
    return turns


def _fetch_batch(
    src: sqlite3.Connection, after_rowid: int, limit: int,
    chats: str = "", exclude_chats: str = "",
) -> list[dict[str, Any]]:
    filter_sql, filter_params = _chat_filter_sql(chats, exclude_chats)
    rows = src.execute(
        "SELECT rowid, id, chat_jid, sender, content, timestamp "
        "FROM messages "
        "WHERE rowid > ? AND content IS NOT NULL AND content <> '' "
        + filter_sql
        + "ORDER BY rowid ASC LIMIT ?",
        (after_rowid, *filter_params, limit),
    ).fetchall()
    return [
        {
            "rowid": r[0],
            "message_id": r[1],
            "chat_jid": r[2],
            "sender": r[3] or "",
            "content": preprocess(r[4]),
            "timestamp": _to_epoch(r[5]),
        }
        for r in rows
    ]


def _to_epoch(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return int(s)
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except ValueError:
            try:
                return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp())
            except ValueError:
                return 0
    return 0


def _persist_batch(
    conn: sqlite3.Connection, batch: list[dict[str, Any]], vecs: list[list[float]],
    model: str,
) -> None:
    """Upsert a batch into rag_documents and resync rag_fts in one tx."""
    now = int(time.time())
    try:
        conn.execute("BEGIN")
        for msg, vec in zip(batch, vecs):
            blob = vec_to_blob(vec)
            chash = content_hash(msg["content"])
            members = msg.get("members")
            members_json = (
                json.dumps(members, ensure_ascii=False) if members else None
            )
            conn.execute(
                """
                INSERT INTO rag_documents (
                    source_rowid, message_id, chat_jid, sender, timestamp,
                    content, content_hash, model, embedding, members_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_jid, message_id, model) DO UPDATE SET
                    source_rowid  = excluded.source_rowid,
                    sender        = excluded.sender,
                    timestamp     = excluded.timestamp,
                    content       = excluded.content,
                    content_hash  = excluded.content_hash,
                    embedding     = excluded.embedding,
                    members_json  = excluded.members_json
                """,
                (
                    msg["rowid"],
                    msg["message_id"],
                    msg["chat_jid"],
                    msg["sender"],
                    msg["timestamp"],
                    msg["content"],
                    chash,
                    model,
                    blob,
                    members_json,
                ),
            )
            doc_id = conn.execute(
                "SELECT doc_id FROM rag_documents "
                "WHERE chat_jid = ? AND message_id = ? AND model = ?",
                (msg["chat_jid"], msg["message_id"], model),
            ).fetchone()[0]
            conn.execute("DELETE FROM rag_fts WHERE rowid = ?", (doc_id,))
            conn.execute(
                "INSERT INTO rag_fts(rowid, content, sender, chat_jid, message_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc_id, msg["content"], msg["sender"], msg["chat_jid"], msg["message_id"]),
            )
        last_rowid = batch[-1]["rowid"]
        _set_watermark(conn, model, last_rowid, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def sync_index(
    *,
    src_path: str = SOURCE_DB,
    idx_path: str = INDEX_DB,
    model: str = EMBED_MODEL,
    rebuild: bool = False,
    all_rows: bool = False,
    batch_size: int = BATCH_SIZE,
    log: bool = True,
    chats: str = CHATS,
    exclude_chats: str = EXCLUDE_CHATS,
) -> dict[str, Any]:
    """Sync messages.db -> rag.db. Returns a small stats dict (no content)."""
    t0 = time.time()
    src = open_source(src_path)
    try:
        idx = open_index(idx_path)
        try:
            if rebuild:
                idx.executescript(
                    "DROP TABLE IF EXISTS rag_fts;"
                    "DROP TABLE IF EXISTS rag_documents;"
                    "DROP TABLE IF EXISTS rag_state;"
                    "DROP TABLE IF EXISTS rag_meta;"
                )
                idx.commit()
                _init_schema(idx)
                start_rowid = 0
            elif all_rows:
                start_rowid = 0
            else:
                start_rowid = _get_watermark(idx, model)

            # El denylist entra en la firma: si cambia, el indice ya no cubre lo
            # que dice cubrir y hay que reconstruirlo.
            deny = ",".join(load_deny_jids())
            _check_chat_filter(
                idx, f"{chats}|{exclude_chats}|{deny}", rebuild
            )
            idx.execute(
                "INSERT INTO rag_meta(key, value) VALUES ('chunk_strategy', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (CHUNK_STRATEGY,),
            )
            idx.commit()

            total_indexed = 0
            last_rowid = start_rowid
            pending: dict[str, list[dict[str, Any]]] = {}
            while True:
                batch = _fetch_batch(
                    src, last_rowid, batch_size, chats, exclude_chats
                )
                if not batch:
                    turns = _flush_pending(pending)
                    pending = {}
                    if not turns:
                        break
                else:
                    last_rowid = batch[-1]["rowid"]
                    turns, pending = _absorb_messages(pending, batch)
                    if not turns:
                        continue
                vecs = embed_batch([m["content"] for m in turns], model=model)
                _persist_batch(idx, turns, vecs, model)
                total_indexed += len(turns)
                if log:
                    print(
                        f"  indexed_turns={total_indexed} last_rowid={last_rowid} "
                        f"batches_ok",
                        file=sys.stderr,
                    )
            elapsed = time.time() - t0
            return {
                "model": model,
                "indexed": total_indexed,
                "last_source_rowid": last_rowid,
                "elapsed_seconds": round(elapsed, 3),
            }
        finally:
            idx.close()
    finally:
        src.close()


# --- Search: hybrid FTS5 + vector scan, fused via RRF ---
def _parse_iso(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        return int(
            datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        )
    except ValueError as e:
        raise RagError(f"invalid ISO timestamp {s!r}: {e}") from e


def _build_fts_query(query: str) -> str:
    tokens = re.findall(r"[\w]+", query.lower(), flags=re.UNICODE)
    tokens = [t for t in tokens if t]
    if not tokens:
        return ""
    # Quote each token, escaping embedded double quotes.
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _retrieve_fts(
    idx: sqlite3.Connection,
    fts_query: str,
    chat_jid: Optional[str],
    after: Optional[int],
    before: Optional[int],
    limit: int,
) -> list[tuple[int, float]]:
    """Returns list of (doc_id, score) where lower score = better match (BM25)."""
    if not fts_query:
        return []
    sql = (
        "SELECT f.rowid, bm25(rag_fts) AS score "
        "FROM rag_fts f "
        "JOIN rag_documents d ON d.doc_id = f.rowid "
        "WHERE rag_fts MATCH ? "
    )
    params: list[Any] = [fts_query]
    if chat_jid:
        sql += "AND d.chat_jid = ? "
        params.append(chat_jid)
    if after is not None:
        sql += "AND d.timestamp >= ? "
        params.append(after)
    if before is not None:
        sql += "AND d.timestamp <= ? "
        params.append(before)
    sql += "ORDER BY score ASC LIMIT ?"
    params.append(limit)
    rows = idx.execute(sql, params).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def _retrieve_vector(
    idx: sqlite3.Connection,
    qvec: list[float],
    chat_jid: Optional[str],
    after: Optional[int],
    before: Optional[int],
    limit: int,
) -> list[tuple[int, float]]:
    sql = (
        "SELECT doc_id, embedding, chat_jid, timestamp FROM rag_documents WHERE 1=1 "
    )
    params: list[Any] = []
    if chat_jid:
        sql += "AND chat_jid = ? "
        params.append(chat_jid)
    if after is not None:
        sql += "AND timestamp >= ? "
        params.append(after)
    if before is not None:
        sql += "AND timestamp <= ? "
        params.append(before)
    rows = idx.execute(sql, params).fetchall()
    scored: list[tuple[float, int]] = []
    for doc_id, blob, _jid, _ts in rows:
        try:
            vec = blob_to_vec(blob)
        except RagError:
            continue
        scored.append((_dot(qvec, vec), int(doc_id)))
    scored.sort(reverse=True)
    return [(doc_id, score) for score, doc_id in scored[:limit]]


def _rrf_fuse(
    fts: list[tuple[int, float]],
    vec: list[tuple[int, float]],
    k: int = RRF_K,
    limit: int = FINAL_LIMIT_DEFAULT,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion. Returns (doc_id, fused_score) sorted desc."""
    scores: dict[int, float] = {}
    for rank, (doc_id, _) in enumerate(fts):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (doc_id, _) in enumerate(vec):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return fused[:limit]


def _members_from_row(
    doc_id: int,
    message_id: str,
    sender: str,
    timestamp: int,
    content: str,
    members_json: str | None,
) -> list[dict[str, Any]]:
    """Expand a turn doc into citation-level messages."""
    if members_json:
        try:
            raw = json.loads(members_json)
        except (TypeError, ValueError):
            raw = None
        if isinstance(raw, list) and raw:
            out = []
            for m in raw:
                if not isinstance(m, dict):
                    continue
                out.append(
                    {
                        "doc_id": doc_id,
                        "message_id": str(m.get("message_id") or message_id),
                        "sender": str(m.get("sender") or ""),
                        "timestamp": int(m.get("timestamp") or timestamp),
                        "content": str(m.get("content") or ""),
                    }
                )
            if out:
                return out
    return [
        {
            "doc_id": doc_id,
            "message_id": message_id,
            "sender": sender or "",
            "timestamp": int(timestamp),
            "content": content or "",
        }
    ]


def _window_around(
    idx: sqlite3.Connection, doc_id: int, window: int, max_gap: int
) -> list[dict[str, Any]]:
    anchor = idx.execute(
        "SELECT chat_jid, timestamp FROM rag_documents WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    if not anchor:
        return []
    chat_jid, anchor_ts = anchor
    before_rows = idx.execute(
        "SELECT doc_id, message_id, sender, timestamp, content, members_json "
        "FROM rag_documents "
        "WHERE chat_jid = ? AND timestamp <= ? "
        "ORDER BY timestamp DESC, doc_id DESC LIMIT ?",
        (chat_jid, anchor_ts, window + 1),
    ).fetchall()
    after_rows = idx.execute(
        "SELECT doc_id, message_id, sender, timestamp, content, members_json "
        "FROM rag_documents "
        "WHERE chat_jid = ? AND timestamp > ? "
        "ORDER BY timestamp ASC, doc_id ASC LIMIT ?",
        (chat_jid, anchor_ts, window),
    ).fetchall()
    chronological_docs = list(reversed(before_rows)) + list(after_rows)
    msgs: list[dict[str, Any]] = []
    for r in chronological_docs:
        msgs.extend(
            _members_from_row(r[0], r[1], r[2] or "", int(r[3]), r[4] or "", r[5])
        )
    # Anchor cut uses the turn's first message timestamp (doc timestamp).
    return _cut_by_gap(msgs, int(anchor_ts), max_gap)


def _cut_by_gap(
    msgs: list[dict[str, Any]], anchor_ts: int, max_gap: int
) -> list[dict[str, Any]]:
    """Keep a contiguous run around the anchor, cutting on gaps > max_gap."""
    if not msgs:
        return msgs
    # Find anchor index (timestamp == anchor_ts)
    try:
        anchor_idx = next(
            i for i, m in enumerate(msgs) if m["timestamp"] == anchor_ts
        )
    except StopIteration:
        return msgs
    # Walk left, stop at first gap > max_gap
    left = anchor_idx
    while left > 0:
        gap = msgs[anchor_idx]["timestamp"] - msgs[left - 1]["timestamp"]
        if gap < 0:
            gap = -gap
        if gap > max_gap:
            break
        left -= 1
    # Walk right
    right = anchor_idx
    while right < len(msgs) - 1:
        gap = msgs[right + 1]["timestamp"] - msgs[anchor_idx]["timestamp"]
        if gap < 0:
            gap = -gap
        if gap > max_gap:
            break
        right += 1
    return msgs[left : right + 1]


def _dedup_windows(
    windows: list[list[dict[str, Any]]],
) -> list[list[dict[str, Any]]]:
    seen_msg_ids: set[str] = set()
    out: list[list[dict[str, Any]]] = []
    for w in windows:
        if not w:
            continue
        ids = {m["message_id"] for m in w}
        if ids.issubset(seen_msg_ids):
            continue
        out.append(w)
        seen_msg_ids |= ids
    return out


def search(
    query: str,
    *,
    chat_jid: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = FINAL_LIMIT_DEFAULT,
    idx_path: str = INDEX_DB,
    model: str = EMBED_MODEL,
    small_sync: bool = False,
    src_path: str = SOURCE_DB,
) -> dict[str, Any]:
    """Hybrid search. Returns windows with citations; never raises on no hits."""
    q = query.strip()
    if not q:
        raise RagError("query must not be empty")
    if not (1 <= limit <= 20):
        raise RagError("limit must be between 1 and 20")

    after_ts = _parse_iso(after)
    before_ts = _parse_iso(before)

    if small_sync:
        try:
            sync_index(
                src_path=src_path,
                idx_path=idx_path,
                model=model,
                all_rows=False,
                log=False,
                batch_size=SMALL_SYNC_ROWS,
            )
        except RagError:
            # Small sync best-effort; surface later if search also fails.
            pass

    idx = open_index(idx_path)
    try:
        fts_q = _build_fts_query(q)
        fts_hits = _retrieve_fts(
            idx, fts_q, chat_jid, after_ts, before_ts, FTS_CANDIDATES
        )
        try:
            qvecs = embed_batch([q], model=model)
            qvec = qvecs[0]
            vec_hits = _retrieve_vector(
                idx, qvec, chat_jid, after_ts, before_ts, VECTOR_CANDIDATES
            )
        except RagError as e:
            vec_hits = []
            vec_error = str(e)
        else:
            vec_error = None

        fused = _rrf_fuse(fts_hits, vec_hits, k=RRF_K, limit=limit)
        raw_windows: list[list[dict[str, Any]]] = []
        for doc_id, score in fused:
            win = _window_around(idx, doc_id, WINDOW_MESSAGES, MAX_GAP_SECONDS)
            if not win:
                continue
            chat = win[0]["chat_jid"] if "chat_jid" in win[0] else None
            # chat_jid not stored in window rows; pull from first doc.
            first_doc = idx.execute(
                "SELECT chat_jid FROM rag_documents WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if first_doc:
                chat = first_doc[0]
            ts_range = [min(m["timestamp"] for m in win), max(m["timestamp"] for m in win)]
            raw_windows.append(
                [
                    {
                        **m,
                        "chat_jid": chat,
                        "_anchor_score": score,
                        "_anchor_doc_id": doc_id,
                    }
                    for m in win
                ]
            )
        deduped = _dedup_windows(raw_windows)
        results = []
        for win in deduped:
            anchor_score = win[0].get("_anchor_score", 0.0)
            anchor_doc_id = win[0].get("_anchor_doc_id")
            chat = win[0]["chat_jid"]
            results.append(
                {
                    "score": round(float(anchor_score), 6),
                    "chat_jid": chat,
                    "start_timestamp": min(m["timestamp"] for m in win),
                    "end_timestamp": max(m["timestamp"] for m in win),
                    "anchor_doc_id": anchor_doc_id,
                    "messages": [
                        {
                            "message_id": m["message_id"],
                            "sender": m["sender"],
                            "timestamp": m["timestamp"],
                            "content": m["content"],
                        }
                        for m in win
                    ],
                }
            )
        return {
            "query": q,
            "model": model,
            "fts_query": fts_q,
            "fts_hits": len(fts_hits),
            "vector_hits": len(vec_hits),
            "vector_error": vec_error,
            "results": results,
        }
    finally:
        idx.close()


# --- CLI ---
def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Local WhatsApp RAG (read-only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="Index messages.db into rag.db")
    s.add_argument("--all", action="store_true", help="re-embed all rows")
    s.add_argument("--rebuild", action="store_true", help="drop and rebuild index")

    q = sub.add_parser("search", help="Hybrid search (prints summary, no content)")
    q.add_argument("query")
    q.add_argument("--chat", default=None)
    q.add_argument("--after", default=None, help="ISO-8601 lower bound")
    q.add_argument("--before", default=None, help="ISO-8601 upper bound")
    q.add_argument("--limit", type=int, default=FINAL_LIMIT_DEFAULT)

    args = parser.parse_args(argv)

    if args.cmd == "sync":
        stats = sync_index(all_rows=args.all, rebuild=args.rebuild)
        # Privacy: counts only.
        print(
            json.dumps(
                {
                    "model": stats["model"],
                    "indexed": stats["indexed"],
                    "last_source_rowid": stats["last_source_rowid"],
                    "elapsed_seconds": stats["elapsed_seconds"],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.cmd == "search":
        try:
            res = search(
                args.query,
                chat_jid=args.chat,
                after=args.after,
                before=args.before,
                limit=args.limit,
            )
        except RagError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        # Privacy-safe summary: no message bodies, no JIDs.
        summary = {
            "fts_hits": res["fts_hits"],
            "vector_hits": res["vector_hits"],
            "vector_error": res["vector_error"],
            "windows": len(res["results"]),
            "window_sizes": [len(w["messages"]) for w in res["results"]],
            "scores": [w["score"] for w in res["results"]],
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
