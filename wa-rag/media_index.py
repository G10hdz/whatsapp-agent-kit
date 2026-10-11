#!/usr/bin/env python3
"""Media indexing for wa-rag: images, audio and documents -> searchable text.

Downloads media through the local bridge's /api/download, extracts searchable
text with local models (caption via Ollama vision, transcript via
faster-whisper, text layer via pdftotext) and indexes it into rag.db next to
the text index, in the same 768-dim embedding space.

Privacy: same rules as rag.py. The chat allowlist/denylist applies BEFORE any
download — media from denied chats is never fetched. Everything runs on
loopback or local models; no content leaves the machine.

Usage:
    python media_index.py sync [--all] [--limit N]
    python media_index.py search "query" [--chat JID] [--after ISO] [--before ISO]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import rag

SOURCE_DB = rag.SOURCE_DB
INDEX_DB = rag.INDEX_DB
EMBED_MODEL = rag.EMBED_MODEL

BRIDGE_URL = os.environ.get("WA_MEDIA_BRIDGE_URL", "http://127.0.0.1:8080")
VISION_URL = os.environ.get("WA_MEDIA_VISION_URL", "http://127.0.0.1:11434/api/generate")
VISION_MODEL = os.environ.get("WA_MEDIA_VISION_MODEL", "qwen3-vl:8b")
WHISPER_MODEL = os.environ.get("WA_MEDIA_WHISPER_MODEL", "small")
MEDIA_TYPES = {
    t.strip() for t in os.environ.get("WA_MEDIA_TYPES", "image,audio,document").split(",")
    if t.strip()
}
# WhatsApp media URLs caducan: descargar historico antiguo es churn inutil.
MAX_AGE_DAYS = int(os.environ.get("WA_MEDIA_MAX_AGE_DAYS", "14"))
# Cada media indexado materializa un archivo; frenar antes de llenar el disco.
MIN_FREE_GB = float(os.environ.get("WA_MEDIA_MIN_FREE_GB", "5"))
BATCH_SIZE = int(os.environ.get("WA_MEDIA_BATCH_SIZE", "8"))
CAPTION_MAX_CHARS = int(os.environ.get("WA_MEDIA_CAPTION_MAX_CHARS", "4000"))
EXTRACTOR_VERSION = "media-v1"

IMAGE_PROMPT = (
    "Describe la imagen en español, en una sola linea de maximo 60 palabras: "
    "que muestra, quien aparece si se nota, cualquier texto visible, y el "
    "contexto probable (meme, captura de pantalla, foto, documento, flyer)."
)


def _loopback_only(url: str) -> None:
    import re

    m = re.match(r"^https?://([^/:]+)", url)
    if not m or m.group(1) not in rag._LOOPBACK_HOSTS:
        raise rag.RagError(f"media endpoints must be loopback, got {url!r}")


def _init_media_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rag_media (
            media_doc_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            source_rowid  INTEGER NOT NULL,
            message_id    TEXT NOT NULL,
            chat_jid      TEXT NOT NULL,
            sender        TEXT,
            timestamp     INTEGER NOT NULL,
            modality      TEXT NOT NULL,
            file_path     TEXT,
            caption       TEXT NOT NULL,
            status        TEXT NOT NULL,
            content_hash  TEXT NOT NULL,
            model         TEXT NOT NULL,
            embedding     BLOB,
            UNIQUE(message_id, chat_jid, model)
        );
        CREATE INDEX IF NOT EXISTS idx_rag_media_rowid
            ON rag_media(model, source_rowid);
        CREATE INDEX IF NOT EXISTS idx_rag_media_chat_time
            ON rag_media(chat_jid, timestamp);

        CREATE VIRTUAL TABLE IF NOT EXISTS rag_media_fts USING fts5(
            caption, sender, modality UNINDEXED,
            chat_jid UNINDEXED, message_id UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TABLE IF NOT EXISTS rag_media_state (
            model            TEXT PRIMARY KEY,
            last_source_rowid INTEGER NOT NULL DEFAULT 0,
            updated_at       INTEGER NOT NULL
        );
        """
    )
    conn.commit()


def _open_index(path: str) -> sqlite3.Connection:
    conn = rag.open_index(path)
    _init_media_schema(conn)
    return conn


def _media_model_key() -> str:
    return f"{EXTRACTOR_VERSION}|{EMBED_MODEL}"


