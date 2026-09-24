"""
Shared retry logic for LM Studio calls used by both notes.py and quiz.py.

A single request failing with a token-limit exhaustion or a timeout doesn't
mean the subchapter/quiz is unanswerable — it usually means the budget was
too tight or the model had a slow moment. Automatically retry the same
request a few times, escalating the token budget on a token-limit failure,
before giving up.
"""

from __future__ import annotations

from typing import Callable, TypeVar

import openai

from src.llm_client import EmptyResponseError

MAX_ATTEMPTS = 3
TOKEN_ESCALATION_FACTOR = 1.75
MAX_ESCALATED_TOKENS = 24576

T = TypeVar("T")


def call_with_retry(fn: Callable[[int], T], initial_max_tokens: int,
                     on_retry: Callable[[int, str, int], None] | None = None) -> T:
    """fn(max_tokens) -> result. Retries up to MAX_ATTEMPTS times total: on
    EmptyResponseError the token budget is escalated each retry; on
    APITimeoutError it's retried with the same budget (assumed transient
    slowness, not a size problem). Re-raises the last error if every
    attempt fails. on_retry(attempt, reason, next_max_tokens) is called
    before each retry, reason being "token_limit" or "timeout"."""
    max_tokens = initial_max_tokens
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn(max_tokens)
        except EmptyResponseError as e:
            last_error = e
            if attempt < MAX_ATTEMPTS:
                max_tokens = min(int(max_tokens * TOKEN_ESCALATION_FACTOR), MAX_ESCALATED_TOKENS)
                if on_retry:
                    on_retry(attempt, "token_limit", max_tokens)
        except openai.APITimeoutError as e:
            last_error = e
            if attempt < MAX_ATTEMPTS:
                if on_retry:
                    on_retry(attempt, "timeout", max_tokens)

    raise last_error
