"""Bounded timing only; callers explicitly decide which operations are safe."""

import asyncio
import logging
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)
_sleep = asyncio.sleep


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    initial_delay: float = 0.25
    max_delay: float = 1.0
    total_delay: float = 1.5
    jitter: float = 0.0

    def __post_init__(self) -> None:
        if self.attempts < 1 or any(
            not math.isfinite(value) or value < 0
            for value in (self.initial_delay, self.max_delay, self.total_delay, self.jitter)
        ):
            raise ValueError("invalid retry policy")


_DEFAULT_POLICY = RetryPolicy()


async def retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    retryable: Callable[[Exception], bool],
    policy: RetryPolicy = _DEFAULT_POLICY,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    random_value: Callable[[], float] = random.random,
    retry_after: Callable[[Exception], float | None] = lambda exc: None,
) -> T:
    delay_total = 0.0
    attempt = 0
    try:
        for attempt in range(1, policy.attempts + 1):
            try:
                return await operation()
            except Exception as exc:
                if attempt == policy.attempts or not retryable(exc):
                    raise
                delay = min(policy.initial_delay * 2 ** (attempt - 1), policy.max_delay)
                jittered = delay + policy.jitter * max(0, min(1, random_value()))
                delay = min(jittered, policy.max_delay)
                hint = retry_after(exc)
                if hint is not None:
                    if not math.isfinite(hint) or hint < 0:
                        raise
                    delay = max(delay, hint)
                if delay > policy.max_delay or delay_total + delay > policy.total_delay:
                    raise
                delay_total += delay
                await (sleep or _sleep)(delay)
    finally:
        # Never format exceptions, provider payloads, URLs or operation arguments.
        logger.info("provider_retry attempts=%d total_delay_ms=%.0f", attempt, delay_total * 1000)
    raise AssertionError("unreachable")
