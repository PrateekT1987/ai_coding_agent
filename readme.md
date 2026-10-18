# Local Cursor

> **Privacy‑first AI coding assistant that lives entirely in your terminal. No clouds, no compromises.**

Local Cursor wraps a local large‑language‑model (LLM) served by [Ollama](https://ollama.ai/) in a simple CLI, giving you Copilot‑style help without ever sending a byte of your code to external servers.


## Key features

| Capability      | What it means                                                          |
| --------------- | ---------------------------------------------------------------------- |
| File tooling    | Read, write, list & search files via natural‑language requests        |
| Shell commands  | Safely run whitelisted commands (`ls`, `grep`, `find`, …)             |
| Web search      | Optional Exa API integration for up‑to‑date answers                   |
| Local LLM       | Ships with **`qwen2.5-coder:3b`** by default—swap in any Ollama model |
| Sandboxed exec  | All code runs in an ephemeral, resource‑capped Docker container (no network at run time) |
| Test‑driven     | Writes its own `test_*.py` suite and iterates on `pytest` until green |
| Git checkpoints | Every change is snapshotted; passing work is committed and tagged `last_good`, failures roll back |
| Semantic search | Embedding index (`nomic-embed-text` + cosine) over your codebase      |
| Offline docs    | Local RAG over a docs pool — no ChromaDB/FAISS needed                 |
| Multi‑agent     | Developer ↔ Reviewer graph with automatic `black`/`flake8`/`pytest` gates |
| Web console     | FastAPI + Tailwind single‑file UI that live‑streams the agents' conversation |
| Extensible      | Add new tools, commands or UI layers without touching model code      |


## Quick start

### 1 · Prerequisites

* **Python ≥ 3.8**
* **Ollama** running locally (`brew install ollama` or see the [docs](https://ollama.ai/))
* *(Optional)* Exa API key for web search

### 2 · Install & run

```bash
# Clone and enter the repo
git clone https://github.com/towardsai/local-cursor.git
cd local‑cursor

# Set up a virtualenv
python -m venv .venv && source .venv/bin/activate

# Install Python deps (or: uv venv && uv pip install -r requirements.txt)
pip install -r requirements.txt

# Pull a model & start Ollama
ollama pull qwen2.5-coder:3b
ollama serve      # keep this terminal running

# (Optional) add your Exa key
echo "EXA_API_KEY=sk‑..." > .env

# Fire up the assistant
python main.py --model qwen2.5-coder:3b
```

*Tip →* run `python main.py --help` for all CLI flags (debug mode, model override, …).


## How it works
![Architecture](https://github-production-user-asset-6210df.s3.amazonaws.com/209818798/440162845-5d8e3b21-2095-41f6-b1db-e0c6c047958f.png?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAVCODYLSA53PQK4ZA%2F20250504%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20250504T053828Z&X-Amz-Expires=300&X-Amz-Signature=7a44b959a613474452f0e24e21d884f080f7e78438a8e5c39aa2c8ff054c7ca7&X-Amz-SignedHeaders=host)

1. Natural language input from the terminal is sent to the model, along with a system prompt that lists the available tools.
2. The local LLM (via Ollama) analyzes the request and responds with either a plain-text answer or a structured tool call.
3. If a tool call is issued, the OllamaAgent executes the corresponding function (e.g., read/write a file, run a shell command) and sends the result back to the model.
4. This loop continues until the model produces a final answer, which is then printed in your terminal.

All logic is in [`main.py`](./main.py); the heavy lifting is done by the open‑source model running locally.

### Sandboxed agents

Beyond the interactive `main.py`, the repo includes progressively more capable
autonomous agents. They share one execution model: **file edits happen in a host
workspace, but every shell command / test runs inside an ephemeral Docker
container** with no network, a memory cap (`SANDBOX_MEMORY`) and a timeout. The
sandbox image is auto‑built once from `python:3.11-slim` plus a small package
set; third‑party dependencies go into a persistent host virtualenv that is
mounted read‑only at run time, so installed code runs offline.

`secure_agent.py` is the foundation — it defines the workspace tools and the
sandbox runner used by every other agent. `advanced_dev_agent.py` adds the Git
lifecycle guard and the semantic codebase index. `multi_agent_graph.py` wires a
Developer agent and a Reviewer agent together with deterministic quality gates.

### Repository layout

| File                    | Role                                                                  |
| ----------------------- | --------------------------------------------------------------------- |
| `main.py`               | Interactive single‑file agent (files, whitelisted shell, web search)  |
| `secure_agent.py`       | Sandboxed TDD agent + the shared workspace tool / sandbox layer       |
| `advanced_dev_agent.py` | Git‑guarded agent with `wip:` snapshots, commits & codebase indexing  |
| `multi_agent_graph.py`  | Developer ↔ Reviewer graph with `black`/`flake8`/`pytest` gates      |
| `codebase_index.py`     | Embedding index over the workspace (cosine search)                    |
| `local_rag.py`          | Offline docs retrieval, exposed as the `search_docs` tool             |
| `rich_monitor.py`       | Rich split‑screen live workspace monitor (`--exec` a command)         |
| `watch_workspace.py`    | Lightweight colorama workspace watcher for a second terminal          |
| `web_state.py`          | Thread‑safe in‑process memory pool for the live session state         |
| `web_server.py`         | FastAPI server exposing the pool + serving the web UI                 |
| `web_ui.html`           | Single‑file Tailwind frontend (polls the API once per second)         |


## Available tools

The interactive `main.py` agent exposes these tools:

| Tool                               | What it does                                         |
| ---------------------------------- | ---------------------------------------------------- |
| `list_files(path=".")`             | Show directories and files with human‑friendly icons |
| `read_file(path)`                  | Return the full text of a file                       |
| `write_file(path, content)`        | Create or overwrite a file                           |
| `find_files(pattern)`              | Glob search (e.g. `**/*.py`)                         |
| `run_command(cmd)`                 | Execute a whitelisted shell command                  |
| `web_search(query, num_results=5)` | Query the web via Exa                                |

Add your own by editing `get_tools_definition()`—the model will “see” them automatically at runtime. The sandboxed agents expose a different (workspace‑scoped) tool set, listed in the sections below.


## Test-driven secure agent

`secure_agent.py` runs the model against an isolated Docker sandbox and follows a
test-driven workflow by default: it implements the code, writes a `test_*.py`
suite, and calls `run_tests` to execute pytest inside the sandbox. When tests
fail, the pytest stdout/stderr is fed back to the model, which patches the code
(fix, `edit_workspace_file`) and re-runs `run_tests` until the suite is green.

| Secure agent tool                                  | What it does                                          |
| -------------------------------------------------- | ----------------------------------------------------- |
| `write_workspace_file(filepath, content)`          | Create/overwrite a file in the sandbox workspace      |
| `read_workspace_file(filepath)`                    | Read a file from the workspace                        |
| `list_workspace_files(path=".")`                   | List workspace files/folders                          |
| `edit_workspace_file(filepath, old, new)`          | Replace an exact string in a workspace file           |
| `install_dependencies(packages)`                   | Install packages (only step with network access)      |
| `run_sandbox_command(command)`                     | Run a shell command offline in the sandbox            |
| `run_tests(target=".", args="")`                   | Run pytest in the sandbox; returns pass/fail + output |

```bash
python secure_agent.py "write a module and tests, run until green"
python rich_monitor.py --exec "python secure_agent.py '<task>'"   # Rich live monitor
python watch_workspace.py                                          # lightweight watcher (second terminal)
```


## Advanced Git-guarded agent

`advanced_dev_agent.py` layers two modules on top of the secure sandbox:

- **Git Lifecycle Guard** – before any mutating tool runs, the working tree is
  committed as a temporary `wip:` snapshot. When `run_tests` passes, the WIP
  snapshots are squashed into one permanent `feat:` commit and the `last_good`
  tag is moved. If the same test failure repeats `AGENT_UNRECOVERABLE_LIMIT`
  (default 3) times, the workspace is hard-rolled-back to `last_good`
  (`git reset --hard` + `git clean -fd`).
- **Multi-File Indexing Scanner** – `scan_codebase_index` walks the workspace
  and returns a structured map (path · lines · chars), then builds/refreshes a
  semantic embedding index. Raw content is retrieved on demand with
  `search_codebase(query, top_k)`, which embeds the query using Ollama's
  `nomic-embed-text` and returns the most relevant code sections (path + line
  range + code). The index is cached at `~/.cache/local-cursor/codebase_index.json`
  and only re-embeds files whose content hash changed, so the context window
  stays bounded even for large projects.

```bash
python advanced_dev_agent.py            # runs the built-in auth.py TDD demo
python advanced_dev_agent.py "<task>"   # custom task

# index utilities (standalone)
python codebase_index.py build          # (re)build the embedding index
python codebase_index.py search "password validation" -k 3
```

The Rich dashboard shows the workspace tree (green = new, yellow = modified,
cyan = tests), a live log stream, and the index size, with `HEAD`/`last_good`
in the header.

Design notes:

- Rollback is reserved for **repeated identical failures**, not every failed
  run — otherwise the agent could never iterate on a fix. Tune the threshold with
  `AGENT_UNRECOVERABLE_LIMIT`.
- The model defaults to `qwen2.5-coder:3b` (what is installed). To use 7b:
  `ollama pull qwen2.5-coder:7b` then `AGENT_MODEL=qwen2.5-coder:7b python advanced_dev_agent.py`.
- The workspace is a Git repo (initialized with a baseline commit + `.gitignore`);
  inspect history with `git -C ~/data/pt87/ai_coding/agent_workspace log --oneline --decorate`.


## Local RAG for offline documentation

`local_rag.py` gives the agent an offline documentation lookup, so it can consult
library/API manuals without internet access. Drop `.md` / `.txt` / `.rst` files
into the docs pool (default `~/data/pt87/ai_coding/local_docs`, override with
`DOCS_DIR`) and the `search_docs(query, top_k)` tool retrieves the most relevant
sections.

It reuses the same lightweight stack as the codebase index — Ollama
`nomic-embed-text` embeddings + cosine similarity over a cached, incrementally
built index (`~/.cache/local-cursor/docs_index.json`) — so no ChromaDB/FAISS
dependency is needed. On first run the pool is seeded with a README and a
`python-stdlib.md` cheatsheet.

```bash
python local_rag.py path                 # show the docs pool location
python local_rag.py build                # (re)build the docs index
python local_rag.py list                 # list indexed docs
python local_rag.py search "requests.Session retries" -k 3
```


## Multi-agent graph: Developer ↔ Reviewer

`multi_agent_graph.py` runs two agents with **separate system contexts** on the
same local model, wired as a graph:

```
develop → format → lint → test → review ─┬─ approve → commit
   ▲                                      │
   └──────────── revise (feedback) ◄──────┘
```

- **Developer Agent** — turns the task (plus any reviewer feedback) into source
  files and `test_*.py` unit tests, using the workspace/index/docs tools.
- **Quality payload (deterministic, in-sandbox)** — `black` auto-formats the
  changed files, `flake8` lints them, and `pytest` runs the changed tests.
- **Code Reviewer Agent** — judges correctness/clarity/coverage from the sources
  plus the lint/test results and returns `{"verdict": "approve"|"revise",
  "feedback": ...}`.

A permanent Git checkpoint is committed only when the change is formatted,
lint-clean, tests pass **and** the reviewer approves; otherwise feedback goes
back to the Developer, and after `AGENT_MAX_REVISIONS` the workspace is rolled
back to `last_good`. `black`/`flake8` are installed into the sandbox venv on
first run. Lint/format/test are scoped to the files changed in that run so
pre-existing styles never block a change.

```bash
python multi_agent_graph.py            # built-in stringutils.py demo
python multi_agent_graph.py "<task>"   # custom task
```

The Rich dashboard shows the graph node states, the live agent console, and the
workspace tree with git-status colours.


## Interactive web console

For a richer, browser-based view of the same loop, `web_server.py` runs a
lightweight **FastAPI** server *in the same process* as the graph. Instead of
printing to the terminal, `run_graph` appends its entire conversational state
into a thread-safe global memory pool (`web_state.POOL` in `web_state.py`):
chat messages, tool executions, `black`/`flake8`/`pytest` results, graph-node
transitions, metrics and Git status.

A single-file **Tailwind** frontend (`web_ui.html`) polls `GET /api/state` once
per second and renders chat bubbles for the Developer and Reviewer, tool-call
badges, lint/test result cards, a graph timeline, live metrics and the git tree.

```bash
python web_server.py                       # serve the console (idle)
python web_server.py --run                 # serve + auto-start the demo task
python web_server.py --task "..." --open   # serve + start a task + open browser
python multi_agent_graph.py --web "..."    # same thing via the graph CLI
```

| Endpoint           | Method | Purpose                                              |
| ------------------ | ------ | ---------------------------------------------------- |
| `/`                | GET    | The single-file Tailwind UI                          |
| `/api/state`       | GET    | Pool snapshot; `?since=<id>` returns only new events |
| `/api/status`      | GET    | Lightweight running/status/result summary            |
| `/api/run`         | POST   | Start a run: `{"task": "..."}` (409 if already busy) |

The server binds `127.0.0.1:8000` by default (`--host` / `--port`). Because the
pool lives in-process, the server and the agent must run together — start
everything with `web_server.py` (or `multi_agent_graph.py --web`). The frontend
is one HTML file that pulls Tailwind from a CDN; no build step is required.



## Environment variables

Every knob is overridable via environment variables (nothing needs editing).

| Variable                              | Default                              | Purpose                                              |
| ------------------------------------- | ------------------------------------ | ---------------------------------------------------- |
| `AGENT_MODEL`                         | `qwen2.5-coder:3b`                   | Chat model for the agents                            |
| `AGENT_MAX_STEPS`                     | `30`                                 | Max tool steps per `secure_agent.py` run             |
| `AGENT_UNRECOVERABLE_LIMIT`           | `3`                                  | Repeated failures before the advanced agent rolls back |
| `AGENT_MAX_REVISIONS`                 | `2`                                  | Reviewer revise rounds before the graph rolls back   |
| `AGENT_DEV_MAX_STEPS`                 | `8`                                  | Max Developer tool steps per revision                |
| `EMBED_MODEL`                         | `nomic-embed-text`                   | Embedding model for codebase/docs indexes            |
| `SANDBOX_IMAGE`                       | `local-agent-sandbox:latest`         | Sandbox Docker image name                            |
| `SANDBOX_BASE` / `SANDBOX_PACKAGES`   | `python:3.11-slim` / numpy…pytest    | Auto‑build inputs for the sandbox image              |
| `SANDBOX_AUTOBUILD`                   | `1`                                  | Auto‑build the sandbox image on first run            |
| `SANDBOX_MEMORY` / `SANDBOX_TIMEOUT`  | `2g` / `120`                         | Sandbox memory cap / per‑command timeout (s)         |
| `SANDBOX_INSTALL_TIMEOUT`             | `300`                                | `install_dependencies` timeout (only step with net)  |
| `SANDBOX_TEST_TIMEOUT`                | `180`                                | `run_tests` timeout (s)                              |
| `SANDBOX_FORMAT_TIMEOUT` / `SANDBOX_LINT_TIMEOUT` | `120` / `120`            | `black` / `flake8` timeouts (s)                      |
| `FLAKE8_MAX_LINE`                     | `100`                                | Max line length passed to `flake8`                   |
| `SANDBOX_VENV_DIR`                    | `~/.cache/local-cursor/venv`         | Persistent venv mounted read‑only into the sandbox   |
| `SCAN_MAX_CHARS`                      | `40000`                              | Max chars returned by `scan_codebase_index`          |
| `INDEX_CHUNK_LINES` / `INDEX_CHUNK_OVERLAP` / `INDEX_EMBED_BATCH` | `40` / `10` / `32` | Index chunking & embedding batch size |
| `CODEBASE_INDEX_PATH` / `DOCS_INDEX_PATH` | `~/.cache/local-cursor/…_index.json` | Index cache locations                     |
| `DOCS_DIR`                            | `~/data/pt87/ai_coding/local_docs`   | Docs pool for `search_docs`                          |
| `DOCKER_CMD`                          | `docker`                             | Docker binary (e.g. `podman`)                        |

> **Note** — the local Ollama server typically serves **one model at a time**
> (`-np 1`). A long‑running or stuck agent process blocks every other request, so
> make sure only one agent is running before starting another.


## Python dependencies

Local Cursor keeps its runtime lean:

```text
requests       # To make EXA API calls
click          # To write CLI
colorama       # To format CLI output
openai         # To create an OpenAI client
python‑dotenv  # To load environment variables from our .env file
rich           # Live dashboards for the sandboxed agents & monitor
fastapi        # Local web console API
uvicorn        # ASGI server for the web console
```

(See [`requirements.txt`](./requirements.txt) for exact versions.) 


## Security considerations

* Only a safe subset of shell commands is allowed by default—edit `allowed_commands` in `run_command()` to adjust. 
* All file paths are resolved inside the current working directory to avoid accidental system‑wide access.
* The sandboxed agents execute **everything inside an ephemeral Docker container**:
  * no network at run time (dependencies are the only networked step, installed
    into a host virtualenv that is mounted read‑only);
  * a memory cap (`SANDBOX_MEMORY`) and per‑command timeouts (`SANDBOX_*_TIMEOUT`);
  * workspace paths are re‑resolved and rejected if they escape the workspace.


## Troubleshooting

| Symptom                                      | Fix                                                        |
| -------------------------------------------- | ---------------------------------------------------------- |
| `Error: Exa API key not configured`          | Add `EXA_API_KEY` to `.env` or disable web search          |
| `command … not allowed for security reasons` | Add it to `allowed_commands` (understand the risks first!) |
| High memory usage                            | Try a smaller Ollama model like `qwen2.5-coder:1.5b`       |
| Model calls take minutes / hang              | A stray agent process may hold the single Ollama slot (`-np 1`); kill other `python*agent*.py` runs |
| `Sandbox image '…' not found`                | Keep `SANDBOX_AUTOBUILD=1`, or point `DOCKER_CMD` at a working Docker/Podman |
| `black`/`flake8` missing in the sandbox      | They install on the first multi‑agent run via `install_dependencies` |


## Contributing

1. Fork & clone
2. Create a virtualenv and install dev dependencies (`pip install -r requirements-dev.txt`)
3. Create a PR 


### Acknowledgements

* [Ollama](https://ollama.ai/) for making local LLMs drop‑dead simple
* [Exa](https://exa.ai/) for the search API
* The open‑source community that keeps pushing offline AI forward

