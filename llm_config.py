#!/usr/bin/env python3
"""
Central LLM configuration for the AI coding agent.

Loads provider settings from ``.env`` (project root) and exposes a single
configured OpenAI-compatible client used by every agent module:

    from llm_config import client, MODEL_NAME, chat_completion

Supported providers (``LLM_PROVIDER``):

* ``openrouter`` (default) – OpenRouter (https://openrouter.ai)
* ``openai``               – OpenAI or any OpenAI-compatible endpoint
* ``ollama`` / ``local``   – a local Ollama server
* ``custom``               – any OpenAI-compatible ``LLM_BASE_URL``

Run ``python -m llm_config --check`` to validate the configuration and
verify connectivity without exposing secrets.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

PROJECT_ROOT = Path(__file__).resolve().parent
# ``override=False`` keeps values already exported in the real environment
# (container/CI secrets) ahead of the local file.
load_dotenv(PROJECT_ROOT / ".env", override=False)


class ConfigError(RuntimeError):
    """Raised when the LLM configuration is missing or invalid."""


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    """Return the first non-empty environment value among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _int_env(default: int, *names: str) -> int:
    raw = _env(*names)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(
            f"Environment variable {names[0]} must be an integer, got {raw!r}"
        ) from None


# --------------------------------------------------------------------------- #
# Provider resolution
# --------------------------------------------------------------------------- #
LLM_PROVIDER = (_env("LLM_PROVIDER", default="openrouter") or "openrouter").lower()


def _resolve_provider(provider: str) -> Dict[str, Any]:
    if provider == "openrouter":
        return {
            "base_url": _env("OPENROUTER_BASE_URL",
                             default="https://openrouter.ai/api/v1"),
            "api_key": _env("OPENROUTER_API_KEY"),
            "model": _env("OPENROUTER_MODEL",
                          default="nvidia/nemotron-3-ultra-550b-a55b:free"),
            "headers": {
                "HTTP-Referer": _env("OPENROUTER_SITE_URL",
                                     default="https://github.com/PrateekT1987/ai_coding_agent"),
                "X-Title": _env("OPENROUTER_APP_NAME",
                                default="local-cursor AI Coding Agent"),
            },
        }
    if provider == "openai":
        return {
            "base_url": _env("OPENAI_BASE_URL", default="https://api.openai.com/v1"),
            "api_key": _env("OPENAI_API_KEY"),
            "model": _env("OPENAI_MODEL", default="gpt-4o-mini"),
            "headers": {},
        }
    if provider in ("ollama", "local"):
        return {
            "base_url": _env("OLLAMA_BASE_URL",
                             default="http://localhost:11434/v1"),
            "api_key": _env("OLLAMA_API_KEY", default="ollama"),
            "model": _env("OLLAMA_MODEL", default="qwen2.5-coder:3b"),
            "headers": {},
        }
    if provider == "custom":
        return {
            "base_url": _env("LLM_BASE_URL"),
            "api_key": _env("LLM_API_KEY"),
            "model": _env("LLM_MODEL"),
            "headers": {},
        }
    raise ConfigError(
        f"Unsupported LLM_PROVIDER={provider!r}. "
        "Use one of: openrouter, openai, ollama, custom."
    )


_PROVIDER = _resolve_provider(LLM_PROVIDER)

BASE_URL: Optional[str] = _PROVIDER["base_url"]
# ``AGENT_MODEL`` stays supported as a global override for backwards compat.
MODEL_NAME: str = _env("AGENT_MODEL", default=_PROVIDER["model"]) or ""
_API_KEY: Optional[str] = _PROVIDER["api_key"]
_DEFAULT_HEADERS: Dict[str, str] = _PROVIDER["headers"]

if not BASE_URL:
    raise ConfigError(
        f"Missing base URL for provider {LLM_PROVIDER!r}. "
        "Set the provider's *_BASE_URL (e.g. OPENROUTER_BASE_URL) or LLM_BASE_URL."
    )
if not MODEL_NAME:
    raise ConfigError(
        f"Missing model for provider {LLM_PROVIDER!r}. "
        "Set the provider's *_MODEL (e.g. OPENROUTER_MODEL) or AGENT_MODEL."
    )

# Generation / transport limits. Reasoning models need head-room for their
# hidden "reasoning" tokens before the visible answer is emitted.
LLM_MAX_TOKENS: int = _int_env(4096, "AGENT_LLM_MAX_TOKENS", "LLM_MAX_TOKENS")
LLM_TIMEOUT: float = float(_int_env(300, "AGENT_LLM_TIMEOUT", "LLM_TIMEOUT"))
LLM_TEMPERATURE: float = float(_env("LLM_TEMPERATURE", default="0") or 0)
LLM_MAX_RETRIES: int = _int_env(4, "LLM_MAX_RETRIES")
LLM_RETRY_BASE_SECONDS: float = float(_env("LLM_RETRY_BASE_SECONDS", default="2") or 2)

