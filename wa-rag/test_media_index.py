#!/usr/bin/env python3
"""Unit tests for media_index.py: stdlib only.

Same fixtures as test_rag.py: synthetic messages.db, fake embedder
(`OLLAMA_EMBED_URL=fake:`), injected download/extract functions. No network,
no real media, no PII.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

os.environ["OLLAMA_EMBED_URL"] = "fake:"

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import media_index  # noqa: E402
import rag  # noqa: E402


def _make_source_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TIMESTAMP
        );
        CREATE TABLE messages (
            id TEXT, chat_jid TEXT, sender TEXT, content TEXT,
            timestamp TIMESTAMP, is_from_me BOOLEAN,
            media_type TEXT, filename TEXT, url TEXT,
            media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB,
            file_length INTEGER,
            PRIMARY KEY (id, chat_jid)
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_media(conn, msg_id, chat_jid, media_type, content="", filename="f.jpg"):
    # Mismo formato TEXT con tz que escribe el bridge (time.Time serializado).
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S-06:00")
    cur = conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename) "
        "VALUES (?, ?, 'someone', ?, ?, 0, ?, ?)",
        (msg_id, chat_jid, content, ts, media_type, filename),
    )
    conn.commit()
    return cur.lastrowid


class MediaTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src_path = Path(self.tmp.name) / "messages.db"
        self.idx_path = Path(self.tmp.name) / "rag.db"
        _make_source_db(self.src_path)
        self.src = sqlite3.connect(str(self.src_path))
        self.fake_file = Path(self.tmp.name) / "media.bin"
        self.fake_file.write_bytes(b"fake")

    def tearDown(self):
        self.src.close()

    def sync(self, **kw):
        kw.setdefault("src_path", str(self.src_path))
        kw.setdefault("idx_path", str(self.idx_path))
        kw.setdefault("download_fn", lambda mid, jid: str(self.fake_file))
        kw.setdefault("extract_fn", lambda row, path: f"extracted:{row['media_type']}")
        kw.setdefault("log", False)
        return media_index.sync_media(**kw)


class TestSync(MediaTestBase):
    def test_indexes_media_row(self):
        _insert_media(self.src, "m1", "g@g.us", "image")
        stats = self.sync()
        self.assertEqual(stats["indexed"], 1)
        idx = sqlite3.connect(str(self.idx_path))
        row = idx.execute(
            "SELECT modality, caption, status FROM rag_media"
        ).fetchone()
        self.assertEqual(row[0], "image")
        self.assertIn("extracted:image", row[1])
        self.assertEqual(row[2], "ok")

    def test_skips_unlisted_modality(self):
        _insert_media(self.src, "m1", "g@g.us", "video")
        stats = self.sync()
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["indexed"], 0)

    def test_failed_download_advances_watermark(self):
        rid = _insert_media(self.src, "m1", "g@g.us", "image")
        stats = self.sync(download_fn=lambda mid, jid: None)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["last_source_rowid"], rid)
        # Segunda pasada no reintenta ni cuenta de nuevo.
        stats2 = self.sync(download_fn=lambda mid, jid: None)
        self.assertEqual(stats2["indexed"], 0)
        self.assertEqual(stats2["failed"], 0)

    def test_chat_scope_applies_before_download(self):
        _insert_media(self.src, "ok1", "allowed@g.us", "image")
        _insert_media(self.src, "no1", "denied@g.us", "image")
        calls = []

        def spy(mid, jid):
            calls.append(jid)
            return str(self.fake_file)

        self.sync(chats="allowed@g.us", download_fn=spy)
        self.assertEqual(calls, ["allowed@g.us"])

    def test_deny_file_blocks_download(self):
        deny = Path(self.tmp.name) / "never.jids"
        deny.write_text("denied@g.us\n", encoding="utf-8")
        _insert_media(self.src, "no1", "denied@g.us", "image")
        calls = []

        old = rag.DENY_FILE, media_index.rag.DENY_FILE
        try:
            rag.DENY_FILE = str(deny)
            self.sync(download_fn=lambda mid, jid: calls.append(jid) or None)
        finally:
            rag.DENY_FILE = old[0]
        self.assertEqual(calls, [])


class TestSearch(MediaTestBase):
    def test_media_search_returns_caption(self):
        _insert_media(self.src, "m1", "g@g.us", "image", content="flyer")
        self.sync()
        res = media_index.media_search("flyer", idx_path=str(self.idx_path))
        self.assertGreaterEqual(len(res["results"]), 0)
        row = sqlite3.connect(str(self.idx_path)).execute(
            "SELECT caption FROM rag_media"
        ).fetchone()
        self.assertIn("flyer", row[0])
        self.assertIn("[image", row[0])


if __name__ == "__main__":
    unittest.main()
