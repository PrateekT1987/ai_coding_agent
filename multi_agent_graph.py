#!/usr/bin/env python3
"""
Multi-Agent Graph: Developer ↔ Code-Reviewer with automatic format + lint.

Two agents share the same configured model (see `llm_config`) but run in separate
system contexts:

  * Developer Agent  — turns the task (+ any reviewer feedback) into source
    files and pytest unit tests.
  * Code Reviewer Agent — runs a strict quality payload inside the Docker
    sandbox (black auto-format, flake8 lint, pytest), then either APPROVES the
    change or sends structured feedback back to the Developer for revision.

The graph: develop → format → lint → test → review → (commit | develop …)

Only when the code is formatted, lint-clean, tests pass AND the reviewer
approves is a permanent Git checkpoint committed; otherwise the loop revises,
and on exhaustion the workspace is rolled back to the last passing state.
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
from rich.text import Text
from rich.console import Console
from rich.markup import escape

import secure_agent as sa
import advanced_dev_agent as ada
import llm_config
import web_state

MODEL_NAME = llm_config.MODEL_NAME
MAX_REVISIONS = int(os.environ.get("AGENT_MAX_REVISIONS", "2"))
DEV_MAX_STEPS = int(os.environ.get("AGENT_DEV_MAX_STEPS", "8"))
FLAKE8_MAX_LINE = int(os.environ.get("FLAKE8_MAX_LINE", "100"))
FORMAT_TIMEOUT = int(os.environ.get("SANDBOX_FORMAT_TIMEOUT", "120"))
LINT_TIMEOUT = int(os.environ.get("SANDBOX_LINT_TIMEOUT", "120"))

log_to_ui = ada.log_to_ui
ui_logs = ada.ui_logs
WORKSPACE_DIR = ada.WORKSPACE_DIR
guard = ada.guard

GRAPH_NODES = ["develop", "format", "lint", "test", "review", "commit"]
_node_status = {name: "pending" for name in GRAPH_NODES}
_revision = {"n": 0, "current": ""}

DEV_ALLOWED = {
    "scan_codebase_index", "search_codebase", "search_docs",
    "write_workspace_file", "read_workspace_file", "list_workspace_files",
    "edit_workspace_file", "install_dependencies",
}
DEV_TOOLS = [
    t for t in ada.tools_definition if t["function"]["name"] in DEV_ALLOWED
]


def set_node(name: str, status: str, note: str = ""):
    _node_status[name] = status
    if note:
        log_to_ui(f"   {note}")
    web_state.POOL.set_node(name, status, note)


# --------------------------------------------------------------------------- #
# Deterministic quality payload (format / lint / test)
# --------------------------------------------------------------------------- #
def ensure_formatters():
    ok = (
        sa._sandbox_run(["python", "-m", "black", "--version"], 60).returncode == 0
        and sa._sandbox_run(["python", "-m", "flake8", "--version"], 60).returncode == 0
    )
    if not ok:
        log_to_ui("📦 Installing black + flake8 into the sandbox venv")
        sa.install_dependencies("black flake8")


def run_format(files):
    if not files:
        return True, ""
    res = sa._sandbox_run(["python", "-m", "black", "--quiet", *files], FORMAT_TIMEOUT)
    out = (res.stdout + res.stderr).strip()
    if out:
        log_to_ui(f"🎨 black: {out.replace(chr(10), ' ⏎ ')[:160]}")
    ok = res.returncode == 0
    web_state.POOL.bump("format_runs")
    web_state.POOL.emit("format", ok=ok, files=list(files), output=out, exit=res.returncode)
    return ok, out


def run_lint(files):
    if not files:
        web_state.POOL.bump("lint_runs")
        web_state.POOL.emit("lint", ok=False, files=[], output="No Python files were changed.",
                            exit=1)
        return False, "No Python files were changed."
    res = sa._sandbox_run(
        ["python", "-m", "flake8", *files, f"--max-line-length={FLAKE8_MAX_LINE}",
         "--extend-ignore=E203,W503"],
        LINT_TIMEOUT,
    )
    out = (res.stdout + res.stderr).strip()
    ok = res.returncode == 0
    web_state.POOL.bump("lint_runs")
    web_state.POOL.emit("lint", ok=ok, files=list(files), output=out, exit=res.returncode)
    return ok, out


def _changed_py_files():
    """Python files changed since last_good (incl. those in wip snapshots)."""
    try:
        paths = guard.changed_paths()
    except Exception:
        return []
    return sorted({
        path for path in paths
        if path.endswith(".py") and os.path.isfile(os.path.join(WORKSPACE_DIR, path))
    })


def run_tests_scoped(test_files):
    """Run pytest only on the changed test files (fallback: whole workspace)."""
    targets = test_files or ["."]
    log_to_ui(f"🧪 pytest {' '.join(targets)}")
    web_state.POOL.emit("test_started", targets=list(targets))
    res = sa._sandbox_run(["python", "-m", "pytest", "-q", "--color=no", *targets],
                          sa.TEST_TIMEOUT)
    out = (res.stdout + res.stderr).strip()
    ok = res.returncode == 0
    web_state.POOL.bump("test_runs")
    web_state.POOL.emit("test", ok=ok, targets=list(targets), output=out,
                        exit=res.returncode)
    return ok, out


def collect_sources(limit: int = 8000, files=None) -> str:
    """Collect Python source for review.

    If ``files`` (workspace-relative paths) is given, only those files are
    collected; otherwise every .py file in the workspace is returned.
    """
    if files is not None:
        paths = [os.path.join(WORKSPACE_DIR, f) for f in files]
    else:
        paths = []
        for root, dirs, names in os.walk(WORKSPACE_DIR):
            dirs[:] = sorted(d for d in dirs
                             if d not in ada.SCAN_SKIP_DIRS and not d.startswith("."))
            for name in sorted(names):
                if name.endswith(".py"):
                    paths.append(os.path.join(root, name))

    blocks = []
    size = 0
    for path in paths:
        if not path.endswith(".py") or not os.path.isfile(path):
            continue
        rel = os.path.relpath(path, WORKSPACE_DIR)
        try:
            text = open(path, "r", encoding="utf-8").read()
        except OSError:
            continue
        block = f"### {rel}\n{text}"
        if size + len(block) > limit:
            blocks.append("... [more files omitted] ...")
            return "\n\n".join(blocks)
        blocks.append(block)
        size += len(block)
    return "\n\n".join(blocks) or "(no Python files yet)"


# --------------------------------------------------------------------------- #
# Developer Agent (separate system context)
# --------------------------------------------------------------------------- #
DEV_SYSTEM = (
    "You are the DEVELOPER AGENT. You translate the task into clean, working "
    "Python source files and comprehensive pytest unit tests. This environment "
    "only formats, lints and tests Python (.py) files, so implement the feature "
    "in Python. The sandbox is HEADLESS and OFFLINE: never use GUI/display or "
    "network libraries (tkinter, PyQt, pygame, requests to external hosts, etc.); "
    "keep the logic pure-Python and unit-testable. "
    "Explore first with scan_codebase_index and search_codebase; "
    "write code with write_workspace_file and make small fixes with "
    "edit_workspace_file. "
    "Rules for tests: name each test file 'test_<module>.py' and put it in the "
    "SAME directory as the module under test. Only the FILENAME gets the 'test_' "
    "prefix — never prefix directory names (write 'src/components/test_login.py', "
    "NOT 'test_src/components/test_login.py'). "
    "Rules for content: always write the complete, real implementation — never "
    "placeholder text such as '<!-- ... -->' or 'your code here'. "
    "Do not run tests yourself — a reviewer does that. "
    "If reviewer feedback is provided, address every point precisely. "
    "When your files are ready, reply with a short plain-text summary (no tool call)."
)


def _safe_args(raw):
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def developer_node(task: str, feedback: str) -> None:
    log_to_ui(f"👨\u200d💻 Developer Agent: {'revision' if feedback else 'initial build'}")
    web_state.POOL.message(
        "developer",
        f"Working on the task ({'revision' if feedback else 'initial build'}).",
        phase="start", revision=_revision["n"],
    )
    if feedback:
        web_state.POOL.message("developer", feedback, phase="feedback",
                               label="Reviewer feedback received")
    user = f"Task:\n{task}"
    if feedback:
        user += f"\n\nReviewer/lint feedback to address:\n{feedback}"
    messages = [
        {"role": "system", "content": DEV_SYSTEM},
        {"role": "user", "content": user},
    ]

    last_sig, repeat = None, 0
    for step in range(DEV_MAX_STEPS):
        log_to_ui(f"👨\u200d💻 Dev step {step + 1}: thinking")
        web_state.POOL.bump("dev_steps")
        web_state.POOL.emit("dev_step", step=step + 1, budget=DEV_MAX_STEPS)
        resp = llm_config.chat_completion(
            messages,
            model=MODEL_NAME, tools=DEV_TOOLS,
            tool_choice="auto", temperature=0.1, max_tokens=sa.LLM_MAX_TOKENS,
        )
        message = resp.choices[0].message
        messages.append(llm_config.assistant_message(message))

        native = message.tool_calls or []
        if native:
            calls = [(tc.function.name, _safe_args(tc.function.arguments), tc.id) for tc in native]
        else:
            real = [(n, a) for n, a in sa.parse_text_tool_calls(message.content or "")
                    if n in DEV_ALLOWED]
            if not real:
                log_to_ui(f"👨\u200d💻 Dev done: {ada._short(message.content or '', 140)}")
                web_state.POOL.message(
                    "developer", (message.content or "").strip() or "(no summary)",
                    phase="done", revision=_revision["n"],
                )
                return
            calls = [(n, a, None) for n, a in real]

        if (message.content or "").strip():
            web_state.POOL.message(
                "developer", message.content.strip(), phase="thinking",
                revision=_revision["n"], step=step + 1,
            )

        sig = tuple(sorted((c[0], json.dumps(c[1], sort_keys=True)) for c in calls))
        if sig == last_sig:
            repeat += 1
        else:
            repeat, last_sig = 0, sig
        if repeat >= 2:
            log_to_ui("👨\u200d💻 Dev stopped: repeated identical tool calls")
            web_state.POOL.message("developer", "Stopped: repeated identical tool calls.",
                                   phase="done", revision=_revision["n"])
            return

        results = []
        for name, args, call_id in calls:
            log_to_ui(f"👨\u200d💻 {name}({ada._short(args, 100)})")
            web_state.POOL.bump("tool_calls")
            web_state.POOL.emit("tool_call", agent="developer", tool=name, args=args)
            output = ada.execute_tool(name, args)
            log_to_ui(f"   ↩ {ada._short(output, 140)}")
            web_state.POOL.emit("tool_result", agent="developer", tool=name,
                                output=output, ok=not str(output).startswith("[Error"))
            if call_id is not None:
                messages.append({"tool_call_id": call_id, "role": "tool",
                                 "name": name, "content": output})
            else:
                results.append(f"Tool '{name}' returned:\n{output}")
        if results:
            messages.append({
                "role": "user",
                "content": "\n\n".join(results) + "\n\nContinue or give your final summary.",
            })
    log_to_ui(f"👨\u200d💻 Dev reached step budget ({DEV_MAX_STEPS})")
    web_state.POOL.message("developer", f"Reached step budget ({DEV_MAX_STEPS}).",
                           phase="done", revision=_revision["n"])


# --------------------------------------------------------------------------- #
# Code Reviewer Agent (separate system context)
# --------------------------------------------------------------------------- #
REVIEW_SYSTEM = (
    "You are the CODE REVIEWER AGENT. Carefully evaluate the source files and unit "
    "tests against the task. Do NOT trust a passing test suite as proof of "
    "correctness — the tests may be incomplete or wrong. Trace the code line by line "
    "and verify the ACTUAL returned value against what the task requires. For every "
    "function, check edge cases: empty input, a single element, even vs odd length, "
    "duplicates, already-sorted vs unsorted input, boundary values, and any special "
    "case named in the task. Explicitly check each requirement in the task. If ANY "
    "requirement or common edge case is handled incorrectly, OR the tests do not "
    "cover it, verdict must be 'revise' with the exact defect and a fix. Only approve "
    "when the implementation is correct AND adequately tested. "
    "Reply ONLY with a JSON object: "
    '{"verdict": "approve" or "revise", "feedback": "<specific, actionable points>"}.'
)


def _parse_verdict(content: str):
    text = (content or "").strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:]).rsplit("```", 1)[0]
    text = sa._repair_json_strings(text)
    start = text.find("{")
    if start != -1:
        try:
            data, _ = json.JSONDecoder().raw_decode(text, start)
            verdict = str(data.get("verdict", "")).lower()
            return ("approve" if "approve" in verdict else "revise",
                    str(data.get("feedback", "")).strip())
        except Exception:
            pass
    lowered = text.lower()
    if "revise" in lowered:
        return "revise", text[:600]
    if "approve" in lowered:
        return "approve", text[:600]
    return "revise", text[:600]


def reviewer_node(task: str, format_note: str, lint_ok: bool, lint_out: str,
                  test_ok: bool, test_out: str, files=None):
    sources = collect_sources(files=files)
    payload = (
        f"Task:\n{task}\n\n"
        f"black (formatter): {format_note or 'no changes'}\n"
        f"flake8: {'clean' if lint_ok else 'issues'}\n{lint_out or '(none)'}\n\n"
        f"pytest: {'PASSED' if test_ok else 'FAILED'}\n{ada._short(test_out, 1200)}\n\n"
        f"Changed files:\n{sources}"
    )
    web_state.POOL.message(
        "reviewer",
        f"Reviewing the change — flake8: {'clean' if lint_ok else 'issues'}, "
        f"pytest: {'passed' if test_ok else 'failed'}. Comparing sources against the task.",
        phase="reviewing", revision=_revision["n"],
    )
    resp = llm_config.chat_completion(
        [{"role": "system", "content": REVIEW_SYSTEM},
         {"role": "user", "content": payload}],
        model=MODEL_NAME,
        temperature=0.0, max_tokens=sa.LLM_MAX_TOKENS,
    )
    verdict, feedback = _parse_verdict(resp.choices[0].message.content)
    log_to_ui(f"🧑\u200d🏫 Reviewer verdict: {verdict.upper()}")
    if feedback:
        log_to_ui(f"🧑\u200d🏫 {ada._short(feedback, 180)}")
    web_state.POOL.message(
        "reviewer", feedback or ("Looks good." if verdict == "approve" else "Please revise."),
        phase=verdict, revision=_revision["n"], verdict=verdict,
    )
    return verdict, feedback


# --------------------------------------------------------------------------- #
# Graph orchestration
# --------------------------------------------------------------------------- #
def _reset_nodes():
    for name in GRAPH_NODES:
        _node_status[name] = "pending"


def _sync_git_to_pool():
    try:
        web_state.POOL.set_git(head=guard.head(), last_good=guard.last_good(),
                               status=guard.status_map())
    except Exception:
        pass


def run_graph(task: str) -> bool:
    guard.ensure_repo()
    sa.ensure_sandbox_image()
    ensure_formatters()

    web_state.POOL.begin(task, MODEL_NAME, max_revisions=MAX_REVISIONS)
    web_state.POOL.message(
        "system",
        f"Multi-Agent Graph started · model {MODEL_NAME} · up to {MAX_REVISIONS} revision(s).",
        phase="start",
    )
    _sync_git_to_pool()
    log_to_ui("🕸️  Multi-Agent Graph started")
    feedback = ""
    for revision in range(MAX_REVISIONS + 1):
        _revision["n"] = revision + 1
        web_state.POOL.update_meta(revision=revision + 1)
        if revision:
            web_state.POOL.bump("revisions")
        _reset_nodes()

        set_node("develop", "running")
        developer_node(task, feedback)

        changed = _changed_py_files()
        web_state.POOL.metrics["files_changed"] = list(changed)
        log_to_ui(f"🔧 Reviewing {len(changed)} changed file(s): {', '.join(changed) or '(none)'}")

        if not changed:
            set_node("develop", "fail", "no files written")
            feedback = (
                "You did not create or modify any Python files, so there is nothing "
                "to review or test. Write the implementation (a .py module) and a "
                "pytest test file named test_<module>.py in the requested folder, "
                "then reply with a short summary."
            )
            web_state.POOL.emit("revision", number=revision + 1, feedback=feedback)
            log_to_ui("↻ No files were written → asking Developer to produce them")
            continue

        set_node("develop", "pass")

        set_node("format", "running")
        format_ok, format_out = run_format(changed)
        set_node("format", "pass" if format_ok else "fail",
                 f"format {'ok' if format_ok else 'failed'}")

        set_node("lint", "running")
        lint_ok, lint_out = run_lint(changed)
        set_node("lint", "pass" if lint_ok else "fail",
                 "flake8 clean" if lint_ok else "flake8 reported issues")

        set_node("test", "running")
        test_files = [p for p in changed if os.path.basename(p).startswith("test_")]
        test_ok, test_report = run_tests_scoped(test_files)
        set_node("test", "pass" if test_ok else "fail",
                 "pytest passed" if test_ok else "pytest failed")

        set_node("review", "running")
        verdict, review_feedback = reviewer_node(
            task, format_out, lint_ok, lint_out, test_ok, test_report, files=changed
        )

        approved = (verdict == "approve") and lint_ok and test_ok
        if approved:
            set_node("review", "pass")
            set_node("commit", "running")
            guard.commit_success("feat: reviewer-approved, formatted change")
            set_node("commit", "pass", "Git checkpoint committed")
            _sync_git_to_pool()
            web_state.POOL.emit("commit", message="feat: reviewer-approved, formatted change",
                                head=guard.head(), last_good=guard.last_good())
            log_to_ui("✅ Reviewer APPROVED — change committed")
            web_state.POOL.finish("approved")
            return True

        set_node("review", "revise")
        reject_count = web_state.POOL.hitl.get("reject_count", 0)
        parts = []
        if not lint_ok:
            parts.append("flake8 reported the following issues — fix them:\n" + lint_out)
        if not test_ok:
            parts.append("pytest failed — fix the code/tests:\n" + ada._short(test_report, 1600))
        if review_feedback:
            parts.append("Reviewer feedback:\n" + review_feedback)
        feedback = "\n\n".join(parts) or "Tests did not pass; revise the implementation."
        web_state.POOL.emit("revision", number=revision + 1, feedback=feedback)
        log_to_ui(f"↻ Revision {revision + 1} requested → sending feedback to Developer")

        if reject_count > 2:
            log_to_ui("⏸️ Reviewer rejected > 2 times — pausing for human input")
            web_state.POOL.emit("hitl_paused", reason="Reviewer rejected more than twice.",
                                reject_count=reject_count)
            with web_state.POOL._lock:
                web_state.POOL.hitl["paused"] = True
                web_state.POOL.hitl["wait_for"] = "feedback"
                web_state.POOL.hitl["reason"] = "Reviewer rejected more than twice."
            while True:
                with web_state.POOL._lock:
                    if not web_state.POOL.hitl.get("paused", False):
                        feedback = web_state.POOL.hitl.get("feedback") or feedback
                        break
                time.sleep(1)
            web_state.POOL.emit("hitl_resumed", feedback_len=len(feedback))


    log_to_ui("🚫 Revisions exhausted — rolling back to the last passing state")
    guard.rollback()
    _sync_git_to_pool()
    web_state.POOL.emit("rollback", message="Revisions exhausted; workspace reset to last_good")
    web_state.POOL.finish("rolled_back")
    return False


# --------------------------------------------------------------------------- #
# Rich live dashboard
# --------------------------------------------------------------------------- #
_STATUS_STYLE = {
    "pending": ("○", "grey50"),
    "running": ("▶", "bold cyan"),
    "pass": ("✓", "bold green"),
    "fail": ("✗", "bold red"),
    "revise": ("↻", "bold yellow"),
}
_NODE_LABEL = {
    "develop": "Developer Agent",
    "format": "Black formatter",
    "lint": "Flake8 linter",
    "test": "Pytest",
    "review": "Reviewer Agent",
    "commit": "Git commit",
}


def make_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(name="graph", ratio=1),
        Layout(name="logs", ratio=2),
        Layout(name="tree", ratio=1),
    )
    return layout


def _graph_panel() -> Panel:
    text = Text()
    for i, name in enumerate(GRAPH_NODES):
        icon, style = _STATUS_STYLE.get(_node_status[name], ("○", "grey50"))
        text.append(f" {icon} ", style=style)
        text.append(f"{_NODE_LABEL[name]}\n", style="white")
        if i < len(GRAPH_NODES) - 1:
            arrow = " │" if name != "review" else " ├─ (revise ↺)\n │"
            text.append(f" {arrow}\n", style="grey37")
    return Panel(text, title="🕸️ Agent Graph", border_style="magenta", padding=(0, 1))


def _refresh(layout: Layout, console: Console):
    try:
        status = guard.status_map()
    except Exception:
        status = {}
    layout["header"].update(Panel(Text.from_markup(
        f"[bold magenta]Multi-Agent Graph[/]  ·  model [cyan]{MODEL_NAME}[/]  ·  "
        f"revision [yellow]{_revision['n']}[/]  ·  "
        f"[grey50]HEAD {guard.head()} · last_good {guard.last_good()}[/]"
    ), style="on grey11"))
    layout["graph"].update(_graph_panel())
    visible = max(6, console.size.height - 6)
    layout["logs"].update(Panel(
        Text("\n".join(list(ui_logs)[-visible:])) if ui_logs else Text("waiting…", style="grey50"),
        title="📟 Agent Console", border_style="green",
    ))
    layout["tree"].update(Panel(
        ada.generate_directory_tree(WORKSPACE_DIR, status=status),
        title="📂 Workspace", border_style="blue",
    ))
    layout["footer"].update(Text.from_markup(
        "[grey50]develop → format → lint → test → review → commit  ·  Ctrl-C to quit[/]"
    ))


def run_with_ui(task: str):
    guard.ensure_repo()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(run_graph, task)
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
        ok = future.result()
    except Exception as e:
        console.print(f"[red]Graph error: {e}[/]")
        ok = False
    console.print(
        f"{'✅ Approved & committed' if ok else '🚫 Not approved (rolled back)'}.\n"
        f"   git -C '{WORKSPACE_DIR}' log --oneline --decorate"
    )


def main():
    import argparse
    default_task = (
        "Create a module stringutils.py with a function is_palindrome(s) that "
        "ignores case, spaces and punctuation, and a function reverse_words(s). "
        "Write comprehensive pytest tests in test_stringutils.py covering "
        "palindromes, empty strings and multiple words."
    )
    parser = argparse.ArgumentParser(description="Developer ↔ Reviewer multi-agent graph.")
    parser.add_argument("task", nargs="?", default=default_task)
    parser.add_argument("--web", action="store_true",
                        help="Serve the FastAPI web console instead of the terminal dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="Open the browser (with --web).")
    args = parser.parse_args()
    if args.web:
        import web_server
        web_server.serve(host=args.host, port=args.port, task=args.task,
                         open_browser=args.open)
    else:
        run_with_ui(args.task)


if __name__ == "__main__":
    main()
