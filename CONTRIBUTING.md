# Contributing

## Tests

`wa-rag/` uses plain stdlib unittest scripts — no pytest, no test deps. Run
them from the directory:

```sh
cd wa-rag
python3 test_rag.py
python3 test_media_index.py
python3 test_archive_import.py
```

Decision: standalone scripts over pytest because wa-rag is stdlib-only and
adding a runner would be the repo's first test dependency for no gain.

The `check` workflow (`.github/workflows/check.yml`) builds the Go bridge,
compiles every Python module, syntax-checks the Node scripts, and runs these
tests on each PR.

`mcp-server/test_never_send.py` is the NEVER_SEND guardrail test: it imports
`main.py` and asserts the registered MCP tool set is exactly the read-only
set. It needs the server deps:

```sh
pip install "mcp[cli]>=1.6.0,<2" httpx requests
cd mcp-server
python3 test_never_send.py
```
