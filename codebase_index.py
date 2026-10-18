#!/usr/bin/env python3
"""
Embedding-backed codebase index for semantic retrieval.

Files are chunked and embedded with Ollama's `nomic-embed-text` (via the
OpenAI-compatible embeddings endpoint). The index is cached on disk, keyed by a
content hash per file, so only changed files are re-embedded on each build.

Used by `advanced_dev_agent.py` through the `search_codebase` tool, and usable
standalone:

    python codebase_index.py build
    python codebase_index.py search "password validation"
"""

import os
import json
import math
import time
import hashlib

import secure_agent as sa

EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
INDEX_PATH = os.path.abspath(os.path.expanduser(
    os.environ.get("CODEBASE_INDEX_PATH", "~/.cache/local-cursor/codebase_index.json")
))
CHUNK_LINES = int(os.environ.get("INDEX_CHUNK_LINES", "40"))
CHUNK_OVERLAP = int(os.environ.get("INDEX_CHUNK_OVERLAP", "10"))
EMBED_BATCH = int(os.environ.get("INDEX_EMBED_BATCH", "32"))
SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".venv", "venv",
    "node_modules", ".mypy_cache", ".ruff_cache",
}


def _normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


class CodebaseIndex:
    """A durable, incrementally-built semantic index over the workspace."""

    def __init__(self, workspace: str = sa.WORKSPACE_DIR, model: str = EMBED_MODEL,
                 path: str = INDEX_PATH, extensions=None):
        self.ws = workspace
        self.model = model
        self.path = path
        self.extensions = {e.lower() for e in extensions} if extensions else None
        self.data = {"model": model, "dim": None, "files": {}, "built_at": None}

    # -- persistence -------------------------------------------------------- #
    def load(self):
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and "files" in loaded:
                    self.data = loaded
            except (json.JSONDecodeError, OSError):
                pass
        return self.data

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    # -- walking / chunking ------------------------------------------------- #
    def _iter_files(self):
        for root, dirs, names in os.walk(self.ws):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(names):
                if name.startswith("."):
                    continue
                if self.extensions and os.path.splitext(name)[1].lower() not in self.extensions:
                    continue
                full = os.path.join(root, name)
                yield os.path.relpath(full, self.ws), full

    @staticmethod
    def _read_text(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (UnicodeDecodeError, OSError):
            return None

    @staticmethod
    def _chunk(text):
        lines = text.splitlines()
        if not lines:
            return []
        step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
        chunks = []
        for start in range(0, len(lines), step):
            window = lines[start:start + CHUNK_LINES]
            if not window:
                break
            chunks.append({
                "start": start + 1,
                "end": start + len(window),
                "text": "\n".join(window),
            })
            if start + CHUNK_LINES >= len(lines):
                break
        return chunks

    def _embed(self, texts):
        vectors = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            resp = sa.client.embeddings.create(model=self.model, input=batch)
            vectors.extend(_normalize(d.embedding) for d in resp.data)
        return vectors

    # -- build -------------------------------------------------------------- #
    def build(self, force: bool = False) -> dict:
        self.load()
        old_files = self.data.get("files", {})
        new_files = {}
        embedded = reused = skipped = 0

        for rel, full in self._iter_files():
            text = self._read_text(full)
            if text is None:
                skipped += 1
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            prev = old_files.get(rel)
            if prev and prev.get("hash") == digest and not force:
                new_files[rel] = prev
                reused += 1
                continue

            chunks = self._chunk(text)
            vectors = self._embed([c["text"] for c in chunks]) if chunks else []
            for chunk, vec in zip(chunks, vectors):
                chunk["vec"] = vec
            new_files[rel] = {
                "hash": digest,
                "lines": len(text.splitlines()),
                "chunks": chunks,
            }
            embedded += 1

        self.data = {
            "model": self.model,
            "dim": len(next(iter(new_files.values()))["chunks"][0]["vec"]) if new_files and
                   next(iter(new_files.values()))["chunks"] else self.data.get("dim"),
            "files": new_files,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.save()
        return {
            "files": len(new_files),
            "embedded": embedded,
            "reused": reused,
            "skipped": skipped,
            "chunks": sum(len(m["chunks"]) for m in new_files.values()),
        }

    # -- search ------------------------------------------------------------- #
    def search(self, query: str, top_k: int = 5):
        self.build()
        if not query.strip():
            return []
        query_vec = _embed_query(self, query)
        scored = []
        for rel, meta in self.data["files"].items():
            for chunk in meta["chunks"]:
                scored.append({
                    "path": rel,
                    "start": chunk["start"],
                    "end": chunk["end"],
                    "score": _cosine(query_vec, chunk["vec"]),
                    "text": chunk["text"],
                })
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]

    def stats(self) -> dict:
        files = self.data.get("files", {})
        return {
            "files": len(files),
            "chunks": sum(len(m["chunks"]) for m in files.values()),
            "dim": self.data.get("dim"),
            "built_at": self.data.get("built_at"),
        }


def _embed_query(index: CodebaseIndex, query: str):
    return _normalize(sa.client.embeddings.create(model=index.model, input=[query]).data[0].embedding)


def format_results(results) -> str:
    if not results:
        return "[No semantic matches found in the codebase index.]"
    blocks = []
    for r in results:
        blocks.append(
            f"--- {r['path']} (lines {r['start']}-{r['end']}, score {r['score']:.3f}) ---\n"
            f"{r['text']}"
        )
    return "\n\n".join(blocks)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Build or query the codebase embedding index.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    q = sub.add_parser("search")
    q.add_argument("query")
    q.add_argument("-k", "--top-k", type=int, default=5)
    args = parser.parse_args()

    index = CodebaseIndex()
    if args.cmd == "build":
        print(json.dumps(index.build(), indent=2))
    else:
        for r in index.search(args.query, args.top_k):
            preview = r["text"].splitlines()[0][:70]
            print(f"{r['score']:.3f}  {r['path']}:{r['start']}  {preview}")


if __name__ == "__main__":
    main()