def _get_watermark(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT last_source_rowid FROM rag_media_state WHERE model = ?",
        (_media_model_key(),),
    ).fetchone()
    return int(row[0]) if row else 0


def _set_watermark(conn: sqlite3.Connection, rowid: int) -> None:
    conn.execute(
        "INSERT INTO rag_media_state(model, last_source_rowid, updated_at) "
        "VALUES (?, ?, ?) ON CONFLICT(model) DO UPDATE SET "
        "last_source_rowid = excluded.last_source_rowid, "
        "updated_at = excluded.updated_at",
        (_media_model_key(), rowid, int(time.time())),
    )


def _fetch_media_batch(
    src: sqlite3.Connection,
    after_rowid: int,
    limit: int,
    chats: str,
    exclude_chats: str,
    max_age_days: int = MAX_AGE_DAYS,
) -> list[dict[str, Any]]:
    """Media rows under the SAME scope filter as text indexing.

    El filtro se aplica en la consulta, antes de tocar el bridge: un chat
    vetado no genera ni siquiera un download.
    """
    filter_sql, filter_params = rag._chat_filter_sql(chats, exclude_chats)
    # El bridge guarda timestamp como TEXT 'YYYY-MM-DD HH:MM:SS±HH:MM'. El
    # cutoff compara el prefijo ISO lexicografico; un cutoff int jamas filtraria
    # (INTEGER < TEXT siempre en sqlite).
    cutoff = ""
    if max_age_days > 0:
        cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    rows = src.execute(
        "SELECT rowid, id, chat_jid, sender, content, timestamp, media_type, filename "
        "FROM messages "
        "WHERE rowid > ? AND media_type IS NOT NULL AND media_type <> '' "
        "AND substr(timestamp, 1, 19) >= ? "
        + filter_sql
        + "ORDER BY rowid ASC LIMIT ?",
        (after_rowid, cutoff, *filter_params, limit),
    ).fetchall()
    return [
        {
            "rowid": r[0],
            "message_id": r[1],
            "chat_jid": r[2],
            "sender": r[3] or "",
            "content": rag.preprocess(r[4] or ""),
            "timestamp": rag._to_epoch(r[5]),
            "media_type": r[6],
            "filename": r[7] or "",
        }
        for r in rows
    ]


def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Fetch one media file via the bridge API. Returns local path or None."""
    _loopback_only(BRIDGE_URL)
    payload = json.dumps(
        {"message_id": message_id, "chat_jid": chat_jid}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{BRIDGE_URL}/api/download",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None
    if not body.get("success") or not body.get("path"):
        return None
    path = body["path"]
    return path if os.path.isfile(path) else None


def caption_image(path: str) -> str:
    """Caption one image via the local vision model. Raises on failure."""
    _loopback_only(VISION_URL)
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    payload = json.dumps(
        {
            "model": VISION_MODEL,
            "prompt": IMAGE_PROMPT,
            "images": [b64],
            "stream": False,
            "think": False,
            "options": {"num_predict": 600},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        VISION_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return (body.get("response") or "").strip()


_whisper = None


def transcribe_audio(path: str) -> str:
    """Transcribe one audio file locally with faster-whisper."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _ = _whisper.transcribe(path, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


def extract_document(path: str) -> str:
    """Text layer of a document via pdftotext; empty if unavailable/fails."""
    if not shutil.which("pdftotext"):
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-l", "10", path, "-"],
            capture_output=True, timeout=120, check=True,
        )
        return out.stdout.decode("utf-8", "replace").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _extract_text(row: dict[str, Any], path: str) -> str:
    mt = row["media_type"]
    if mt == "image":
        return caption_image(path)
    if mt == "audio":
        return transcribe_audio(path)
    if mt == "document":
        return extract_document(path)
    return ""


def _doc_caption(row: dict[str, Any], extracted: str) -> str:
    """Searchable text for a media doc: modality, filename, caption, extracted."""
    parts = [f"[{row['media_type']}"]
    if row["filename"]:
        parts.append(f" {row['filename']}")
    parts.append("]")
    head = "".join(parts)
    if row["content"]:
        head += " " + row["content"]
    return (head + " " + extracted).strip()[:CAPTION_MAX_CHARS]


