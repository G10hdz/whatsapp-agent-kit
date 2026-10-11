# Learnings — whatsapp-agent-kit

### 2026-09-29 — whatsmeow directPath needs the signed query
- **Problem**: `/api/download` returned 403 for every media fetch, even fresh messages.
- **Root cause**: `extractDirectPathFromURL` in `whatsapp-bridge/main.go` stripped `?ccb&oh&oe` params, but whatsmeow builds `https://<host><directPath>&hash=...` and expects the signature inside directPath.
- **Fix**: keep the query string on directPath (`957a045`).
- **Lesson**: media download had never worked since the feature shipped. A health check that exercises one real download would have caught it.

### 2026-09-29 — bridge stores timestamps as TEXT, not epoch
- **Problem**: `timestamp >= <epoch>` range filters silently match every row.
- **Root cause**: the Go bridge writes `time.Time` as TEXT (`2026-09-29 10:57:41-06:00`); in SQLite, TEXT > INTEGER always.
- **Fix**: compare the ISO date prefix lexicographically (`timestamp >= 'YYYY-MM-DD'`).
- **Lesson**: check column affinity in the bridge schema before writing range predicates; epoch comparisons only work on `rag_media`'s own epoch columns.

### 2026-09-29 — Pop's system python is PEP 668 managed
- **Problem**: `pip install faster-whisper` fails on Pop.
- **Fix**: `python3 -m venv wa-rag/.venv` and install there; systemd units call the venv python.
- **Lesson**: anything new on Pop goes in a venv, never system site-packages.

### 2026-09-30 — qwen3-vl thinks anyway, budget the thinking
- **Problem**: vision captions return `""` or look hung even with `think:false` and `/no_think`.
- **Root cause**: ollama <=0.35 (tested 0.20.7 Pop, 0.35.0 Mac) ignores both flags for qwen3-vl; the model always emits ~200 thinking tokens before the answer, and `num_predict` counts thinking.
- **Fix**: `num_predict: 600` in the caption payload; a truncated-to-thinking response lands empty, the record is skipped, and the next extract pass retries it.
- **Lesson**: for vision models on ollama, never trust response disabling flags — measure `eval_count` vs `thinking` and size `num_predict` for think+answer.

### 2026-09-30 — llama-server single slot + zombie request = silent stall
- **Problem**: after restarting a stuck client, every following caption stalls with an open connection while `llama-server` idles at <1% CPU inside `encoding mtmd batch`.
- **Root cause**: the abandoned request still occupies the one inference slot (n_slots=1); the runner waits on it instead of processing the queue.
- **Fix**: kill the client process holding the ESTABLISHED connection; the encoder unblocks immediately (~188ms/image batch).
- **Lesson**: when an ollama runner goes quiet with work queued, check `lsof -p <pid>` for a dead client holding the slot before restarting anything.

### 2026-09-30 — RX 6700 XT needs Vulkan (or patient ROCm) on ollama
- **Problem**: Pop's ollama ran every model on CPU: "offloaded 0/13 layers to GPU".
- **Root cause**: ROCm GPU discovery hangs ~56s on gfx1031 (unsupported SKU) and times out, so the card is never registered.
- **Fix**: user-level `ollama.service` with `OLLAMA_VULKAN=1`/`OLLAMA_LLM_LIBRARY=vulkan` — RADV sees the card in 0.6s. After the first JIT cache completed, ROCm discovery also started succeeding; either backend now works.
- **Lesson**: on unsupported RDNA2 parts, Vulkan is the deterministic path; "model loads fine" logs do not mean the GPU is being used — check `PROCESSOR` in `ollama ps`.

### 2026-09-30 — pgrep -f self-matches through launcher cmdlines
- **Problem**: the archive watcher never fired even though rsync had finished.
- **Root cause**: the `bash -c` launcher that wrote the script via heredoc kept the entire script text in its cmdline, so `pgrep -f "rsync --server"` (and the extract pattern) matched the launcher forever.
- **Fix**: killed the stale launcher; watcher proceeded.
- **Lesson**: in watcher scripts, match a stable artifact (e.g. `bash archive-pipeline.sh` or a PID file), never a pattern that also appears in the script text itself — heredoc launchers make every pattern self-matching.

### 2026-09-30 — long screenshots stall vision encoders
- **Problem**: a single ~20k-px-tall WhatsApp screenshot froze captioning for >10min.
- **Fix**: downscale to <=1568px long edge (`sips -Z 1568` on Mac, ffmpeg scale elsewhere) into a temp file before base64.
- **Lesson**: always pre-bounds image dimensions before sending to a VL model; encoder cost scales with pixels, not file size.

### 2026-10-10 — deps sin cota en libs jóvenes
- **Problem**: `pip install "mcp[cli]>=1.6.0"` resolvió a mcp 2.x en CI; FastMCP
  se renombró a MCPServer y el import tronó.
- **Root cause**: `>=` sin cota superior en una lib pre-1.0-de-hecho.
- **Fix**: `mcp[cli]>=1.6.0,<2` en pyproject, workflow y CONTRIBUTING. uv.lock
  ya pinneaba 1.6.0 — el lockfile era la verdad.
- **Lesson**: deps `>=` sueltos son bombas; pin `<major+1` o instala del lock.

### 2026-10-10 — clon upstream anidado dentro del repo
- **Problem**: un directorio del repo era un clon git propio
  (lharries/whatsapp-mcp) con .git interno — los fuentes modificados
  (NEVER_SEND) no podían trackearse aunque se quitara el ignore.
- **Fix**: `mv .git ~/.local/share/whatsapp-mcp-upstream.git` + gitignore
  selectivo (`dir/*` + `!dir/sub/`). Mismo path → cero churn de deploy.
- **Lesson**: `git status` no muestra repos anidados como "untracked";
  `find -name .git` los descubre.

### 2026-10-10 — identidad de commits
- **Problem**: commits salieron con el autor equivocado por un
  `-c user.name=` inline inventado.
- **Fix**: `--amend --author` en rama abierta; de aquí en adelante confiar en
  `git config --global` (ya es el correcto).
- **Lesson**: nunca forzar identidad por `-c`; verificar
  `git config user.email` antes de commitear en repo ajeno.
