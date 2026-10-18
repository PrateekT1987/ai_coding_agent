#!/usr/bin/env python3
"""
Rich terminal monitor for the agent workspace.

A split-screen, auto-refreshing view:
  * left  — live activity log (agent output + file change events)
  * right — real-time directory tree of the workspace
  * footer — counters and key hints

Run it while the agent works, optionally streaming the agent itself:

    python rich_monitor.py
    python rich_monitor.py --exec "python secure_agent.py 'write fib.py ...'"
    python rich_monitor.py --path ~/agent_workspace --interval 0.3
"""

import os
import sys
import time
import shutil
import argparse
import datetime
import threading
import subprocess
from collections import deque

from rich.text import Text
from rich.tree import Tree
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.console import Console
from rich.markup import escape

import secure_agent

IGNORED = {"__pycache__", ".git", ".venv", "node_modules"}


def human_size(num: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if num < 1024 or unit == "G":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num}B"


def build_snapshot(root: str, include_all: bool) -> dict:
    """Map every file under root to (size, mtime_ns)."""
    snapshot = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_all:
            dirnames[:] = [d for d in dirnames if d not in IGNORED]
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            try:
                st = os.stat(abs_path)
            except OSError:
                continue
            snapshot[os.path.relpath(abs_path, root)] = (st.st_size, st.st_mtime_ns)
    return snapshot


class MonitorState:
    """Thread-safe shared state between the watcher/runner and the renderer."""

    def __init__(self, root: str, max_log: int = 300):
        self.root = root
        self.lock = threading.Lock()
        self.log = deque(maxlen=max_log)
        self.snapshot = {}
        self.changed = {}
        self.total_size = 0
        self.running_cmd = None

    def add_log(self, message: str, style: str = ""):
        with self.lock:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log.append((ts, message.rstrip("\n"), style))

    def apply_snapshot(self, current: dict, changed: dict):
        with self.lock:
            self.snapshot = current
            self.changed = changed
            self.total_size = sum(size for size, _ in current.values())


def watch_loop(state: MonitorState, interval: float, include_all: bool, stop: threading.Event):
    previous = {}
    state.add_log(f"watching {state.root}", "bold cyan")
    while not stop.is_set():
        if not os.path.isdir(state.root):
            os.makedirs(state.root, exist_ok=True)
        current = build_snapshot(state.root, include_all)

        added = {p for p in current if p not in previous}
        removed = {p for p in previous if p not in current}
        modified = {p for p in current if p in previous and previous[p] != current[p]}

        for p in sorted(added):
            state.add_log(f"+ {p}", "bold green")
        for p in sorted(modified):
            state.add_log(f"~ {p}", "bold yellow")
        for p in sorted(removed):
            state.add_log(f"- {p}", "bold red")

        changed = {p: "added" for p in added}
        changed.update({p: "modified" for p in modified})
        state.apply_snapshot(current, changed)

        previous = current
        stop.wait(interval)


def command_loop(cmd: str, state: MonitorState, stop: threading.Event):
    state.add_log(f"$ {cmd}", "bold magenta")
    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=os.getcwd(),
        )
    except Exception as e:
        state.add_log(f"failed to start command: {e}", "bold red")
        return

    with state.lock:
        state.running_cmd = cmd
    try:
        for line in proc.stdout:
            state.add_log(line, "white")
    except Exception:
        pass
    proc.wait()
    state.add_log(f"command exited with code {proc.returncode}", "bold magenta")
    with state.lock:
        state.running_cmd = None