def sync_media(
    *,
    src_path: str = SOURCE_DB,
    idx_path: str = INDEX_DB,
    all_rows: bool = False,
    limit: Optional[int] = None,
    chats: str = rag.CHATS,
    exclude_chats: str = rag.EXCLUDE_CHATS,
    download_fn: Callable[..., Optional[str]] = download_media,
    extract_fn: Optional[Callable[..., str]] = None,
    log: bool = True,
) -> dict[str, Any]:
    """Index media messages into rag_media. Extractors injectable for tests."""
    t0 = time.time()
    extract_fn = extract_fn or _extract_text
    src = rag.open_source(src_path)
    try:
        idx = _open_index(idx_path)
        try:
            start_rowid = 0 if all_rows else _get_watermark(idx)
            deny = ",".join(rag.load_deny_jids())
            idx.execute(
                "INSERT INTO rag_meta(key, value) VALUES ('media_chat_filter', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"{chats}|{exclude_chats}|{deny}",),
            )
            idx.commit()

            stats = {"indexed": 0, "skipped": 0, "failed": 0}
            last_rowid = start_rowid
            batch = _fetch_media_batch(src, last_rowid, BATCH_SIZE, chats, exclude_chats)
            pending_docs: list[dict[str, Any]] = []
            while batch:
                if (
                    MIN_FREE_GB > 0
                    and shutil.disk_usage(os.path.dirname(idx_path) or ".").free
                    < MIN_FREE_GB * (1 << 30)
                ):
                    if log:
                        print("disk below WA_MEDIA_MIN_FREE_GB, stopping", file=sys.stderr)
                    break
                for row in batch:
                    last_rowid = row["rowid"]
                    if limit is not None and stats["indexed"] >= limit:
                        break
                    if row["media_type"] not in MEDIA_TYPES:
                        stats["skipped"] += 1
                        continue
                    path = download_fn(row["message_id"], row["chat_jid"])
                    if not path:
                        stats["failed"] += 1
                        continue
                    try:
                        extracted = extract_fn(row, path)
                    except Exception:
                        stats["failed"] += 1
                        continue
                    caption = _doc_caption(row, extracted)
                    if not caption:
                        stats["skipped"] += 1
                        continue
                    pending_docs.append(
                        {**row, "file_path": path, "caption": caption}
                    )
                docs = pending_docs
                pending_docs = []
                if docs:
                    vecs = rag.embed_batch([d["caption"] for d in docs])
                    _persist_media(idx, docs, vecs)
                    stats["indexed"] += len(docs)
                _set_watermark(idx, last_rowid)
                idx.commit()
                if log:
                    print(
                        f"  media_indexed={stats['indexed']} "
                        f"skipped={stats['skipped']} failed={stats['failed']} "
                        f"last_rowid={last_rowid}",
                        file=sys.stderr,
                    )
                if limit is not None and stats["indexed"] >= limit:
                    break
                batch = _fetch_media_batch(src, last_rowid, BATCH_SIZE, chats, exclude_chats)
            return {
                "model": _media_model_key(),
                "last_source_rowid": last_rowid,
                "elapsed_seconds": round(time.time() - t0, 3),
                **stats,
            }
        finally:
            idx.close()
    finally:
        src.close()


def _persist_media(
    conn: sqlite3.Connection,
    docs: list[dict[str, Any]],
    vecs: list[list[float]],
) -> None:
    model = _media_model_key()
    for doc, vec in zip(docs, vecs):
        conn.execute(
            """
            INSERT INTO rag_media (
                source_rowid, message_id, chat_jid, sender, timestamp,
                modality, file_path, caption, status, content_hash, model, embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?)
            ON CONFLICT(message_id, chat_jid, model) DO UPDATE SET
                source_rowid = excluded.source_rowid,
                caption = excluded.caption,
                file_path = excluded.file_path,
                status = excluded.status,
                embedding = excluded.embedding
            """,
            (
                doc["rowid"], doc["message_id"], doc["chat_jid"], doc["sender"],
                doc["timestamp"], doc["media_type"], doc["file_path"],
                doc["caption"], rag.content_hash(doc["caption"]), model,
                rag.vec_to_blob(vec),
            ),
        )
        row = conn.execute(
            "SELECT media_doc_id FROM rag_media "
            "WHERE message_id = ? AND chat_jid = ? AND model = ?",
            (doc["message_id"], doc["chat_jid"], model),
        ).fetchone()
        conn.execute("DELETE FROM rag_media_fts WHERE rowid = ?", (row[0],))
        conn.execute(
            "INSERT INTO rag_media_fts(rowid, caption, sender, modality, chat_jid, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row[0], doc["caption"], doc["sender"], doc["media_type"],
             doc["chat_jid"], doc["message_id"]),
        )


