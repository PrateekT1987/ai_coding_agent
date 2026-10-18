#!/usr/bin/env python3
"""
Local RAG for offline documentation.

Drop `.md` / `.txt` / `.rst` manuals, API references or cheatsheets into the
docs pool and the agent can semantically query them with no internet access.
Embeddings come from Ollama's `nomic-embed-text`; ranking is plain cosine
similarity over an on-disk, incrementally-built index (no Chroma/FAISS needed).

    python local_rag.py path                # show the docs pool location
    python local_rag.py build               # (re)build the docs index
    python local_rag.py search "requests.Session retries" -k 3
"""

import os
import json

import secure_agent as sa
import codebase_index
from codebase_index import CodebaseIndex, format_results

DOCS_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("DOCS_DIR", os.path.join(os.path.dirname(sa.WORKSPACE_DIR), "local_docs"))
))
DOCS_INDEX_PATH = os.path.abspath(os.path.expanduser(
    os.environ.get("DOCS_INDEX_PATH", "~/.cache/local-cursor/docs_index.json")
))
DOC_EXTS = {".md", ".txt", ".rst", ".markdown"}

README = """# Local documentation pool

Put offline documentation, API references and manuals here as `.md`, `.txt`
or `.rst` files. The agent searches this folder through the `search_docs` tool
using local embeddings (`nomic-embed-text`) — no internet required.

Examples:
  * `python-stdlib.md`      — stdlib cheatsheet
  * `requests-api.txt`      — HTTP client reference
  * `pandas-cookbook.md`    — dataframe recipes
"""

SAMPLE_STDLIB = """# Python standard library cheatsheet

## functools
`functools.partial(func, *args)` freezes some arguments of a callable and
returns a new callable. `functools.lru_cache(maxsize=None)` memoizes a function.
`functools.reduce(func, iterable)` folds an iterable into a single value.

## itertools
`itertools.count(start, step)` yields an infinite arithmetic progression.
`itertools.chain(a, b)` concatenates iterables. `itertools.groupby(data, key)`
groups consecutive items sharing a key. `itertools.combinations(seq, r)` yields
r-length subsequences.

## json
`json.dumps(obj, indent=2)` serializes to a pretty string. `json.loads(text)`
parses JSON into Python objects. Use `default=str` to serialize non-JSON types.

## pathlib
`pathlib.Path('a/b.txt').read_text()` reads a file. `Path.write_text(data)`
writes one. `Path.glob('**/*.py')` recursively matches files. `Path.mkdir(
parents=True, exist_ok=True)` creates directories.

## re
`re.sub(pattern, repl, text)` replaces matches. `re.findall(pattern, text)`
returns all matches. Compile with `re.compile` for reuse. `\\d` matches a digit,
`\\w` a word character, `\\s` whitespace.

## dataclasses
`@dataclass` auto-generates `__init__`, `__repr__` and `__eq__`. Use
`field(default_factory=list)` for mutable defaults.
"""


class LocalRAG:
    """Embedding search over a folder of local documentation files."""

    def __init__(self, docs_dir: str = DOCS_DIR, index_path: str = DOCS_INDEX_PATH,
                 model: str = codebase_index.EMBED_MODEL):
        self.docs_dir = docs_dir
        self.index = CodebaseIndex(
            workspace=docs_dir, model=model, path=index_path, extensions=DOC_EXTS
        )

    def ensure_pool(self):
        os.makedirs(self.docs_dir, exist_ok=True)
        has_docs = any(
            os.path.splitext(f)[1].lower() in DOC_EXTS for f in os.listdir(self.docs_dir)
        )
        if not has_docs:
            with open(os.path.join(self.docs_dir, "README.md"), "w", encoding="utf-8") as f:
                f.write(README)
            with open(os.path.join(self.docs_dir, "python-stdlib.md"), "w", encoding="utf-8") as f:
                f.write(SAMPLE_STDLIB)

    def build(self) -> dict:
        self.ensure_pool()
        return self.index.build()

    def search(self, query: str, top_k: int = 5) -> str:
        self.build()
        results = self.index.search(query, int(top_k) if top_k else 5)
        results = [r for r in results if r["score"] > 0]
        if not results:
            return (
                "[Local RAG: no relevant documentation found in the docs pool "
                f"({self.docs_dir}).]"
            )
        return (
            f"[Local docs] {len(results)} match(es) from {self.docs_dir}:\n\n"
            + format_results(results)
        )

    def list_docs(self) -> str:
        self.ensure_pool()
        docs = sorted(
            f for f in os.listdir(self.docs_dir)
            if os.path.splitext(f)[1].lower() in DOC_EXTS
        )
        return f"Docs pool ({self.docs_dir}):\n" + "\n".join(docs)

    def stats(self) -> dict:
        st = self.index.stats()
        st["docs_dir"] = self.docs_dir
        return st


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Local RAG over offline documentation.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("path")
    sub.add_parser("build")
    sub.add_parser("list")
    q = sub.add_parser("search")
    q.add_argument("query")
    q.add_argument("-k", "--top-k", type=int, default=5)
    args = parser.parse_args()

    rag = LocalRAG()
    if args.cmd == "path":
        print(rag.docs_dir)
    elif args.cmd == "build":
        print(json.dumps(rag.build(), indent=2))
    elif args.cmd == "list":
        print(rag.list_docs())
    else:
        print(rag.search(args.query, args.top_k))


if __name__ == "__main__":
    main()
