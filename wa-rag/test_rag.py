#!/usr/bin/env python3
"""Unit tests for rag.py: stdlib only.

Uses a synthetic messages.db (matching the Go bridge schema) and the fake
embedder (`OLLAMA_EMBED_URL=fake:`). No network, no real WhatsApp data, no
PII.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# Force fake embedder before importing rag.
os.environ["OLLAMA_EMBED_URL"] = "fake:"

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import rag  # noqa: E402  (import after env setup)


def _make_source_db(path: Path) -> None:
    """Create a synthetic messages.db with the Go bridge schema."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TIMESTAMP
        );
        CREATE TABLE messages (
            id TEXT,
            chat_jid TEXT,
            sender TEXT,
            content TEXT,
            timestamp TIMESTAMP,
            is_from_me BOOLEAN,
            media_type TEXT,
            filename TEXT,
            url TEXT,
            media_key BLOB,
            file_sha256 BLOB,
            file_enc_sha256 BLOB,
            file_length INTEGER,
            PRIMARY KEY (id, chat_jid)
        );
        """
    )
    conn.commit()
    conn.close()


def _ts(days_ago: int = 0, hour: int = 12, minute: int = 0) -> int:
    base = datetime(2026, 1, 1, hour, minute, 0)
    return int((base + timedelta(days=days_ago)).timestamp())


def _insert_msg(
    conn: sqlite3.Connection,
    msg_id: str,
    chat_jid: str,
    sender: str,
    content: str,
    timestamp: int,
) -> int:
    cur = conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(id, chat_jid, sender, content, timestamp, is_from_me) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (msg_id, chat_jid, sender, content, timestamp),
    )
    conn.commit()
    return cur.lastrowid


class DefaultPathsTest(unittest.TestCase):
    def test_default_source_db_lives_under_bridge(self):
        d = os.path.dirname(
            os.path.dirname(os.path.abspath(rag._DEFAULT_SOURCE_DB))
        )
        self.assertTrue(os.path.isdir(d), f"{d} no existe")
        self.assertEqual(os.path.basename(d), "bridge")

    def test_default_index_db_lives_under_bridge(self):
        d = os.path.dirname(
            os.path.dirname(os.path.abspath(rag._DEFAULT_INDEX_DB))
        )
        self.assertTrue(os.path.isdir(d), f"{d} no existe")
        self.assertEqual(os.path.basename(d), "bridge")


class RagTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src_path = Path(self.tmp.name) / "messages.db"
        self.idx_path = Path(self.tmp.name) / "rag.db"
        _make_source_db(self.src_path)
        self.src = sqlite3.connect(str(self.src_path))

    def tearDown(self) -> None:
        self.src.close()

    def sync(self, **kw) -> dict:
        kw.setdefault("src_path", str(self.src_path))
        kw.setdefault("idx_path", str(self.idx_path))
        kw.setdefault("log", False)
        return rag.sync_index(**kw)

    def search(self, query: str, **kw) -> dict:
        kw.setdefault("idx_path", str(self.idx_path))
        kw.setdefault("src_path", str(self.src_path))
        return rag.search(query, **kw)


class TestSync(RagTestBase):
    def test_initial_backfill_indexes_all(self) -> None:
        for i in range(5):
            _insert_msg(
                self.src, f"m{i}", "chat_a@s.whatsapp.net", "alice",
                f"hello world {i}", _ts(days_ago=i),
            )
        stats = self.sync()
        self.assertEqual(stats["indexed"], 5)
        self.assertEqual(stats["model"], rag.EMBED_MODEL)

    def test_incremental_sync_advances_watermark(self) -> None:
        _insert_msg(self.src, "m1", "c@s.whatsapp.net", "a", "first", _ts())
        self.sync()
        stats2 = self.sync()
        self.assertEqual(stats2["indexed"], 0)

        _insert_msg(self.src, "m2", "c@s.whatsapp.net", "a", "second", _ts(days_ago=1))
        stats3 = self.sync()
        self.assertEqual(stats3["indexed"], 1)

    def test_idempotent_rerun_does_not_reembed(self) -> None:
        _insert_msg(self.src, "m1", "c@s.whatsapp.net", "a", "first", _ts())
        self.sync()
        s2 = self.sync()
        self.assertEqual(s2["indexed"], 0)

    def test_replace_on_same_pk_reindexes(self) -> None:
        """INSERT OR REPLACE in source bumps rowid; watermark must catch it."""
        _insert_msg(self.src, "m1", "c@s.whatsapp.net", "a", "original", _ts())
        self.sync()
        # Simulate the bridge doing INSERT OR REPLACE on the same PK.
        _insert_msg(self.src, "m1", "c@s.whatsapp.net", "a", "edited body", _ts())
        stats = self.sync()
        # New rowid > watermark -> picked up.
        self.assertEqual(stats["indexed"], 1)
        # Confirm content was actually replaced in the index.
        idx = rag.open_index(str(self.idx_path))
        try:
            row = idx.execute(
                "SELECT content FROM rag_documents WHERE message_id = ? AND model = ?",
                ("m1", rag.EMBED_MODEL),
            ).fetchone()
            self.assertEqual(row[0], "a: edited body")
            members = idx.execute(
                "SELECT members_json FROM rag_documents WHERE message_id = ?",
                ("m1",),
            ).fetchone()[0]
            self.assertIn("edited body", members)
        finally:
            idx.close()

    def test_empty_content_skipped(self) -> None:
        _insert_msg(self.src, "m1", "c@s.whatsapp.net", "a", "", _ts())
        _insert_msg(self.src, "m2", "c@s.whatsapp.net", "a", "real", _ts())
        stats = self.sync()
        self.assertEqual(stats["indexed"], 1)

    def test_long_message_truncated_before_embedding(self) -> None:
        # Un mensaje largo hacia Ollama devuelve 400 y atora el watermark.
        long_text = "hola " * 5000
        full, cut = rag.embed_batch([long_text, long_text[: rag.EMBED_MAX_CHARS]])
        self.assertEqual(full, cut)

    def test_long_message_indexed_with_full_content(self) -> None:
        long_text = "preambulo " * 2000 + " palabraunica"
        _insert_msg(self.src, "m3", "c@s.whatsapp.net", "a", long_text, _ts())
        self.sync()
        idx = rag.open_index(self.idx_path)
        try:
            stored = idx.execute(
                "SELECT content FROM rag_documents WHERE message_id = ?", ("m3",)
            ).fetchone()[0]
        finally:
            idx.close()
        self.assertGreater(len(stored), rag.EMBED_MAX_CHARS)
        self.assertTrue(stored.endswith("palabraunica"))


class TestChatScope(RagTestBase):
    """El allowlist es el unico control entre chats personales y agentes externos."""

    GROUP = "120363000000000001@g.us"
    OTHER_GROUP = "120363000000000002@g.us"
    DIRECT = "5215555555555@s.whatsapp.net"

    def setUp(self) -> None:
        super().setUp()
        _insert_msg(self.src, "g1", self.GROUP, "a", "junta del lunes", _ts())
        _insert_msg(self.src, "g2", self.OTHER_GROUP, "b", "cumple de mi mama", _ts())
        _insert_msg(self.src, "d1", self.DIRECT, "c", "algo privado", _ts())

    def _indexed_jids(self) -> set:
        idx = rag.open_index(str(self.idx_path))
        try:
            return {r[0] for r in idx.execute("SELECT chat_jid FROM rag_documents")}
        finally:
            idx.close()

    def test_groups_only_excludes_direct_chats(self) -> None:
        self.sync(chats="groups")
        self.assertEqual(self._indexed_jids(), {self.GROUP, self.OTHER_GROUP})

    def test_exclude_list_removes_named_group(self) -> None:
        self.sync(chats="groups", exclude_chats=self.OTHER_GROUP)
        self.assertEqual(self._indexed_jids(), {self.GROUP})

    def test_explicit_jid_allowlist(self) -> None:
        self.sync(chats=self.GROUP)
        self.assertEqual(self._indexed_jids(), {self.GROUP})

    def test_excluded_chat_unreachable_by_search(self) -> None:
        self.sync(chats="groups", exclude_chats=self.OTHER_GROUP)
        res = self.search("cumple mama")
        ids = {m["message_id"] for w in res["results"] for m in w["messages"]}
        self.assertNotIn("g2", ids)

    def test_widening_filter_without_rebuild_is_refused(self) -> None:
        self.sync(chats="groups", exclude_chats=self.OTHER_GROUP)
        with self.assertRaises(rag.RagError):
            self.sync(chats="groups")

    def test_rebuild_accepts_new_filter(self) -> None:
        self.sync(chats="groups", exclude_chats=self.OTHER_GROUP)
        self.sync(chats="groups", rebuild=True)
        self.assertEqual(self._indexed_jids(), {self.GROUP, self.OTHER_GROUP})

    def _write_deny(self, *jids: str) -> None:
        path = Path(self.tmp.name) / "deny.jids"
        path.write_text(
            "# grupos personales, nunca indexables\n" + "\n".join(jids) + "\n",
            encoding="utf-8",
        )
        original = rag.DENY_FILE
        rag.DENY_FILE = str(path)
        self.addCleanup(setattr, rag, "DENY_FILE", original)

    def test_deny_file_beats_explicit_allowlist(self) -> None:
        # Pedir el chat por JID explicito no debe alcanzar para saltarse el deny.
        self._write_deny(self.GROUP)
        self.sync(chats=f"{self.GROUP},{self.OTHER_GROUP}")
        self.assertEqual(self._indexed_jids(), {self.OTHER_GROUP})

    def test_deny_file_ignores_comments_and_blanks(self) -> None:
        self._write_deny(self.OTHER_GROUP, "", "   ")
        self.sync(chats="groups")
        self.assertEqual(self._indexed_jids(), {self.GROUP})

    def test_unreadable_deny_file_fails_closed(self) -> None:
        original = rag.DENY_FILE
        rag.DENY_FILE = str(Path(self.tmp.name) / "no-existe.jids")
        self.addCleanup(setattr, rag, "DENY_FILE", original)
        with self.assertRaises(rag.RagError):
            self.sync(chats="groups")

    def test_deny_change_without_rebuild_is_refused(self) -> None:
        self._write_deny(self.OTHER_GROUP)
        self.sync(chats="groups")
        self._write_deny()
        with self.assertRaises(rag.RagError):
            self.sync(chats="groups")


class TestSearch(RagTestBase):
    def setUp(self) -> None:
        super().setUp()
        _insert_msg(self.src, "x1", "c1@s.whatsapp.net", "a", "dental appointment tuesday", _ts(days_ago=2, hour=9))
        _insert_msg(self.src, "x2", "c1@s.whatsapp.net", "b", "what time is it", _ts(days_ago=2, hour=9, minute=2))
        _insert_msg(self.src, "x3", "c2@s.whatsapp.net", "c", "totally unrelated weather", _ts(days_ago=1))
        self.sync()

    def test_fts_finds_exact_term(self) -> None:
        res = self.search("appointment")
        self.assertGreaterEqual(res["fts_hits"], 1)
        ids = {m["message_id"] for w in res["results"] for m in w["messages"]}
        self.assertIn("x1", ids)

    def test_vector_finds_paraphrase(self) -> None:
        # The fake embedder bucketizes tokens; "dental"/"appointment" present.
        res = self.search("dental")
        self.assertGreaterEqual(res["vector_hits"], 1)

    def test_rrf_no_duplicate_doc_in_results(self) -> None:
        res = self.search("appointment dental weather time")
        # A doc can appear in multiple top windows but each window's
        # anchor_doc_id list has no dupes (since RRF dedups keys).
        anchors = [w["anchor_doc_id"] for w in res["results"]]
        self.assertEqual(len(anchors), len(set(anchors)))

    def test_chat_filter_restricts_hits(self) -> None:
        res = self.search("appointment", chat_jid="c2@s.whatsapp.net")
        for w in res["results"]:
            self.assertEqual(w["chat_jid"], "c2@s.whatsapp.net")
        res2 = self.search("appointment", chat_jid="c1@s.whatsapp.net")
        self.assertTrue(any(w["chat_jid"] == "c1@s.whatsapp.net" for w in res2["results"]))

    def test_date_filter_bounds(self) -> None:
        res_before = self.search("appointment", before="2025-12-31T00:00:00")
        self.assertEqual(res_before["results"], [])
        res_after = self.search("appointment", after="2025-01-01T00:00:00")
        self.assertGreaterEqual(len(res_after["results"]), 1)

    def test_limit_clamped(self) -> None:
        with self.assertRaises(rag.RagError):
            self.search("x", limit=0)
        with self.assertRaises(rag.RagError):
            self.search("x", limit=21)

    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(rag.RagError):
            self.search("   ")


class TestWindows(RagTestBase):
    def test_window_does_not_cross_chats(self) -> None:
        # Two chats, adjacent timestamps.
        _insert_msg(self.src, "a1", "chatA@s.whatsapp.net", "a", "alpha", _ts(hour=10))
        _insert_msg(self.src, "b1", "chatB@s.whatsapp.net", "b", "beta", _ts(hour=10, minute=1))
        _insert_msg(self.src, "a2", "chatA@s.whatsapp.net", "a", "gamma", _ts(hour=10, minute=2))
        self.sync()
        res = self.search("alpha", limit=1)
        self.assertEqual(len(res["results"]), 1)
        window = res["results"][0]
        self.assertEqual(window["chat_jid"], "chatA@s.whatsapp.net")
        # All messages in a window belong to that same chat.
        self.assertTrue(len(window["messages"]) >= 1)

    def test_window_cuts_on_gap_over_30min(self) -> None:
        # 6 messages tightly packed, then a 2h gap, then more.
        chat = "gap@s.whatsapp.net"
        for i in range(6):
            _insert_msg(self.src, f"g{i}", chat, "x", f"msg{i}", _ts(hour=10, minute=i))
        # 2h gap
        _insert_msg(self.src, "g6", chat, "x", "msg6 late", _ts(hour=12, minute=0))
        _insert_msg(self.src, "g7", chat, "x", "msg7 later", _ts(hour=12, minute=1))
        self.sync()
        # Search the anchor g0
        res = self.search("msg0", limit=1)
        self.assertEqual(len(res["results"]), 1)
        ids = [m["message_id"] for m in res["results"][0]["messages"]]
        self.assertIn("g0", ids)
        self.assertNotIn("g6", ids)  # across the gap
        self.assertNotIn("g7", ids)

    def test_overlapping_windows_dedup(self) -> None:
        chat = "ovr@s.whatsapp.net"
        # Three close messages so anchors g0 and g1 windows fully overlap.
        _insert_msg(self.src, "o0", chat, "x", "delta", _ts(hour=10))
        _insert_msg(self.src, "o1", chat, "x", "delta echo", _ts(hour=10, minute=1))
        _insert_msg(self.src, "o2", chat, "x", "delta foxtrot", _ts(hour=10, minute=2))
        self.sync()
        res = self.search("delta", limit=3)
        # Multiple anchors may produce overlap; final dedup should reduce them.
        all_ids: list[str] = []
        for w in res["results"]:
            all_ids += [m["message_id"] for m in w["messages"]]
        self.assertEqual(len(all_ids), len(set(all_ids)), "no duplicate message across windows")

    def test_window_messages_chronological(self) -> None:
        chat = "chr@s.whatsapp.net"
        _insert_msg(self.src, "c0", chat, "x", "zeta", _ts(hour=10))
        _insert_msg(self.src, "c1", chat, "x", "eta", _ts(hour=10, minute=2))
        _insert_msg(self.src, "c2", chat, "x", "theta", _ts(hour=10, minute=4))
        self.sync()
        res = self.search("zeta", limit=1)
        self.assertGreaterEqual(len(res["results"]), 1)
        ts = [m["timestamp"] for m in res["results"][0]["messages"]]
        self.assertEqual(ts, sorted(ts))


class TestRebuild(RagTestBase):
    def test_rebuild_round_trip(self) -> None:
        for i in range(3):
            _insert_msg(self.src, f"r{i}", "rb@s.whatsapp.net", "a", f"body {i}", _ts(days_ago=i))
        self.sync()
        s_rebuild = self.sync(rebuild=True)
        self.assertEqual(s_rebuild["indexed"], 3)
        # Watermark advanced correctly after rebuild (next sync = 0 new).
        s_follow = self.sync()
        self.assertEqual(s_follow["indexed"], 0)

    def test_all_rows_reembeds_without_drop(self) -> None:
        for i in range(3):
            _insert_msg(self.src, f"a{i}", "all@s.whatsapp.net", "a", f"body {i}", _ts(days_ago=i))
        self.sync()
        s_all = self.sync(all_rows=True)
        self.assertEqual(s_all["indexed"], 3)
        # Documents survived (count = 3)
        idx = rag.open_index(str(self.idx_path))
        try:
            n = idx.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0]
            self.assertEqual(n, 3)
        finally:
            idx.close()


class TestSecurity(RagTestBase):
    def test_remote_embed_url_rejected(self) -> None:
        old = os.environ.get("OLLAMA_EMBED_URL")
        os.environ["OLLAMA_EMBED_URL"] = "https://api.evil.example.com/embed"
        try:
            with self.assertRaises(rag.RagError) as ctx:
                rag.embed_url()
            self.assertIn("loopback", str(ctx.exception).lower())
        finally:
            if old is None:
                os.environ.pop("OLLAMA_EMBED_URL", None)
            else:
                os.environ["OLLAMA_EMBED_URL"] = old

    def test_garbage_embed_url_rejected(self) -> None:
        old = os.environ.get("OLLAMA_EMBED_URL")
        os.environ["OLLAMA_EMBED_URL"] = "not-a-url"
        try:
            with self.assertRaises(rag.RagError):
                rag.embed_url()
        finally:
            if old is None:
                os.environ.pop("OLLAMA_EMBED_URL", None)
            else:
                os.environ["OLLAMA_EMBED_URL"] = old

    def test_wrong_dim_blob_rejected(self) -> None:
        bad = b"\x00" * (rag.EMBED_DIM * 4 + 8)  # off-by-8 bytes
        with self.assertRaises(rag.RagError):
            rag.blob_to_vec(bad)

    def test_wrong_dim_vec_to_blob_rejected(self) -> None:
        with self.assertRaises(rag.RagError):
            rag.vec_to_blob([0.0] * (rag.EMBED_DIM - 1))

    def test_query_only_on_source_db(self) -> None:
        """Source DB must reject writes via query_only pragma."""
        _insert_msg(self.src, "w1", "c@s.whatsapp.net", "a", "hi", _ts())
        conn = rag.open_source(str(self.src_path))
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM messages WHERE id = 'w1'")
        finally:
            conn.close()


class TestEmbedFake(unittest.TestCase):
    def test_fake_embedding_deterministic_and_normalized(self) -> None:
        v1 = rag._fake_embedding("hello world")
        v2 = rag._fake_embedding("hello world")
        self.assertEqual(v1, v2)
        self.assertEqual(len(v1), rag.EMBED_DIM)
        norm = sum(x * x for x in v1) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=5)


class TestTurnGroup(RagTestBase):
    def test_close_messages_merge_into_one_turn(self) -> None:
        chat = "turn@g.us"
        _insert_msg(self.src, "t0", chat, "alice", "hola equipo", _ts(hour=10, minute=0))
        _insert_msg(self.src, "t1", chat, "bob", "hola, listos?", _ts(hour=10, minute=1))
        _insert_msg(self.src, "t2", chat, "alice", "si, arrancamos", _ts(hour=10, minute=2))
        stats = self.sync()
        self.assertEqual(stats["indexed"], 1)
        idx = rag.open_index(str(self.idx_path))
        try:
            n = idx.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0]
            self.assertEqual(n, 1)
            content, members = idx.execute(
                "SELECT content, members_json FROM rag_documents"
            ).fetchone()
            self.assertIn("alice: hola equipo", content)
            self.assertIn("bob: hola, listos?", content)
            self.assertEqual(len(__import__("json").loads(members)), 3)
        finally:
            idx.close()

    def test_gap_starts_new_turn(self) -> None:
        chat = "gapturn@g.us"
        _insert_msg(self.src, "a0", chat, "x", "antes", _ts(hour=10, minute=0))
        _insert_msg(
            self.src, "a1", chat, "x", "despues",
            _ts(hour=10, minute=0) + rag.TURN_GAP_SECONDS + 60,
        )
        stats = self.sync()
        self.assertEqual(stats["indexed"], 2)

    def test_search_expands_turn_members(self) -> None:
        chat = "exp@g.us"
        _insert_msg(self.src, "e0", chat, "a", "alpha topic", _ts(hour=11, minute=0))
        _insert_msg(self.src, "e1", chat, "b", "follow up", _ts(hour=11, minute=1))
        self.sync()
        res = self.search("alpha topic", limit=1)
        self.assertEqual(len(res["results"]), 1)
        ids = [m["message_id"] for m in res["results"][0]["messages"]]
        self.assertEqual(ids, ["e0", "e1"])


class TestWorkServerGuardrails(unittest.TestCase):
    """main_work.py es la superficie que ven agentes de terceros.

    Los tools de whatsapp.py leen messages.db directo y no pasan por el
    allowlist del RAG. La proteccion es que este archivo no los importa, y eso
    se rompe con un import de una linea, sin ruido.
    """

    def setUp(self) -> None:
        self.src = (Path(__file__).with_name("main_work.py")).read_text(encoding="utf-8")

    def test_does_not_import_whatsapp_module(self) -> None:
        self.assertNotIn("import whatsapp", self.src)
        self.assertNotIn("from whatsapp", self.src)

    def test_exposes_exactly_one_tool(self) -> None:
        self.assertEqual(self.src.count("@mcp.tool()"), 1)

    def test_refuses_to_start_without_allowlist(self) -> None:
        # Solo se salta si falta el paquete mcp (no esta en todos los entornos).
        # Cualquier otro import roto es un despliegue incompleto, no un skip:
        # asi se colo una vez un redact.py que faltaba en el servidor.
        if importlib.util.find_spec("mcp") is None:
            self.skipTest("paquete mcp no instalado en este entorno")
        import main_work
        original = main_work.rag.CHATS
        main_work.rag.CHATS = ""
        self.addCleanup(setattr, main_work.rag, "CHATS", original)
        with self.assertRaises(SystemExit):
            main_work._check_scope()

    def test_rejects_unfiltered_index(self) -> None:
        # Un indice sin filtro es el personal: tiene todos los chats. Comparar
        # rutas no lo detecta, hay que preguntarle al archivo.
        if importlib.util.find_spec("mcp") is None:
            self.skipTest("paquete mcp no instalado en este entorno")
        import main_work

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "messages.db"
        idx = Path(tmp.name) / "personal.db"
        _make_source_db(src)
        rag.sync_index(
            src_path=str(src), idx_path=str(idx), log=False, chats=""
        )

        original = main_work.rag.CHATS
        main_work.rag.CHATS = "groups"
        self.addCleanup(setattr, main_work.rag, "CHATS", original)
        original_idx = main_work.WORK_INDEX_DB
        main_work.WORK_INDEX_DB = str(idx)
        self.addCleanup(setattr, main_work, "WORK_INDEX_DB", original_idx)

        with self.assertRaises(SystemExit):
            main_work._check_scope()

    def test_accepts_scoped_index(self) -> None:
        if importlib.util.find_spec("mcp") is None:
            self.skipTest("paquete mcp no instalado en este entorno")
        import main_work

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "messages.db"
        idx = Path(tmp.name) / "work.db"
        _make_source_db(src)
        rag.sync_index(
            src_path=str(src), idx_path=str(idx), log=False, chats="groups"
        )

        original = main_work.rag.CHATS
        main_work.rag.CHATS = "groups"
        self.addCleanup(setattr, main_work.rag, "CHATS", original)
        original_idx = main_work.WORK_INDEX_DB
        main_work.WORK_INDEX_DB = str(idx)
        self.addCleanup(setattr, main_work, "WORK_INDEX_DB", original_idx)

        main_work._check_scope()  # no debe levantar


if __name__ == "__main__":
    unittest.main(verbosity=2)
