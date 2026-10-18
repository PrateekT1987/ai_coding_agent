#!/usr/bin/env python3
"""
Lightweight FastAPI server for the Developer ↔ Reviewer multi-agent graph.

The graph (``multi_agent_graph.run_graph``) appends its full conversational
state into the in-process :data:`web_state.POOL`; this server exposes that pool
as JSON and serves a single-file Tailwind frontend that polls it once per second.

Run it directly::

    python web_server.py                  # serve the UI (idle until you start a run)
    python web_server.py --task "..."      # serve + start a run immediately
    python web_server.py --port 8080

or through the graph CLI::

    python multi_agent_graph.py --web "build a palindrome module"

Both the server and the agent run in one process, so they share the memory pool
without any IPC.
"""

import os
import threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

import web_state
import multi_agent_graph as graph

HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "web_ui.html")

DEFAULT_TASK = (
    "Create a module stringutils.py with a function is_palindrome(s) that "
    "ignores case, spaces and punctuation, and a function reverse_words(s). "
    "Write comprehensive pytest tests in test_stringutils.py covering "
    "palindromes, empty strings and multiple words."
)

app = FastAPI(title="Local Cursor · Multi-Agent Web UI", version="1.0")

_run_lock = threading.Lock()
_running = {"active": False}


def _current_task() -> str:
    return web_state.POOL.meta.get("task") or ""


def _runner(task: str):
    try:
        graph.run_graph(task)
    except Exception as exc:  # surface crashes to the UI instead of dying quietly
        web_state.POOL.emit("error", error=f"{type(exc).__name__}: {exc}")
        web_state.POOL.finish("error", error=str(exc))
    finally:
        _running["active"] = False
        try:
            _run_lock.release()
        except RuntimeError:
            pass


def start_run(task: str, reset: bool = False) -> bool:
    """Acquire the single-run lock and launch the graph in a daemon thread.

    Returns ``False`` if a run is already in progress. When ``reset`` is True the
    memory pool is cleared *immediately* so the UI drops the old conversation
    before the worker thread even starts.
    """
    if not _run_lock.acquire(blocking=False):
        return False
    _running["active"] = True
    if reset:
        web_state.POOL.begin(task, graph.MODEL_NAME, max_revisions=graph.MAX_REVISIONS)
    threading.Thread(target=_runner, args=(task,), daemon=True).start()
    return True


@app.get("/", response_class=HTMLResponse)
def index():
    if os.path.exists(UI_FILE):
        with open(UI_FILE, "r", encoding="utf-8") as fh:
            return HTMLResponse(fh.read())
    return HTMLResponse("<h1>web_ui.html not found</h1>", status_code=500)


@app.get("/api/state")
def api_state(since: int = 0, meta: int = 1):
    """Return the pool snapshot. ``since`` is the last event id the client saw."""
    return JSONResponse(web_state.POOL.snapshot(since=since, include_meta=bool(meta)))


@app.get("/api/status")
def api_status():
    return {
        "running": _running["active"],
        "status": web_state.POOL.meta.get("status", "idle"),
        "result": web_state.POOL.meta.get("result"),
        "task": _current_task(),
    }


@app.post("/api/run")
async def api_run(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    task = (body or {}).get("task") or DEFAULT_TASK
    if not start_run(task, reset=True):
        return JSONResponse({"ok": False, "error": "A run is already in progress."},
                            status_code=409)
    return {"ok": True, "task": task}


@app.post("/api/hitl/approve")
async def api_hitl_approve(request: Request):
    try:
        with web_state.POOL._lock:
            if not web_state.POOL.hitl.get("paused"):
                return JSONResponse({"ok": False, "error": "Not paused for HITL approval"}, status_code=400)
            web_state.POOL.hitl["paused"] = False
            web_state.POOL.hitl["wait_for"] = None
            web_state.POOL.hitl["feedback"] = ""
    except Exception:
        pass
    web_state.POOL.emit("hitl_approved", by="human")
    return {"ok": True}


@app.post("/api/hitl/feedback")
async def api_hitl_feedback(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    feedback = str((body or {}).get("feedback", "")).strip()
    if not feedback:
        return JSONResponse({"ok": False, "error": "Feedback cannot be empty"}, status_code=400)
    try:
        with web_state.POOL._lock:
            if not web_state.POOL.hitl.get("paused"):
                return JSONResponse({"ok": False, "error": "Not paused for HITL feedback"}, status_code=400)
            web_state.POOL.hitl["paused"] = False
            web_state.POOL.hitl["wait_for"] = None
            web_state.POOL.hitl["feedback"] = feedback
    except Exception:
        pass
    web_state.POOL.emit("hitl_guidance", feedback=feedback)
    web_state.POOL.message("system", f"Human guidance provided:\n{feedback}", phase="hitl")
    return {"ok": True}


@app.post("/api/submit-task")
async def api_submit_task(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    task = str((body or {}).get("task", "")).strip()
    if not task:
        return JSONResponse({"ok": False, "error": "Task cannot be empty."}, status_code=400)
    if not start_run(task, reset=True):
        return JSONResponse({"ok": False, "error": "The agents are already working on a task."},
                            status_code=409)
    return {"ok": True, "task": task}


def serve(host: str = "127.0.0.1", port: int = 8000, task: str = None,
          open_browser: bool = False):
    """Run the API + UI, optionally auto-starting a build."""
    url = f"http://{host}:{port}/"
    print(f"🌐 Multi-Agent Web UI → {url}")
    print("   Start a run from the page, or POST /api/run {\"task\": \"...\"}.")
    if task:
        print(f"   Auto-starting task: {task[:80]}{'…' if len(task) > 80 else ''}")
        start_run(task)
    if open_browser:
        threading.Timer(1.0, lambda: __import__("webbrowser").open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Multi-Agent graph web interface.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--task", default=None,
                        help="Start this task immediately (defaults to the demo).")
    parser.add_argument("--run", action="store_true",
                        help="Auto-start the built-in demo task on launch.")
    parser.add_argument("--open", action="store_true", help="Open the browser.")
    args = parser.parse_args()

    task = args.task or (DEFAULT_TASK if args.run else None)
    serve(host=args.host, port=args.port, task=task, open_browser=args.open)


if __name__ == "__main__":
    main()