# A missing key is only fatal when the client is actually used, so importing
# modules that do not call the LLM (e.g. the web UI state) still works.
_MISSING_KEY = not _API_KEY

client = OpenAI(
    base_url=BASE_URL,
    api_key=_API_KEY or "MISSING_API_KEY",
    timeout=LLM_TIMEOUT,
    max_retries=0,  # retries are handled explicitly in chat_completion()
    default_headers=_DEFAULT_HEADERS or None,
)


# --------------------------------------------------------------------------- #
# Embeddings (used by the codebase / docs RAG indexes)
# --------------------------------------------------------------------------- #
# Chat providers such as OpenRouter do not serve embedding models, so
# embeddings are configured independently and default to a local Ollama
# server. Set EMBEDDINGS_PROVIDER=none to disable semantic search entirely.
EMBEDDINGS_PROVIDER = (
    _env("EMBEDDINGS_PROVIDER", default="ollama") or "ollama"
).lower()


def _resolve_embeddings(provider: str) -> Dict[str, Any]:
    if provider in ("none", "off", "disabled"):
        return {"base_url": None, "api_key": None,
                "model": _env("EMBED_MODEL", default="nomic-embed-text")}
    if provider in ("ollama", "local"):
        return {
            "base_url": _env("OLLAMA_BASE_URL", default="http://localhost:11434/v1"),
            "api_key": _env("OLLAMA_API_KEY", default="ollama"),
            "model": _env("EMBED_MODEL", default="nomic-embed-text"),
        }
    if provider == "openai":
        return {
            "base_url": _env("OPENAI_BASE_URL", default="https://api.openai.com/v1"),
            "api_key": _env("OPENAI_API_KEY"),
            "model": _env("EMBED_MODEL", default="text-embedding-3-small"),
        }
    if provider == "custom":
        return {
            "base_url": _env("EMBED_BASE_URL", "LLM_BASE_URL"),
            "api_key": _env("EMBED_API_KEY", "LLM_API_KEY"),
            "model": _env("EMBED_MODEL", default="nomic-embed-text"),
        }
    raise ConfigError(
        f"Unsupported EMBEDDINGS_PROVIDER={provider!r}. "
        "Use one of: ollama, openai, custom, none."
    )


_EMBED = _resolve_embeddings(EMBEDDINGS_PROVIDER)
EMBED_MODEL: str = _EMBED["model"] or "nomic-embed-text"
EMBEDDINGS_ENABLED: bool = bool(_EMBED["base_url"])
EMBED_BATCH_SIZE: int = _int_env(32, "INDEX_EMBED_BATCH")

embedding_client: Optional[OpenAI] = None
if EMBEDDINGS_ENABLED:
    embedding_client = OpenAI(
        base_url=_EMBED["base_url"],
        api_key=_EMBED["api_key"] or "EMBEDDINGS_DISABLED",
        timeout=LLM_TIMEOUT,
        max_retries=0,
    )


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #
def image_of(model: Optional[str] = None) -> str:
    """Human-readable ``provider/model`` label for logs and UI."""
    return f"{LLM_PROVIDER}/{model or MODEL_NAME}"


def validate() -> None:
    """Fail fast with an actionable message if the configuration is unusable."""
    if _MISSING_KEY:
        env_name = {
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
            "custom": "LLM_API_KEY",
        }.get(LLM_PROVIDER, "LLM_API_KEY")
        raise ConfigError(
            f"No API key configured for provider {LLM_PROVIDER!r}. "
            f"Set {env_name} in {PROJECT_ROOT / '.env'} or the environment."
        )
    if LLM_MAX_TOKENS <= 0:
        raise ConfigError("LLM_MAX_TOKENS must be a positive integer.")


