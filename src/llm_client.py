"""
Thin LLM client wrapper. Targets an LM Studio local server (OpenAI-compatible
API), so swapping the backend later means changing this one module.
"""

from __future__ import annotations

import os

import openai
from openai import OpenAI

DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_MODEL = "qwen3.5-9b"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_TOKENS = 4096


class EmptyResponseError(RuntimeError):
    """Raised when the model exhausted its token budget (finish_reason=length)
    before producing any actual answer — typical of a reasoning model whose
    'thinking' consumed the whole budget with no completion left over."""


def get_client(base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> OpenAI:
    return OpenAI(
        base_url=base_url or os.environ.get("LMSTUDIO_BASE_URL", DEFAULT_BASE_URL),
        api_key="lm-studio",  # LM Studio ignores the key but the SDK requires one
        timeout=timeout,
        max_retries=0,
    )


def get_model_name() -> str:
    return os.environ.get("LMSTUDIO_MODEL", DEFAULT_MODEL)


def generate(client: OpenAI, model: str, system_prompt: str, user_prompt: str,
             temperature: float = 0.3, max_tokens: int = DEFAULT_MAX_TOKENS) -> tuple[str, bool]:
    """Returns (content, truncated) — truncated is True if the model hit
    max_tokens before finishing (finish_reason="length") but still produced
    some content, so the caller can flag it rather than silently returning
    a cut-off note."""
    # Qwen3's documented chat-template directive to skip its <think> pass
    # entirely — more reliable than the API-level enable_thinking flag,
    # which some backends (including LM Studio's) silently ignore.
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt + "\n/no_think"},
    ]
    try:
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
            # Two different spots backends look for this Qwen3 toggle —
            # harmless if a given backend recognizes neither.
            extra_body={
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
    except openai.BadRequestError:
        # Backend rejected the extra field outright; retry without it.
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
        )
    choice = response.choices[0]
    content = choice.message.content or ""

    if not content.strip() and choice.finish_reason == "length":
        reasoning_preview = (getattr(choice.message, "reasoning_content", "") or "")[:200]
        usage = response.usage
        raise EmptyResponseError(
            f"Model hit its token limit (max_tokens={max_tokens}) before producing any answer "
            f"content — it was still 'thinking' (finish_reason=length, "
            f"completion_tokens={usage.completion_tokens if usage else '?'}). "
            f"Reasoning preview: {reasoning_preview!r}. "
            "Increase max_tokens, or disable the model's reasoning/thinking mode in LM Studio."
        )

    truncated = choice.finish_reason == "length"
    return content, truncated
