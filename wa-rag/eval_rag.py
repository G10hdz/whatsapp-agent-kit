#!/usr/bin/env python3
"""RAG evaluation harness: privacy-safe.

Reads queries from $WA_RAG_EVAL_QUERIES (default ~/.cache/rag-eval/queries.txt),
one per line. Blank lines and lines starting with '#' are skipped.

For each query: runs hybrid search and prints ONLY:
  - latency (ms)
  - fts_hits, vector_hits, window count
  - per-window: anchor_score, chat_alias (8 hex chars of JID hash),
    sender_alias (8 hex chars of sender hash), date range, message count

Never prints message content, full JIDs, or phone numbers.

Usage:
    python eval_rag.py                  # run all queries
    python eval_rag.py --limit 10       # override result limit
    python eval_rag.py --raw-ids        # show full doc_ids (for debugging only)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

import rag

DEFAULT_QUERIES_PATH = Path(
    os.environ.get("WA_RAG_EVAL_QUERIES", str(Path.home() / ".cache/rag-eval/queries.txt"))
)


def alias(value: str | None) -> str:
    if not value:
        return "-"
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


def load_queries(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def run_one(query: str, limit: int, raw_ids: bool) -> dict:
    t0 = time.time()
    try:
        res = rag.search(query, limit=limit)
    except rag.RagError as e:
        elapsed = (time.time() - t0) * 1000.0
        return {
            "query": query,
            "error": str(e),
            "elapsed_ms": round(elapsed, 1),
        }
    elapsed = (time.time() - t0) * 1000.0
    windows = []
    for w in res["results"]:
        first_ts = w["start_timestamp"]
        last_ts = w["end_timestamp"]
        windows.append(
            {
                "score": w["score"],
                "chat_alias": alias(w.get("chat_jid")),
                "sender_aliases": sorted({alias(m["sender"]) for m in w["messages"]}),
                "first_ts": first_ts,
                "last_ts": last_ts,
                "n_messages": len(w["messages"]),
                **({"anchor_doc_id": w.get("anchor_doc_id")} if raw_ids else {}),
            }
        )
    return {
        "query": query,
        "elapsed_ms": round(elapsed, 1),
        "fts_hits": res["fts_hits"],
        "vector_hits": res["vector_hits"],
        "vector_error": res.get("vector_error"),
        "n_windows": len(windows),
        "windows": windows,
    }


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    k = max(0, min(len(sorted_values) - 1, int(round((p / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=rag.FINAL_LIMIT_DEFAULT)
    ap.add_argument("--queries", type=Path, default=DEFAULT_QUERIES_PATH)
    ap.add_argument("--raw-ids", action="store_true", help="include doc_id (debug only)")
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()

    queries = load_queries(args.queries)
    if not queries:
        print(
            f"no queries found at {args.queries}. Create the file with one query per line.",
            file=sys.stderr,
        )
        return 2

    per_query: list[dict] = []
    for q in queries:
        r = run_one(q, args.limit, args.raw_ids)
        per_query.append(r)
        if not args.summary_only:
            safe = {k: v for k, v in r.items() if k != "windows"}
            print(json.dumps(safe, ensure_ascii=False), flush=True)

    latencies = sorted(r["elapsed_ms"] for r in per_query if "error" not in r)
    summary = {
        "n_queries": len(per_query),
        "n_errors": sum(1 for r in per_query if "error" in r),
        "latency_ms": {
            "min": round(min(latencies), 1) if latencies else 0,
            "p50": round(percentile(latencies, 50), 1),
            "p95": round(percentile(latencies, 95), 1),
            "max": round(max(latencies), 1) if latencies else 0,
        },
        "fts_hits_total": sum(r.get("fts_hits", 0) for r in per_query),
        "vector_hits_total": sum(r.get("vector_hits", 0) for r in per_query),
        "windows_total": sum(r.get("n_windows", 0) for r in per_query),
        "zero_hit_queries": [
            r["query"] for r in per_query
            if "error" not in r and r.get("fts_hits", 0) == 0 and r.get("vector_hits", 0) == 0
        ],
    }
    print("---SUMMARY---")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
