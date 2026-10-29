#!/usr/bin/env python3
"""
Advanced developer agent: resilient TDD loop + Git lifecycle guard + codebase indexer.

Two modules are added on top of the secure Docker sandbox in `secure_agent.py`:

  * Git Lifecycle Guard  — before a mutating tool runs, a temporary WIP snapshot
    is committed. When tests pass, the WIP snapshots are squashed into a single
    permanent commit and the `last_good` tag is moved. On unrecoverable failure
    (the same test error repeated) the workspace is hard-reset to `last_good`.

  * Multi-File Indexing Scanner — `scan_codebase_index` walks the workspace,
    counting lines/chars per file and returning a structured map plus previews,
    so the agent can understand an existing project within a bounded context
    budget.

The Rich live dashboard shows the workspace tree (with git status colours) and
the console log stream while the agent works.
"""

import os
import json
import time
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.tree import Tree
from rich.text import Text
from rich.console import Console
from rich.markup import escape

import secure_agent as sa
import llm_config
import codebase_index
import local_rag

MODEL_NAME = sa.MODEL_NAME
WORKSPACE_DIR = sa.WORKSPACE_DIR
LAST_GOOD_TAG = "last_good"
MAX_STEPS = sa.MAX_STEPS
UNRECOVERABLE_LIMIT = int(os.environ.get("AGENT_UNRECOVERABLE_LIMIT", "3"))

DEFAULT_GITIGNORE = """# Python
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
venv/
*.egg-info/

# OS / editor
.DS_Store
*.swp
"""

SCAN_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".venv", "venv",
    "node_modules", ".mypy_cache", ".ruff_cache",
}
MAX_INDEX_CHARS = int(os.environ.get("SCAN_MAX_CHARS", "40000"))

ui_logs = deque(maxlen=200)
git_status_cache = {}


def log_to_ui(message: str):
    ui_logs.append(str(message).rstrip())


def _short(value, limit: int = 160) -> str:
    text = str(value).replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------- #
