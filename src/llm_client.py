"""
Thin LLM client wrapper. Targets an LM Studio local server (OpenAI-compatible
API), so swapping the backend later means changing this one module.
"""

from __future__ import annotations

import os

from openai import OpenAI

DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_MODEL = "qwen3.5-9b"


def get_client(base_url: str | None = None) -> OpenAI:
    return OpenAI(
        base_url=base_url or os.environ.get("LMSTUDIO_BASE_URL", DEFAULT_BASE_URL),
        api_key="lm-studio",  # LM Studio ignores the key but the SDK requires one
    )


def get_model_name() -> str:
    return os.environ.get("LMSTUDIO_MODEL", DEFAULT_MODEL)


def generate(client: OpenAI, model: str, system_prompt: str, user_prompt: str,
             temperature: float = 0.3, max_tokens: int = 2000) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""