def build_tree(root: str, changed: dict, include_all: bool) -> Tree:
    root_label = os.path.basename(root.rstrip("/")) or root
    tree = Tree(f"[bold cyan]{escape(root_label)}/[/]", guide_style="grey37")

    def add(dir_abs: str, node):
        try:
            entries = sorted(os.listdir(dir_abs))
        except OSError:
            return
        entries = [e for e in entries if include_all or e not in IGNORED]
        dirs = [e for e in entries if os.path.isdir(os.path.join(dir_abs, e))]
        files = [e for e in entries if not os.path.isdir(os.path.join(dir_abs, e))]

        for name in dirs + files:
            abs_path = os.path.join(dir_abs, name)
            rel = os.path.relpath(abs_path, root)
            if os.path.isdir(abs_path):
                branch = node.add(f"[bold blue]{escape(name)}/[/]")
                add(abs_path, branch)
            else:
                state = changed.get(rel)
                color = {"added": "bold green", "modified": "bold yellow"}.get(state, "white")
                try:
                    size = human_size(os.path.getsize(abs_path))
                except OSError:
                    size = "?"
                node.add(f"[{color}]{escape(name)}[/] [grey50]{size}[/]")

    add(root, tree)
    return tree


def build_log(state: MonitorState, height: int) -> Text:
    text = Text()
    with state.lock:
        entries = list(state.log)[-max(1, height):]
    if not entries:
        text.append("waiting for activity…", style="grey50")
        return text
    for ts, message, style in entries:
        text.append(f"{ts} ", style="grey50")
        text.append(message + "\n", style=style or "white")
    return text


def make_layout(state: MonitorState, console: Console) -> Layout:
    height = console.size.height
    body_height = max(3, height - 4)

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(name="log", ratio=2),
        Layout(name="tree", ratio=3),
    )

    with state.lock:
        files = len(state.snapshot)
        total = human_size(state.total_size)
        running = state.running_cmd

    status = f"[green]running[/] {escape(running)}" if running else "[grey50]idle[/]"
    layout["header"].update(Panel(
        Text.from_markup(
            f"[bold magenta]Local Cursor — monitor[/]   {status}\n"
            f"[grey50]{escape(state.root)}[/]"
        ),
        style="on grey11",
    ))
    layout["log"].update(Panel(
        build_log(state, body_height),
        title="[bold]Activity", border_style="cyan", padding=(0, 1),
    ))
    layout["tree"].update(Panel(
        build_tree(state.root, state.changed, include_all=state.include_all),
        title="[bold]Workspace", border_style="green", padding=(0, 1),
    ))
    layout["footer"].update(Text.from_markup(
        f"[grey50]{files} files · {total} · {datetime.datetime.now().strftime('%H:%M:%S')}"
        f"  ·  Ctrl-C to quit[/]"
    ))
    return layout


def main():
    parser = argparse.ArgumentParser(description="Rich live monitor for the agent workspace.")
    parser.add_argument("--path", default=secure_agent.WORKSPACE_DIR,
                        help="Directory to watch (default: the agent workspace).")
    parser.add_argument("--interval", type=float, default=0.4,
                        help="File polling interval in seconds (default: 0.4).")
    parser.add_argument("--max-log", type=int, default=300,
                        help="Maximum log lines retained (default: 300).")
    parser.add_argument("--exec", dest="command",
                        help="Optional command to run and stream into the activity log.")
    parser.add_argument("--all", action="store_true",
                        help="Include normally ignored folders (__pycache__, .git, ...).")
    args = parser.parse_args()

    root = os.path.abspath(os.path.expanduser(args.path))
    state = MonitorState(root, args.max_log)
    state.include_all = args.all
    stop = threading.Event()

    threads = [threading.Thread(
        target=watch_loop, args=(state, args.interval, args.all, stop), daemon=True)]
    if args.command:
        threads.append(threading.Thread(
            target=command_loop, args=(args.command, state, stop), daemon=True))
    for t in threads:
        t.start()

    console = Console()
    try:
        with Live(make_layout(state, console), console=console,
                  refresh_per_second=8, screen=True) as live:
            while not stop.is_set():
                live.update(make_layout(state, console))
                time.sleep(0.12)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        console.print("[cyan]monitor stopped[/]")


if __name__ == "__main__":
    main()
