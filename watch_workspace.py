#!/usr/bin/env python3
"""
Live terminal view of the agent workspace.

Polls the workspace and redraws the file tree + a scrolling change log,
highlighting files that were just added or modified. Run it in a second
terminal while secure_agent.py works.

    python watch_workspace.py
    python watch_workspace.py --path ~/some/dir --interval 0.3
"""

import os
import sys
import time
import argparse
import datetime
from collections import deque

from colorama import Fore, Style, init

import secure_agent

init(autoreset=True)

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
            rel = os.path.relpath(abs_path, root)
            snapshot[rel] = (st.st_size, st.st_mtime_ns)
    return snapshot


def render_tree(root: str, changed: dict) -> list:
    """Return the workspace as a list of colored tree lines.

    `changed` maps relpath -> one of 'added' / 'modified'.
    """
    lines = []

    def color_for(rel):
        state = changed.get(rel)
        if state == "added":
            return Fore.GREEN
        if state == "modified":
            return Fore.YELLOW
        return ""

    def walk(abs_dir, rel_dir, prefix):
        try:
            entries = sorted(os.listdir(abs_dir))
        except OSError:
            return
        entries = [e for e in entries if e not in IGNORED]
        dirs = [e for e in entries if os.path.isdir(os.path.join(abs_dir, e))]
        files = [e for e in entries if not os.path.isdir(os.path.join(abs_dir, e))]
        ordered = dirs + files

        for i, name in enumerate(ordered):
            last = i == len(ordered) - 1
            connector = "└── " if last else "├── "
            abs_path = os.path.join(abs_dir, name)
            rel = os.path.join(rel_dir, name) if rel_dir else name

            if os.path.isdir(abs_path):
                lines.append(f"{prefix}{connector}{Fore.CYAN}{name}/{Style.RESET_ALL}")
                walk(abs_path, rel, prefix + ("    " if last else "│   "))
            else:
                try:
                    size = os.path.getsize(abs_path)
                except OSError:
                    size = 0
                lines.append(
                    f"{prefix}{connector}{color_for(rel)}{name}"
                    f"{Style.RESET_ALL} {Fore.LIGHTBLACK_EX}{human_size(size)}{Style.RESET_ALL}"
                )

    lines.append(f"{Fore.CYAN}{os.path.basename(root.rstrip('/')) or root}/{Style.RESET_ALL}")
    walk(root, "", "")
    return lines


def watch(root: str, interval: float, include_all: bool, max_log: int):
    events = deque(maxlen=max_log)
    previous = {}
    changed = {}

    def log(message: str, color: str = ""):
        stamp = Fore.LIGHTBLACK_EX + datetime.datetime.now().strftime("%H:%M:%S")
        events.append(f"{stamp}{Style.RESET_ALL}  {color}{message}{Style.RESET_ALL}")

    sys.stdout.write("\033[2J\033[3J\033[H")  # clear screen + scrollback
    sys.stdout.flush()

    try:
        while True:
            if not os.path.isdir(root):
                os.makedirs(root, exist_ok=True)

            current = build_snapshot(root, include_all)

            added = {p for p in current if p not in previous}
            removed = {p for p in previous if p not in current}
            modified = {
                p for p in current
                if p in previous and previous[p] != current[p]
            }

            for p in sorted(added):
                log(f"+ {p}", Fore.GREEN)
            for p in sorted(modified):
                log(f"~ {p}", Fore.YELLOW)
            for p in sorted(removed):
                log(f"- {p}", Fore.RED)

            changed = {p: "added" for p in added}
            changed.update({p: "modified" for p in modified})

            tree = render_tree(root, changed)
            total = sum(size for size, _ in current.values())

            out = []
            out.append(
                f"{Fore.MAGENTA}Local Cursor — workspace watcher{Style.RESET_ALL}  "
                f"{Fore.LIGHTBLACK_EX}{root}{Style.RESET_ALL}"
            )
            out.append(
                f"{Fore.LIGHTBLACK_EX}{len(current)} files · "
                f"{human_size(total)} · updated "
                f"{datetime.datetime.now().strftime('%H:%M:%S')}"
                f" · Ctrl-C to quit{Style.RESET_ALL}"
            )
            out.append("")
            out.extend(tree)
            out.append("")
            out.append(f"{Fore.MAGENTA}Activity{Style.RESET_ALL}")
            if events:
                out.extend(events)
            else:
                out.append(f"{Fore.LIGHTBLACK_EX}(waiting for changes…){Style.RESET_ALL}")

            sys.stdout.write("\033[H" + "\n".join(out) + "\033[J")
            sys.stdout.flush()

            previous = current
            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n{Fore.CYAN}Stopped watching {root}{Style.RESET_ALL}")


def main():
    parser = argparse.ArgumentParser(description="Watch the agent workspace live.")
    parser.add_argument(
        "--path",
        default=secure_agent.WORKSPACE_DIR,
        help="Directory to watch (default: the agent workspace).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Polling interval in seconds (default: 0.5).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include normally ignored folders (__pycache__, .git, ...).",
    )
    parser.add_argument(
        "--max-log",
        type=int,
        default=12,
        help="Number of recent change events to keep (default: 12).",
    )
    args = parser.parse_args()
    watch(os.path.abspath(os.path.expanduser(args.path)), args.interval, args.all, args.max_log)


if __name__ == "__main__":
    main()
