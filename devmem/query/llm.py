"""OpenRouter LLM client."""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODEL = "anthropic/claude-sonnet-4"
_TIMEOUT_SEC = 60

# Loaded lazily from config / env
_api_key: Optional[str] = None
_model: Optional[str] = None


class DevMemLLMError(Exception):
    """Raised when the LLM call fails."""


def configure(api_key: Optional[str] = None, model: Optional[str] = None) -> None:
    """Configure the LLM client.  Call once at startup."""
    global _api_key, _model
    _api_key = api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("DEVMEM_LLM_KEY")
    _model = model or _DEFAULT_MODEL


def is_available() -> bool:
    """Return True if an API key is configured."""
    key = _api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("DEVMEM_LLM_KEY")
    return bool(key)


def ask(messages: list[dict], model: Optional[str] = None) -> str:
    """
    Send *messages* to OpenRouter and return the response text.

    Raises DevMemLLMError on any failure so callers can degrade gracefully.
    """
    key = _api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("DEVMEM_LLM_KEY")
    if not key:
        raise DevMemLLMError(
            "No LLM API key found.  Set OPENROUTER_API_KEY or run: devmem config llm_key <key>"
        )

    effective_model = model or _model or _DEFAULT_MODEL

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/devmem",
        "X-Title": "DevMem",
    }
    payload = {
        "model": effective_model,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.3,
    }

    try:
        resp = requests.post(
            _OPENROUTER_URL,
            json=payload,
            headers=headers,
            timeout=_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        raise DevMemLLMError(f"HTTP request failed: {exc}") from exc

    if resp.status_code != 200:
        raise DevMemLLMError(
            f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:300]}"
        )

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise DevMemLLMError(f"Unexpected response shape: {exc}") from exc