# Git Lifecycle Guard
# --------------------------------------------------------------------------- #
class GitGuard:
    """Host-level Git wrapper providing snapshots, checkpoints and rollback."""

    def __init__(self, workspace: str = WORKSPACE_DIR, tag: str = LAST_GOOD_TAG):
        self.ws = workspace
        self.tag = tag

    def _git(self, *args, timeout: int = 30):
        return subprocess.run(
            ["git", *args], cwd=self.ws, capture_output=True, text=True, timeout=timeout
        )

    def _ref_exists(self, ref: str) -> bool:
        return self._git("rev-parse", "--verify", ref).returncode == 0

    def _dirty(self) -> bool:
        return bool(self._git("status", "--porcelain").stdout.strip())

    def ensure_repo(self):
        os.makedirs(self.ws, exist_ok=True)
        if not os.path.isdir(os.path.join(self.ws, ".git")):
            self._git("init")
            log_to_ui("🔧 Initialized new Git repository")
        self._git("config", "user.name", "AI Local Agent")
        self._git("config", "user.email", "agent@local.sandbox")

        if not self._ref_exists("HEAD"):
            gitignore = os.path.join(self.ws, ".gitignore")
            if not os.path.exists(gitignore):
                with open(gitignore, "w", encoding="utf-8") as f:
                    f.write(DEFAULT_GITIGNORE)
            readme = os.path.join(self.ws, "README.md")
            if not os.path.exists(readme):
                with open(readme, "w", encoding="utf-8") as f:
                    f.write("# Agent Project Sandbox Workspace\n")
            self._git("add", "-A")
            self._git("commit", "-m", "Initial commit workspace baseline")
            log_to_ui("🌱 Created baseline commit")

        if not self._ref_exists(f"refs/tags/{self.tag}"):
            self._git("tag", self.tag)

    def snapshot(self, label: str):
        """Commit the current (pre-mutation) working tree as a WIP checkpoint."""
        if not self._dirty():
            return None
        self._git("add", "-A")
        res = self._git("commit", "-m", f"wip: {label}")
        if res.returncode != 0:
            return None
        short = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        log_to_ui(f"🧷 Temp snapshot {short}: {label}")
        return short

    def commit_success(self, message: str):
        """Squash pending WIP snapshots + edits into one permanent commit."""
        self._git("add", "-A")
        if self._ref_exists(f"refs/tags/{self.tag}"):
            self._git("reset", "--soft", self.tag)
        if self._git("diff", "--cached", "--quiet").returncode != 0:
            self._git("commit", "-m", message)
        self._git("tag", "-f", self.tag)
        short = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        log_to_ui(f"🔒 Git checkpoint saved: {message} ({short})")
        return short

    def rollback(self) -> bool:
        """Hard-reset the workspace back to the last passing state."""
        self._git("reset", "--hard", self.tag)
        self._git("clean", "-fd")
        log_to_ui("🚨 Rollback: reverted workspace to the last passing state")
        return True

    def status_map(self) -> dict:
        """Map of rel-path -> git status code for the live tree colouring."""
        mapping = {}
        for line in self._git("status", "--porcelain").stdout.splitlines():
            if len(line) < 4:
                continue
            code = line[:2].strip() or "?"
            path = line[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ")[-1]
            mapping[path] = code
        return mapping

    def changed_paths(self, base: str = None) -> set:
        """All paths changed since `base` (default last_good), incl. untracked.

        Uses the tag rather than HEAD so files already captured in a temporary
        `wip:` snapshot are still reported as part of the change set.
        """
        base = base or self.tag
        paths = set()
        for line in self._git("diff", "--name-only", base).stdout.splitlines():
            if line.strip():
                paths.add(line.strip())
        for line in self._git("ls-files", "--others", "--exclude-standard").stdout.splitlines():
            if line.strip():
                paths.add(line.strip())
        return paths

    def head(self) -> str:
        return self._git("rev-parse", "--short", "HEAD").stdout.strip() or "-"

    def last_good(self) -> str:
        return self._git("rev-parse", "--short", self.tag).stdout.strip() or "-"


guard = GitGuard()
code_index = codebase_index.CodebaseIndex(WORKSPACE_DIR)
rag = local_rag.LocalRAG()


# --------------------------------------------------------------------------- #
# Multi-File Indexing Scanner
# --------------------------------------------------------------------------- #
def scan_codebase_index(preview_lines: int = 0) -> str:
    """Return a structural map of the workspace and refresh the embedding index.

    Raw content is no longer dumped wholesale; instead the workspace is chunked
    and embedded with `nomic-embed-text`, and relevant code is retrieved on
    demand via `search_codebase`. Pass `preview_lines > 0` to also include short
    file previews.
    """
    log_to_ui("🔍 Indexing codebase workspace...")
    entries = []
    total_lines = 0
    total_chars = 0

    for root, dirs, names in os.walk(WORKSPACE_DIR):
        dirs[:] = sorted(
            d for d in dirs if d not in SCAN_SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(names):
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, WORKSPACE_DIR)
            text = codebase_index.CodebaseIndex._read_text(full)
            if text is None:
                entries.append((rel, None, None))
                continue
            n_lines = len(text.splitlines())
            entries.append((rel, n_lines, text))
            total_lines += n_lines
            total_chars += len(text)

    if not entries:
        return "[Codebase Index: workspace is empty of readable project files.]"

    try:
        stats = code_index.build()
        index_note = (
            f"Semantic index: {stats['files']} files · {stats['chunks']} chunks "
            f"({stats['embedded']} embedded, {stats['reused']} reused) "
            f"via {codebase_index.EMBED_MODEL}."
        )
        log_to_ui(f"🔍 Indexed {stats['files']} files ({stats['chunks']} chunks)")
    except Exception as e:
        index_note = f"[Embedding index unavailable: {e}]"

    lines = [
        f"[Codebase Index] {len(entries)} files · {total_lines} lines · {total_chars} chars",
        f"Workspace root: {WORKSPACE_DIR}",
        index_note,
        "",
        "=== Workspace Map ===",
    ]
    for rel, n_lines, _ in entries:
        if n_lines is None:
            lines.append(f"{rel}  [binary/unreadable]")
        else:
            lines.append(f"{rel}  ({n_lines} lines)")

    if preview_lines:
        lines.append(f"\n=== File Previews (first {preview_lines} lines) ===")
        for rel, n_lines, text in entries:
            if n_lines is not None:
                lines.append(f"--- {rel} ---\n" + "\n".join(text.splitlines()[:preview_lines]))

    lines.append(
        "\nUse search_codebase(query) to retrieve the most relevant code sections "
        "before editing."
    )
    report = "\n".join(lines)
    if len(report) > MAX_INDEX_CHARS:
        report = report[:MAX_INDEX_CHARS] + "\n[... map truncated to fit context budget ...]"
    return report


def search_codebase(query: str, top_k: int = 5) -> str:
    """Semantic search across the workspace using the embedding index."""
    log_to_ui(f"🔎 Semantic search: {_short(query, 80)}")
    try:
        results = code_index.search(query, int(top_k) if top_k else 5)
    except Exception as e:
        return f"[Semantic search failed: {e}]"
    log_to_ui(f"🔎 {len(results)} section(s) matched")
    return codebase_index.format_results(results)


def search_docs(query: str, top_k: int = 5) -> str:
    """Query offline library/API documentation from the local RAG docs pool."""
    log_to_ui(f"📚 Docs lookup: {_short(query, 80)}")
    try:
        result = rag.search(query, int(top_k) if top_k else 5)
    except Exception as e:
        return f"[Docs lookup failed: {e}]"
    log_to_ui("📚 Docs matches retrieved")
    return result


# --------------------------------------------------------------------------- #
# Tool wrappers (sandbox/tools reused from secure_agent)
# --------------------------------------------------------------------------- #
def write_workspace_file(filepath: str, content) -> str:
    result = sa.write_workspace_file(filepath, content)
    if result.startswith("[Success"):
        log_to_ui(f"💾 Wrote {filepath}")
    return result


def read_workspace_file(filepath: str) -> str:
    return sa.read_workspace_file(filepath)


def list_workspace_files(path: str = ".") -> str:
    return sa.list_workspace_files(path)


def edit_workspace_file(filepath: str, old_string: str, new_string: str,
                        replace_all: bool = False) -> str:
    result = sa.edit_workspace_file(filepath, old_string, new_string, replace_all)
    if result.startswith("[Success"):
        log_to_ui(f"✏️ Edited {filepath}")
    return result


def install_dependencies(packages) -> str:
    log_to_ui(f"📦 Installing: {packages}")
    return sa.install_dependencies(packages)


def run_sandbox_command(command: str) -> str:
    log_to_ui(f"▶️ Running: {_short(command)}")
    return sa.run_sandbox_command(command)


def run_tests(test_file: str = ".", args: str = "") -> str:
    log_to_ui(f"🧪 pytest {test_file} {args}".strip())
    return sa.run_tests(target=test_file or ".", args=args)


tools_map = {
    "scan_codebase_index": scan_codebase_index,
    "search_codebase": search_codebase,
    "search_docs": search_docs,
    "write_workspace_file": write_workspace_file,
    "read_workspace_file": read_workspace_file,
    "list_workspace_files": list_workspace_files,
    "edit_workspace_file": edit_workspace_file,
    "install_dependencies": install_dependencies,
    "run_sandbox_command": run_sandbox_command,
    "run_tests": run_tests,
}

MUTATING_TOOLS = {"write_workspace_file", "edit_workspace_file", "run_sandbox_command"}

tools_definition = [
    {
        "type": "function",
        "function": {
            "name": "scan_codebase_index",
            "description": (
                "Scan the entire workspace and return a structured map of every "
                "file with line/char counts, and refresh the semantic embedding "
                "index. Use this FIRST to understand pre-existing code, then use "
                "search_codebase to read relevant sections."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_codebase",
            "description": (
                "Semantic search over the workspace via a local nomic-embed-text "
                "index. Returns the most relevant code sections (path, line range, "
                "code). Use it to locate where a behavior lives before editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language or code query"},
                    "top_k": {"type": "integer", "description": "Number of sections (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": (
                "Query offline library/API documentation and manuals from a local "
                "docs pool (no internet). Use this whenever you are unsure about an "
                "API, module or library usage; it returns matching doc sections."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What you want to look up"},
                    "top_k": {"type": "integer", "description": "Number of sections (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_workspace_file",
            "description": "Create or overwrite a file (source or test_*.py) in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "e.g. auth.py or pkg/main.py"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["filepath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_file",
            "description": "Read the full contents of a workspace file.",
            "parameters": {
                "type": "object",
                "properties": {"filepath": {"type": "string"}},
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workspace_files",
            "description": "List files and subdirectories in a workspace folder.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "default '.'"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_workspace_file",
            "description": "Replace an exact string in a workspace file. Prefer over rewriting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["filepath", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_dependencies",
            "description": "Install Python packages into the shared sandbox venv (only networked step).",
            "parameters": {
                "type": "object",
                "properties": {"packages": {"type": "string"}},
                "required": ["packages"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sandbox_command",
            "description": "Run a shell command offline inside the sandbox (e.g. 'python auth.py').",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run pytest for a test file in the sandbox. On success the Git "
                "guard commits a checkpoint; on failure the output is returned so "
                "you can fix the code and re-run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_file": {"type": "string", "description": "e.g. test_auth.py (default '.')"},
                    "args": {"type": "string", "description": "optional extra pytest flags"},
                },
                "required": ["test_file"],
            },
        },
    },
]

_test_state = {"last_signature": None, "count": 0}


def _handle_test_result(result: str) -> str:
    """Apply the Git lifecycle rules to a run_tests result and return any note."""
    if "[TESTS PASSED" in result:
        _test_state["last_signature"] = None
        _test_state["count"] = 0
        guard.commit_success("feat: agent progress — tests passing")
        return ""
    if "[TESTS FAILED" in result or "tests timed out" in result:
        signature = result[:400]
        if signature == _test_state["last_signature"]:
            _test_state["count"] += 1
        else:
            _test_state["last_signature"] = signature
            _test_state["count"] = 1
        log_to_ui("❌ Tests failed — piping output back to the model")
        if _test_state["count"] >= UNRECOVERABLE_LIMIT:
            guard.rollback()
            _test_state["last_signature"] = None
            _test_state["count"] = 0
            return (
                "\n\n[Git Guard: the same failure repeated "
                f"{UNRECOVERABLE_LIMIT} times, so the workspace was HARD-ROLLED-BACK "
                "to the last passing state. Your recent edits were discarded — "
                "re-read the files and apply a different fix.]"
            )
    return ""


def execute_tool(name: str, args: dict) -> str:
    tool = tools_map.get(name)
    if tool is None:
        return f"[Error: Tool '{name}' not implemented]"

    if name in MUTATING_TOOLS:
        guard.snapshot(f"before {name}")

    try:
        result = tool(**args)
    except TypeError as e:
        return f"[Error: invalid arguments for {name}: {e}]"
    except Exception as e:
        return f"[Error: {name} failed: {e}]"

    if name == "run_tests":
        result += _handle_test_result(result)
    return result


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are an advanced self-correcting development agent working in a "
    "version-controlled sandbox. "
    "FIRST STEP: call scan_codebase_index to understand what files already exist. "
    "Use search_codebase(query) to semantically find relevant existing code "
    "(it searches a local embedding index) before writing or editing. "
    "When unsure about a library, module or API, call search_docs(query) to "
    "consult the offline documentation pool instead of guessing. "
    "Then follow a TEST-DRIVEN workflow: write the implementation, write a "
    "test_*.py file asserting the required behavior, and call run_tests. If it "
    "fails, read the output, fix the code (or the test), and call run_tests again "
    "until it passes. A passing run_tests is the only proof of success. "
    "If a tool result says the Git guard performed a ROLLBACK, your uncommitted "
    "edits were discarded: re-read the files and try a different approach. "
    "When done, reply with a short plain-text summary (no JSON, no tool call)."
)


def _safe_arguments(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def agent_orchestrator(prompt: str, max_steps: int = MAX_STEPS):
    guard.ensure_repo()
    sa.ensure_sandbox_image()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    log_to_ui("🤖 Advanced Git/Scanner agent started")

    last_signature = None
    repeat_count = 0

    for step in range(max_steps):
        log_to_ui(f"🧠 Step {step + 1}: querying model")
        response = llm_config.chat_completion(
            messages,
            model=MODEL_NAME,
            tools=tools_definition,
            tool_choice="auto",
            temperature=0.1,
            max_tokens=sa.LLM_MAX_TOKENS,
        )
        message = response.choices[0].message
        messages.append(llm_config.assistant_message(message))

        native = message.tool_calls or []
        if native:
            calls = [
                (tc.function.name, _safe_arguments(tc.function.arguments), tc.id)
                for tc in native
            ]
        else:
            parsed = sa.parse_text_tool_calls(message.content or "")
            real = [(n, a) for n, a in parsed if n in tools_map]
            if not real:
                log_to_ui(f"🏁 Final answer: {_short(message.content or '', 240)}")
                break
            calls = [(n, a, None) for n, a in real]

        signature = tuple(sorted(
            (c[0], json.dumps(c[1], sort_keys=True)) for c in calls
        ))
        if signature == last_signature:
            repeat_count += 1
        else:
            repeat_count = 0
            last_signature = signature
        if repeat_count >= 2:
            log_to_ui("🛑 Stopping: identical tool calls repeated without progress")
            break

        results = []
        for name, args, call_id in calls:
            log_to_ui(f"🔧 {name}({_short(args, 120)})")
            output = execute_tool(name, args)
            log_to_ui(f"   ↩ {_short(output, 180)}")
            if call_id is not None:
                messages.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "name": name,
                    "content": output,
                })
            else:
                results.append(f"Tool '{name}' returned:\n{output}")

        if results:
            messages.append({
                "role": "user",
                "content": "\n\n".join(results)
                + "\n\nContinue: call the next tool or give the final answer.",
            })
    else:
        log_to_ui(f"⚠️ Reached max steps ({max_steps}) without a final answer")


# --------------------------------------------------------------------------- #
# Rich live dashboard
# --------------------------------------------------------------------------- #
def generate_directory_tree(path: str, tree: Tree = None, status: dict = None) -> Tree:
    name = os.path.basename(path.rstrip("/")) or path
    if tree is None:
        tree = Tree(f"📁 [bold blue]{escape(name)}/[/]")
    status = status or {}
    try:
        for item in sorted(os.listdir(path)):
            if item.startswith(".") or item in SCAN_SKIP_DIRS:
                continue
            full = os.path.join(path, item)
            rel = os.path.relpath(full, WORKSPACE_DIR)
            if os.path.isdir(full):
                branch = tree.add(f"📁 [bold yellow]{escape(item)}/[/]")
                generate_directory_tree(full, branch, status)
            else:
                code = status.get(rel, "")
                if item.startswith("test_"):
                    style = "bold cyan"
                elif code and code != "??":
                    style = "bold yellow"
                elif code == "??":
                    style = "bold green"
                else:
                    style = "green"
                tree.add(f"📄 [{style}]{escape(item)}[/]")
    except OSError:
        pass
    return tree


def make_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(name="tree", ratio=1),
        Layout(name="logs", ratio=2),
    )
    return layout


def _refresh(layout: Layout, console: Console):
    global git_status_cache
    try:
        git_status_cache = guard.status_map()
    except Exception:
        pass

    layout["header"].update(Panel(
        Text.from_markup(
            f"[bold magenta]Advanced Dev Agent[/]  ·  model [cyan]{MODEL_NAME}[/]\n"
            f"[grey50]{escape(WORKSPACE_DIR)}  ·  HEAD {guard.head()}  ·  "
            f"last_good {guard.last_good()}[/]"
        ),
        style="on grey11",
    ))
    layout["tree"].update(Panel(
        generate_directory_tree(WORKSPACE_DIR, status=git_status_cache),
        title="📂 Git Workspace Indexer", border_style="magenta",
    ))
    visible = max(6, console.size.height - 6)
    log_text = Text("\n".join(list(ui_logs)[-visible:])) if ui_logs else Text("waiting…", style="grey50")
    layout["logs"].update(Panel(
        log_text, title="📟 Resilient TDD Console", border_style="green",
    ))
    layout["footer"].update(Text.from_markup(
        "[grey50]green = new · yellow = modified · cyan = tests"
        f"  ·  index {_index_summary()}  ·  Ctrl-C to quit[/]"
    ))


def _index_summary() -> str:
    try:
        st = code_index.stats()
        if st["files"]:
            return f"{st['files']} files / {st['chunks']} chunks"
    except Exception:
        pass
    return "not built"


def run_with_ui(prompt: str):
    guard.ensure_repo()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(agent_orchestrator, prompt)
    layout = make_layout()
    console = Console()
    try:
        with Live(layout, console=console, refresh_per_second=4, screen=True) as live:
            while not future.done():
                _refresh(layout, console)
                time.sleep(0.2)
            _refresh(layout, console)
    except KeyboardInterrupt:
        log_to_ui("⏹️ Interrupted by user")

    try:
        future.result()
    except Exception as e:
        console.print(f"[red]Agent error: {e}[/]")

    console.print(
        f"🎉 Task complete! Inspect history with:\n"
        f"   git -C '{WORKSPACE_DIR}' log --oneline --decorate"
    )


def main():
    import argparse

    default_task = (
        "Scan the workspace to look for any pre-existing modules. Then create an "
        "application file 'auth.py' with a function validate_password(pwd) that "
        "returns True only if the password is longer than 6 characters and "
        "contains at least one digit. Create 'test_auth.py' covering valid and "
        "invalid passwords, run the tests, fix any errors until they pass."
    )
    parser = argparse.ArgumentParser(description="Advanced Git-guarded TDD coding agent.")
    parser.add_argument("task", nargs="?", default=default_task,
                        help="Task for the agent (defaults to the auth demo).")
    args = parser.parse_args()
    run_with_ui(args.task)


if __name__ == "__main__":
    main()
