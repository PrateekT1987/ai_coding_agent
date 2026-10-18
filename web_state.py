#!/usr/bin/env python3
"""
Thread-safe in-process memory pool for the multi-agent web interface.

The multi-agent graph appends its full conversational state (chat messages,
tool executions, format/lint/test results, node transitions and metrics) to a
single global :data:`POOL`. A FastAPI server (``web_server.py``) reads snapshots
of this pool and serves them to the browser, which polls once per second.

The pool is pure standard library (no web deps), so the CLI agents can import
and write to it without pulling in FastAPI/uvicorn.
"""

import copy
import itertools
import threading
import time


class MemoryPool:
    """A lock-guarded dict-like store for the live agent session."""

    def __init__(self, max_events: int = 4000, keep_events: int = 2500):
        self._lock = threading.RLock()
        self._max_events = max_events
        self._keep_events = keep_events
        self.reset()

    # -- lifecycle -------------------------------------------------------- #
    def reset(self, task: str = "", model: str = ""):
        """Clear all state and start a fresh session."""
        with self._lock:
            self._counter = itertools.count(1)
            self._last_id = 0
            self.meta = {
                "task": task,
                "model": model,
                "status": "idle",          # idle | running | done | error
                "result": None,            # approved | rolled_back | error
                "revision": 0,
                "max_revisions": 0,
                "started_at": None,
                "finished_at": None,
            }
            self.nodes = {}                 # name -> {"status", "note"}
            self.events = []                # list of event dicts
            self.metrics = {
                "messages": 0,
                "tool_calls": 0,
                "dev_steps": 0,
                "lint_runs": 0,
                "test_runs": 0,
                "format_runs": 0,
                "revisions": 0,
                "files_changed": [],
                "reject_count": 0,
            }
            self.git = {"head": "", "last_good": "", "status": {}}
            self.hitl = {
                "paused": False,
                "wait_for": None,           # "approve" | "feedback"
                "reason": "",
                "reject_count": 0,
                "feedback": "",
            }
            return self

    def begin(self, task: str, model: str, max_revisions: int = 0):
        self.reset(task=task, model=model)
        with self._lock:
            self.meta["status"] = "running"
            self.meta["started_at"] = time.time()
            self.meta["max_revisions"] = max_revisions
        return self

    def finish(self, result: str, error: str = ""):
        with self._lock:
            self.meta["status"] = "error" if result == "error" else "done"
            self.meta["result"] = result
            self.meta["finished_at"] = time.time()
        self.emit("finished", result=result, error=error)

    # -- writers ---------------------------------------------------------- #
    def emit(self, type: str, **fields) -> dict:
        """Append an event and return it (with its id/ts)."""
        with self._lock:
            self._last_id += 1
            event = {"id": self._last_id, "type": type, "ts": time.time(), **fields}
            self.events.append(event)
            if len(self.events) > self._max_events:
                self.events = self.events[-self._keep_events:]
            return event

    def message(self, role: str, content: str, **fields):
        """Record a conversational chat message (developer/reviewer/tool/system)."""
        with self._lock:
            self.metrics["messages"] = self.metrics.get("messages", 0) + 1
        return self.emit("message", role=role, content=str(content), **fields)

    def set_node(self, name: str, status: str, note: str = ""):
        with self._lock:
            self.nodes[name] = {"status": status, "note": note}
            if name == "review" and status == "revise":
                self.hitl["reject_count"] = self.hitl.get("reject_count", 0) + 1
                self.metrics["reject_count"] = self.hitl["reject_count"]
        return self.emit("node", node=name, status=status, note=note)

    def update_meta(self, **kwargs):
        with self._lock:
            self.meta.update(kwargs)

    def bump(self, key: str, amount: int = 1):
        with self._lock:
            self.metrics[key] = self.metrics.get(key, 0) + amount

    def set_git(self, head: str = None, last_good: str = None, status: dict = None):
        with self._lock:
            if head is not None:
                self.git["head"] = head
            if last_good is not None:
                self.git["last_good"] = last_good
            if status is not None:
                self.git["status"] = status

    # -- readers ---------------------------------------------------------- #
    def snapshot(self, since: int = 0, include_meta: bool = True) -> dict:
        """Return a JSON-serialisable snapshot; events filtered by ``id > since``."""
        with self._lock:
            data = {
                "last_id": self._last_id,
                "events": copy.deepcopy([e for e in self.events if e["id"] > since]),
            }
            if include_meta:
                data.update({
                    "meta": copy.deepcopy(self.meta),
                    "nodes": copy.deepcopy(self.nodes),
                    "metrics": copy.deepcopy(self.metrics),
                    "git": copy.deepcopy(self.git),
                    "hitl": copy.deepcopy(self.hitl),
                })
            return data


# The single global pool shared by the graph and the web server.
POOL = MemoryPool()
