# Agent Skills

Tool inventory for each agent in `local-cursor`.

---

## 1. CodingAgent — `main.py`
Interactive CLI assistant (Ollama, `qwen2.5-coder:3b`). Runs directly in the current directory.

| Skill | Description |
|---|---|
| `read_file` | Read a file's contents |
| `write_file` | Write clean, well-formatted, properly indented content to a file |
| `list_files` | List files in a directory |
| `find_files` | Find files matching a glob pattern |
| `run_command` | Run a shell command |
| `web_search` | Search the web for current-event / time-sensitive info (max 10 results) |

System-prompt rules: always use `write_file` for new files, prefer `web_search` for
time-sensitive questions, emit code as single-line JSON with escaped newlines, and
keep code consistently indented (4 spaces, blank lines between definitions).

---

## 2. Secure Agent — `secure_agent.py`
Autonomous senior coding agent running inside an **offline Docker sandbox** (`/workspace`).
Test-driven workflow: implement → write `test_*.py` → `run_tests` → fix → repeat until green.

| Skill | Description |
|---|---|
| `write_workspace_file` | Create/overwrite a file in the sandbox workspace |
| `read_workspace_file` | Read a file from the sandbox workspace |
| `list_workspace_files` | List files/subdirs in the sandbox workspace |
| `edit_workspace_file` | Replace an exact string (preferred over rewriting) |
| `install_dependencies` | Install packages into the sandbox venv (only networked step) |
| `run_sandbox_command` | Run a bash command offline in the container |
| `run_tests` | Run pytest on a path with optional extra flags |

---

## 3. Advanced Dev Agent — `advanced_dev_agent.py`
Self-correcting development agent with local RAG/docs + `GitGuard` version control.
FIRST step is always `scan_codebase_index`.

| Skill | Description |
|---|---|
| `scan_codebase_index` | Build/inspect the local embedding index of the codebase |
| `search_codebase` | Semantic search over the local embedding index |
| `search_docs` | Consult the offline documentation pool (`local_rag`) |
| `write_workspace_file` | Create/overwrite a source or `test_*.py` file |
| `read_workspace_file` | Read a workspace file |
| `list_workspace_files` | List files/subdirs |
| `edit_workspace_file` | Exact-string replacement (preferred) |
| `install_dependencies` | Install Python packages into the shared sandbox venv |
| `run_sandbox_command` | Run a shell command offline in the sandbox |
| `run_tests` | Run pytest until it passes |

Supporting capability — **`GitGuard`** (`advanced_dev_agent.py:83`): `ensure_repo`,
`snapshot`, `rollback`, `changed_paths`. Discards uncommitted edits on failure and
reports `ROLLBACK` so the agent re-reads and retries a different approach.

---

## 4. Multi-Agent Graph — `multi_agent_graph.py`
Orchestrated pipeline: **Developer Agent → quality gates → Code Reviewer → commit/rollback**,
with human-in-the-loop approval.

### 4a. Developer Agent
Scoped subset of the Advanced Dev Agent tools (`DEV_ALLOWED`):

| Skill | Description |
|---|---|
| `scan_codebase_index` | Explore the codebase index first |
| `search_codebase` | Semantic code search |
| `search_docs` | Offline documentation lookup |
| `write_workspace_file` | Write clean Python source + pytest tests |
| `read_workspace_file` | Read a file |
| `list_workspace_files` | List files |
| `edit_workspace_file` | Targeted fixes from reviewer feedback |
| `install_dependencies` | Install packages |

*Deliberately denied:* `run_tests` / `run_sandbox_command` — the reviewer runs tests.

### 4b. Code Reviewer Agent
No tools. Consumes task + black/flake8/pytest output + source files, and replies
**only** with JSON: `{"verdict": "approve"|"revise", "feedback": "..."}`.

### 4c. Deterministic quality gates (non-LLM)
| Capability | Description |
|---|---|
| `run_format` | `black` on changed files |
| `run_lint` | `flake8` on changed files (scoped to `last_good`) |
| `run_tests_scoped` | `pytest` on changed test files, fallback whole workspace |
| `GitGuard` | Snapshot / rollback / `changed_paths` (baseline = `last_good`) |
| HITL gate | Pauses after >2 reviewer rejects; `/api/hitl/approve` or `/api/hitl/feedback` |

---

## 5. Supporting modules
| Module | Role |
|---|---|
| `codebase_index.py` | Local embedding index over the codebase |
| `local_rag.py` | Offline documentation pool for `search_docs` |
| `rich_monitor.py` | Rich live dashboard for workspace/watch state |
| `watch_workspace.py` | File tree watcher |
| `web_server.py` / `web_state.py` | FastAPI server + `MemoryPool` (events, metrics, HITL state) powering the web UI |
