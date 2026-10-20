#!/usr/bin/env python3
"""Archive import: index extracted WhatsApp media files into rag.db.

Two phases so the heavy extraction runs wherever the files live:

    python archive_import.py extract <dir> --kind voice-notes|audio|documents|images \
        --out captions.jsonl
    python archive_import.py ingest captions.jsonl [--limit N]

extract walks a directory, classifies by extension, pulls searchable text
with local models (whisper for audio, pdftotext for docs, Ollama vision for
images) and writes JSONL, no rag.db needed on that machine. ingest runs on
the machine that holds rag.db, embedding each caption into the shared
rag_media index; message_id is arch:<sha256> so reruns dedupe naturally.

Everything local: whisper + pdftotext + loopback Ollama. Nothing leaves the
machine.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from typing import Any, Optional

VISION_URL = os.environ.get("WA_MEDIA_VISION_URL", "http://127.0.0.1:11434/api/generate")
VISION_MODEL = os.environ.get("WA_MEDIA_VISION_MODEL", "qwen3-vl:8b")
WHISPER_MODEL = os.environ.get("WA_MEDIA_WHISPER_MODEL", "turbo")
CAPTION_MAX_CHARS = int(os.environ.get("WA_MEDIA_CAPTION_MAX_CHARS", "4000"))
BATCH_EMBED = int(os.environ.get("WA_ARCHIVE_BATCH", "16"))

AUDIO_EXTS = {".opus", ".ogg", ".aac", ".mp3", ".m4a", ".wav", ".amr"}
DOC_EXTS = {".pdf", ".txt", ".md", ".docx"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

IMAGE_PROMPT = (
    "Describe la imagen en español, en una sola linea de maximo 60 palabras: "
    "que muestra, quien aparece si se nota, cualquier texto visible, y el "
    "contexto probable (meme, captura de pantalla, foto, documento, flyer)."
)

_WA_DATE_RE = re.compile(r"(?:IMG|PTT|AUD|VID|DOC)-(\d{4})(\d{2})(\d{2})-WA")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_ts(path: str) -> int:
    m = _WA_DATE_RE.search(os.path.basename(path))
    if m:
        try:
            return int(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp())
        except ValueError:
            pass
    return int(os.path.getmtime(path))


def _modality(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in DOC_EXTS:
        return "document"
    if ext in IMAGE_EXTS:
        return "image"
    return None


_whisper = None


def _transcribe(path: str) -> str:
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _ = _whisper.transcribe(path, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


def _doc_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in {".txt", ".md"}:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()[:20000].strip()
        except OSError:
            return ""
    if ext == ".pdf" and shutil.which("pdftotext"):
        try:
            out = subprocess.run(
                ["pdftotext", "-l", "10", path, "-"],
                capture_output=True, timeout=120, check=True,
            )
            return out.stdout.decode("utf-8", "replace").strip()
        except (subprocess.SubprocessError, OSError):
            return ""
    if ext == ".docx":
        try:
            import docx  # python-docx

            d = docx.Document(path)
            return "\n".join(p.text for p in d.paragraphs).strip()
        except Exception:
            return ""
    return ""


def _scaled_image(path: str) -> str:
    """Downscale to <=1568px on the long edge (vision-encoder friendly).
    Long screenshots and multi-MP photos stall the model; a copy at sane
    resolution captions the same and is much faster.
    """
    tool = "sips" if shutil.which("sips") else ("ffmpeg" if shutil.which("ffmpeg") else None)
    if not tool:
        return path
    tmp = os.path.join("/tmp", f"arch-cap-{os.getpid()}-{os.path.basename(path)}.jpg")
    try:
        if tool == "sips":
            subprocess.run(
                ["sips", "-Z", "1568", "-s", "format", "jpeg", path, "--out", tmp],
                capture_output=True, timeout=60, check=True,
            )
        else:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
                 "-vf", "scale='if(gt(iw,ih),min(1568,iw),-1)':'if(gt(iw,ih),-1,min(1568,ih))'",
                 tmp],
                capture_output=True, timeout=60, check=True,
            )
        return tmp if os.path.isfile(tmp) else path
    except (subprocess.SubprocessError, OSError):
        return path


def _caption(path: str) -> str:
    src = _scaled_image(path)
    with open(src, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    payload = json.dumps(
        {
            "model": VISION_MODEL,
            "prompt": IMAGE_PROMPT,
            "images": [b64],
            "stream": False,
            "think": False,
            # qwen3-vl thinks ~200 tokens before answering even with
            # think:false on ollama <=0.35; 600 covers think + caption.
            "options": {"num_predict": 600},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        VISION_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return (json.loads(resp.read().decode("utf-8")).get("response") or "").strip()


def _extract(path: str, modality: str) -> str:
    if modality == "audio":
        return _transcribe(path)
    if modality == "document":
        return _doc_text(path)
    if modality == "image":
        return _caption(path)
    return ""


def cmd_extract(args: argparse.Namespace) -> int:
    wanted = args.kind
    kind_map = {
        "voice-notes": {"audio"},
        "audio": {"audio"},
        "documents": {"document"},
        "images": {"image"},
        "all": {"audio", "document", "image"},
    }
    modalities = kind_map[wanted]
    done: set[str] = set()
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["sha"])
                except (ValueError, KeyError):
                    continue
    out = open(args.out, "a", encoding="utf-8")
    stats = {"extracted": 0, "skipped": 0, "failed": 0, "dupes": 0}
    seen: set[str] = set(done)
    try:
        for root, _dirs, files in os.walk(args.dir):
            for name in sorted(files):
                if name.startswith("."):
                    continue
                path = os.path.join(root, name)
                modality = _modality(path)
                if modality not in modalities:
                    continue
                sha = _sha256(path)
                if sha in seen:
                    stats["dupes"] += 1
                    continue
                seen.add(sha)
                try:
                    text = _extract(path, modality)
                except Exception as e:  # noqa: BLE001 - log and continue
                    print(f"  fail {name}: {e}", file=sys.stderr)
                    stats["failed"] += 1
                    continue
                if not text.strip():
                    stats["skipped"] += 1
                    continue
                rec = {
                    "sha": sha,
                    "path": path,
                    "filename": name,
                    "rel_dir": os.path.relpath(root, args.dir),
                    "ts": _file_ts(path),
                    "modality": modality,
                    "kind": wanted,
                    "text": text[:CAPTION_MAX_CHARS],
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                stats["extracted"] += 1
                if stats["extracted"] % 25 == 0:
                    print(f"  extract {stats}", file=sys.stderr)
                if args.limit and stats["extracted"] >= args.limit:
                    break
            if args.limit and stats["extracted"] >= args.limit:
                break
    finally:
        out.close()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    import rag
    import media_index

    idx = media_index._open_index(args.db or media_index.INDEX_DB)
    stats = {"indexed": 0, "dupes": 0, "failed": 0}
    pending: list[dict[str, Any]] = []
    model = media_index._media_model_key()
    existing = {
        r[0]
        for r in idx.execute(
            "SELECT message_id FROM rag_media WHERE model = ? AND message_id LIKE 'arch:%'",
            (model,),
        )
    }

    def flush() -> None:
        if not pending:
            return
        vecs = rag.embed_batch([d["caption"] for d in pending])
        media_index._persist_media(idx, pending, vecs)
        idx.commit()
        stats["indexed"] += len(pending)
        if stats["indexed"] % 64 < BATCH_EMBED:
            print(f"  ingest {stats}", file=sys.stderr)
        pending.clear()

    try:
        with open(args.jsonl, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    stats["failed"] += 1
                    continue
                mid = f"arch:{rec['sha'][:40]}"
                if mid in existing:
                    stats["dupes"] += 1
                    continue
                existing.add(mid)
                caption = f"[archive {rec['kind']} {rec['filename']}] {rec['text']}"[:CAPTION_MAX_CHARS]
                pending.append(
                    {
                        "rowid": 0,
                        "message_id": mid,
                        "chat_jid": f"archive:{rec['kind']}",
                        "sender": "",
                        "timestamp": int(rec["ts"]),
                        "media_type": rec["modality"],
                        "file_path": rec["path"],
                        "caption": caption,
                    }
                )
                if len(pending) >= BATCH_EMBED:
                    flush()
                if args.limit and stats["indexed"] >= args.limit:
                    break
        flush()
    finally:
        idx.close()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


def _cli() -> int:
    p = argparse.ArgumentParser(description="wa-rag archive media importer")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="walk dir, emit captions JSONL")
    e.add_argument("dir")
    e.add_argument("--kind", required=True,
                   choices=["voice-notes", "audio", "documents", "images", "all"])
    e.add_argument("--out", required=True, help="jsonl output (appended, resumable)")
    e.add_argument("--limit", type=int, default=None)
    e.set_defaults(fn=cmd_extract)

    i = sub.add_parser("ingest", help="embed JSONL captions into rag.db")
    i.add_argument("jsonl")
    i.add_argument("--db", default=None, help="index db path (default: media_index.INDEX_DB)")
    i.add_argument("--limit", type=int, default=None)
    i.set_defaults(fn=cmd_ingest)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(_cli())
