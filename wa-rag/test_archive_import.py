#!/usr/bin/env python3
"""Unit tests for archive_import.py: stdlib only.

Extract/ingest run against a tempdir with injected extract results and the
fake embedder (`OLLAMA_EMBED_URL=fake:`). No whisper, no Ollama, no PII.
"""
from __future__ import annotations

import argparse
import json
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
import archive_import as ai  # noqa: E402


def _ns(**kw):
    return argparse.Namespace(**kw)


class TestClassify(unittest.TestCase):
    def test_modality_by_ext(self):
        self.assertEqual(ai._modality("x/PTT-20230101-WA0001.opus"), "audio")
        self.assertEqual(ai._modality("x/DOC-20230101-WA0001.pdf"), "document")
        self.assertEqual(ai._modality("x/IMG-20230101-WA0001.JPG"), "image")
        self.assertIsNone(ai._modality("x/VID-20230101-WA0001.mp4"))

    def test_file_ts_from_wa_name(self):
        ts = ai._file_ts("/a/IMG-20230315-WA0007.jpg")
        self.assertEqual(datetime.fromtimestamp(ts).date().isoformat(), "2023-03-15")

    def test_file_ts_fallback_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "random.bin"
            p.write_bytes(b"x")
            self.assertEqual(ai._file_ts(str(p)), int(p.stat().st_mtime))


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "media"
        self.root.mkdir()
        self.out = Path(self.tmp.name) / "out.jsonl"
        self._old_extract = ai._extract
        ai._extract = lambda path, modality: f"text:{modality}:{Path(path).name}"
        self.addCleanup(setattr, ai, "_extract", self._old_extract)

    def _run(self, kind="all"):
        args = _ns(dir=str(self.root), kind=kind, out=str(self.out), limit=None)
        return ai.cmd_extract(args)

    def _records(self):
        return [json.loads(l) for l in self.out.read_text().splitlines()]

    def test_emits_records_with_provenance(self):
        (self.root / "AUD-20230803-WA0005.opus").write_bytes(b"a")
        self.assertEqual(self._run("voice-notes"), 0)
        rec = self._records()[0]
        self.assertEqual(rec["kind"], "voice-notes")
        self.assertEqual(rec["modality"], "audio")
        self.assertEqual(rec["text"], "text:audio:AUD-20230803-WA0005.opus")
        self.assertEqual(len(rec["sha"]), 64)

    def test_sha_dedupes_identical_content(self):
        blob = b"same bytes"
        (self.root / "IMG-20230101-WA0001.jpg").write_bytes(blob)
        (self.root / "IMG-20230102-WA0002.jpg").write_bytes(blob)
        self._run("images")
        self.assertEqual(len(self._records()), 1)

    def test_resume_skips_written_shas(self):
        (self.root / "a.opus").write_bytes(b"a")
        self._run()
        (self.root / "b.opus").write_bytes(b"b")
        self._run()
        texts = [r["text"] for r in self._records()]
        self.assertEqual(texts, ["text:audio:a.opus", "text:audio:b.opus"])

    def test_empty_text_not_recorded(self):
        ai._extract = lambda path, modality: "   "
        (self.root / "x.opus").write_bytes(b"x")
        self._run()
        self.assertFalse(self.out.exists() and self._records())


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "rag.db"
        self.jsonl = Path(self.tmp.name) / "in.jsonl"
        rec = {
            "sha": "ab" * 32,
            "path": "/m/f.opus",
            "filename": "PTT-20230101-WA0001.opus",
            "rel_dir": ".",
            "ts": 1672531200,
            "modality": "audio",
            "kind": "voice-notes",
            "text": "hola que tal",
        }
        self.jsonl.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    def _run(self):
        args = _ns(jsonl=str(self.jsonl), db=str(self.db), limit=None)
        return ai.cmd_ingest(args)

    def test_indexes_arch_record(self):
        self.assertEqual(self._run(), 0)
        row = sqlite3.connect(str(self.db)).execute(
            "SELECT message_id, chat_jid, modality, caption FROM rag_media"
        ).fetchone()
        self.assertTrue(row[0].startswith("arch:"))
        self.assertEqual(row[1], "archive:voice-notes")
        self.assertEqual(row[2], "audio")
        self.assertIn("hola que tal", row[3])
        self.assertIn("PTT-20230101-WA0001.opus", row[3])

    def test_reingest_dedupes(self):
        self._run()
        args = _ns(jsonl=str(self.jsonl), db=str(self.db), limit=None)
        # fresh pass sees the same mid already persisted
        n = sqlite3.connect(str(self.db)).execute(
            "SELECT COUNT(*) FROM rag_media"
        ).fetchone()[0]
        ai.cmd_ingest(args)
        n2 = sqlite3.connect(str(self.db)).execute(
            "SELECT COUNT(*) FROM rag_media"
        ).fetchone()[0]
        self.assertEqual(n, n2)


if __name__ == "__main__":
    unittest.main()