def media_search(
    query: str,
    *,
    chat_jid: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = rag.FINAL_LIMIT_DEFAULT,
    idx_path: str = INDEX_DB,
) -> dict[str, Any]:
    """Hybrid FTS+vector search over media captions. Same shape as rag.search."""
    q = query.strip()
    if not q:
        raise rag.RagError("query must not be empty")
    after_ts = rag._parse_iso(after)
    before_ts = rag._parse_iso(before)
    idx = _open_index(idx_path)
    try:
        fts_q = rag._build_fts_query(q)
        sql = (
            "SELECT f.rowid, bm25(rag_media_fts) AS score "
            "FROM rag_media_fts f JOIN rag_media d ON d.media_doc_id = f.rowid "
            "WHERE rag_media_fts MATCH ? AND d.status = 'ok' "
        )
        params: list[Any] = [fts_q]
        if chat_jid:
            sql += "AND d.chat_jid = ? "
            params.append(chat_jid)
        if after_ts is not None:
            sql += "AND d.timestamp >= ? "
            params.append(after_ts)
        if before_ts is not None:
            sql += "AND d.timestamp <= ? "
            params.append(before_ts)
        sql += "ORDER BY score ASC LIMIT ?"
        params.append(rag.FTS_CANDIDATES)
        fts_hits = [(int(r[0]), float(r[1])) for r in idx.execute(sql, params).fetchall()] if fts_q else []

        vec_hits: list[tuple[int, float]] = []
        vec_error = None
        try:
            qvec = rag.embed_batch([q])[0]
            rows = idx.execute(
                "SELECT media_doc_id, embedding FROM rag_media "
                "WHERE status = 'ok' AND embedding IS NOT NULL"
            ).fetchall()
            scored = []
            for doc_id, blob in rows:
                try:
                    scored.append((rag._dot(qvec, rag.blob_to_vec(blob)), int(doc_id)))
                except rag.RagError:
                    continue
            scored.sort(reverse=True)
            vec_hits = [(d, s) for s, d in scored[: rag.VECTOR_CANDIDATES]]
        except rag.RagError as e:
            vec_error = str(e)

        fused = rag._rrf_fuse(fts_hits, vec_hits, k=rag.RRF_K, limit=limit)
        results = []
        for doc_id, score in fused:
            r = idx.execute(
                "SELECT message_id, chat_jid, sender, timestamp, modality, "
                "file_path, caption FROM rag_media WHERE media_doc_id = ?",
                (doc_id,),
            ).fetchone()
            if not r:
                continue
            results.append(
                {
                    "score": round(float(score), 6),
                    "message_id": r[0],
                    "chat_jid": r[1],
                    "sender": r[2] or "",
                    "timestamp": int(r[3]),
                    "modality": r[4],
                    "file_path": r[5],
                    "caption": r[6],
                }
            )
        return {
            "query": q,
            "model": _media_model_key(),
            "fts_hits": len(fts_hits),
            "vector_hits": len(vec_hits),
            "vector_error": vec_error,
            "results": results,
        }
    finally:
        idx.close()


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="wa-rag media indexer (read-only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="Index media from messages.db into rag.db")
    s.add_argument("--all", action="store_true", help="re-index all media rows")
    s.add_argument("--limit", type=int, default=None)

    q = sub.add_parser("search", help="Hybrid search over media captions")
    q.add_argument("query")
    q.add_argument("--chat", default=None)
    q.add_argument("--after", default=None)
    q.add_argument("--before", default=None)
    q.add_argument("--limit", type=int, default=rag.FINAL_LIMIT_DEFAULT)

    args = parser.parse_args(argv)

    if args.cmd == "sync":
        stats = sync_media(all_rows=args.all, limit=args.limit)
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    if args.cmd == "search":
        try:
            res = media_search(
                args.query, chat_jid=args.chat, after=args.after,
                before=args.before, limit=args.limit,
            )
        except rag.RagError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        summary = {
            "fts_hits": res["fts_hits"],
            "vector_hits": res["vector_hits"],
            "vector_error": res["vector_error"],
            "results": res["results"],
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
