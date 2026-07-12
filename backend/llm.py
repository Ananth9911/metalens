"""
llm.py — thin wrapper around the LLM provider (Groq).

The API key is read from the GROQ_API_KEY environment variable. It is NEVER
hardcoded and NEVER committed. See .env.example and the README for setup.

Groq is OpenAI-API-compatible, so this same code works against OpenAI or any
compatible endpoint by changing base_url + key.
"""

import os

from groq import Groq

# Default chat model. `llama-3.3-70b-versatile` was deprecated by Groq in 2026;
# gpt-oss-120b is the current free-tier general model. Override with LLM_MODEL.
DEFAULT_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")


def _client() -> Groq:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and add your "
            "key, or export GROQ_API_KEY in your shell. Get a free key at "
            "https://console.groq.com/keys"
        )
    return Groq(api_key=key)


def chat(messages: list[dict], model: str | None = None,
         temperature: float = 0.2) -> str:
    """Standard chat completion -> assistant text."""
    resp = _client().chat.completions.create(
        model=model or DEFAULT_MODEL,
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content or ""


def chat_json(messages: list[dict], model: str | None = None) -> str:
    """
    Chat completion tuned for structured output: low temperature and JSON
    response format when the provider supports it. Falls back gracefully.
    """
    try:
        resp = _client().chat.completions.create(
            model=model or DEFAULT_MODEL,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""
    except Exception:
        # provider/model may not support response_format — retry plain
        return chat(messages, model=model, temperature=0.0)


def is_configured() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))