def embed_texts(
    texts: List[str],
    *,
    model: Optional[str] = None,
    batch_size: Optional[int] = None,
) -> List[List[float]]:
    """Embed ``texts`` using the configured embeddings provider.

    Raises :class:`ConfigError` when embeddings are disabled or unreachable so
    callers can degrade gracefully instead of crashing the whole agent.
    """
    if not EMBEDDINGS_ENABLED or embedding_client is None:
        raise ConfigError(
            "Embeddings are disabled (EMBEDDINGS_PROVIDER=none). "
            "Set EMBEDDINGS_PROVIDER=ollama with a local Ollama server to "
            "enable semantic search."
        )
    size = max(1, int(batch_size or EMBED_BATCH_SIZE))
    vectors: List[List[float]] = []
    attempts = max(1, LLM_MAX_RETRIES)
    for start in range(0, len(texts), size):
        batch = texts[start:start + size]
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                resp = embedding_client.embeddings.create(
                    model=model or EMBED_MODEL, input=batch
                )
                vectors.extend(d.embedding for d in resp.data)
                break
            except _RETRYABLE as exc:  # transient
                last_error = exc
                if attempt >= attempts:
                    raise ConfigError(
                        f"Embedding request failed after {attempts} attempt(s): {exc}"
                    ) from exc
                time.sleep(LLM_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
            except Exception as exc:  # noqa: BLE001 - local server may be down
                raise ConfigError(
                    f"Embedding request failed ({type(exc).__name__}): {exc}. "
                    "Is the embedding server running?"
                ) from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise ConfigError(f"Embedding request failed: {last_error}")
    return vectors


def assistant_message(message: Any) -> Dict[str, Any]:
    """Return a clean assistant message safe to send back to the provider.

    Drops provider-specific extras (e.g. OpenRouter ``reasoning`` /
    ``reasoning_details``) that some endpoints reject on the next turn.
    """
    data: Dict[str, Any] = {
        "role": "assistant",
        "content": message.content if message.content is not None else None,
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        data["tool_calls"] = [tc.model_dump() for tc in tool_calls]
    return data


_RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError,
              InternalServerError)


def _retry_delay(exc: Exception, attempt: int) -> float:
    """Exponential backoff, capped by any server-provided Retry-After."""
    response = getattr(exc, "response", None)
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw:
            try:
                return min(max(float(raw), 0.0), 60.0)
            except ValueError:
                pass
    return LLM_RETRY_BASE_SECONDS * (2 ** (attempt - 1))


def chat_completion(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[Iterable[Dict[str, Any]]] = None,
    tool_choice: Optional[str] = None,
    **kwargs: Any,
):
    """Call the configured LLM with bounded retries and clear errors.

    Retries transient failures (rate limits, timeouts, connection resets,
    5xx) with exponential backoff. Authentication / validation errors are
    raised immediately.
    """
    validate()
    params: Dict[str, Any] = {
        "model": model or MODEL_NAME,
        "messages": messages,
        "temperature": LLM_TEMPERATURE if temperature is None else temperature,
        "max_tokens": max_tokens or LLM_MAX_TOKENS,
    }
    if tools:
        params["tools"] = list(tools)
        params["tool_choice"] = tool_choice or "auto"
    params.update(kwargs)

    attempts = max(1, LLM_MAX_RETRIES)
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return client.chat.completions.create(**params)
        except _RETRYABLE as exc:  # transient
            last_error = exc
            if attempt >= attempts:
                break
            delay = _retry_delay(exc, attempt)
            print(
                f"[llm] transient {type(exc).__name__} on attempt "
                f"{attempt}/{attempts}; retrying in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
        except APIStatusError as exc:
            # 4xx other than 429 are not worth retrying.
            raise ConfigError(
                f"LLM request failed ({exc.status_code}): {exc.message}"
            ) from exc
    raise ConfigError(
        f"LLM request failed after {attempts} attempt(s): {last_error}"
    ) from last_error


def probe_embeddings() -> bool:
    """Check the embeddings backend used by semantic search."""
    if not EMBEDDINGS_ENABLED:
        print("embeddings: disabled (EMBEDDINGS_PROVIDER=none)")
        return True
    try:
        vectors = embed_texts(["health check"])
        print(f"embeddings: {EMBEDDINGS_PROVIDER}/{EMBED_MODEL} "
              f"dim={len(vectors[0])}")
        return True
    except Exception as exc:  # noqa: BLE001 - informational probe
        print(f"embeddings: UNAVAILABLE ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return False


def health_check() -> bool:
    """Verify credentials + connectivity with a tiny completion."""
    validate()
    try:
        resp = chat_completion(
            [{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=256,
            temperature=0,
        )
        text = (resp.choices[0].message.content or "").strip()
        print(f"provider : {LLM_PROVIDER}")
        print(f"base_url : {BASE_URL}")
        print(f"model    : {MODEL_NAME}")
        print(f"reply    : {text[:80]!r}")
    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        print(f"health check FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    # Semantic search is optional; report it without failing the check.
    probe_embeddings()
    return True


if __name__ == "__main__":
    if "--check" in sys.argv or len(sys.argv) == 1:
        raise SystemExit(0 if health_check() else 1)
    print("usage: python -m llm_config --check", file=sys.stderr)
    raise SystemExit(2)
