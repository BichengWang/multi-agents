from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Optional, TypeVar, cast

T = TypeVar("T")
R = TypeVar("R")


async def map_concurrently(
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    max_concurrency: int,
) -> list[R]:
    """Map in input order, bounding active workers and owning their complete lifetime.

    A failed worker or cancelled caller cancels and awaits every sibling before returning.
    No queued item starts after a worker fails.
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be >= 1")

    indexed = iter(enumerate(items))
    results: list[Optional[R]] = [None] * len(items)
    failed = False

    async def consume() -> None:
        nonlocal failed
        try:
            for index, item in indexed:
                if failed:
                    return
                results[index] = await worker(item)
        except BaseException:
            failed = True
            raise

    tasks = [asyncio.create_task(consume()) for _ in range(min(max_concurrency, len(items)))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return cast(list[R], results)
