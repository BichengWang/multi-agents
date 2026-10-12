import asyncio

import pytest

from agent.workflows import BestOfN, Verdict, map_concurrently


def test_map_bounds_workers_and_preserves_input_order_and_none():
    async def scenario():
        active = peak = 0
        second_finished = asyncio.Event()

        async def worker(index):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                if index == 0:
                    await second_finished.wait()
                else:
                    await asyncio.sleep(0)
                if index == 1:
                    second_finished.set()
                return None if index == 3 else index * 2
            finally:
                active -= 1

        outputs = await map_concurrently(range(8), worker, max_concurrency=2)
        assert outputs == [0, 2, 4, None, 8, 10, 12, 14]
        assert peak == 2 and active == 0

    asyncio.run(scenario())


def test_map_failure_drains_siblings_and_does_not_start_queued_work():
    async def scenario():
        sibling_started = asyncio.Event()
        cleaned = asyncio.Event()
        started = []

        async def worker(index):
            started.append(index)
            if index == 0:
                await sibling_started.wait()
                raise RuntimeError("model unavailable")
            sibling_started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        with pytest.raises(RuntimeError, match="model unavailable"):
            await map_concurrently(range(10), worker, max_concurrency=2)
        assert cleaned.is_set()
        assert started == [0, 1]

    asyncio.run(scenario())


def test_map_caller_cancellation_waits_for_every_worker_cleanup():
    async def scenario():
        both_started = asyncio.Event()
        started = set()
        cleaned = set()

        async def worker(index):
            started.add(index)
            if len(started) == 2:
                both_started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                cleaned.add(index)

        task = asyncio.create_task(map_concurrently(range(10), worker, max_concurrency=2))
        await both_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert started == cleaned == {0, 1}

    asyncio.run(scenario())


def test_map_empty_input_and_invalid_limit():
    async def worker(_):
        raise AssertionError("empty input must not execute work")

    assert asyncio.run(map_concurrently([], worker, max_concurrency=2)) == []
    with pytest.raises(ValueError, match="max_concurrency"):
        asyncio.run(map_concurrently([], worker, max_concurrency=0))


def test_best_of_n_bounds_calls_and_breaks_score_ties_in_candidate_order():
    async def scenario():
        generator, evaluator = object(), object()
        active = peak = 0
        second_generated = asyncio.Event()

        async def runner(agent, text):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                if agent is generator:
                    if text == "q#1":
                        await second_generated.wait()
                    if text == "q#2":
                        second_generated.set()
                    await asyncio.sleep(0)
                    return text
                return Verdict(score=8, passed=True, feedback="")
            finally:
                active -= 1

        result = await BestOfN(
            generator, evaluator, n=6, runner=runner, max_concurrency=2,
            variant_prompt=lambda q, i: f"{q}#{i}",
        ).run("q")
        assert result.final_output == "q#1"
        assert result.meta["chosen"] == 1
        assert [step.iteration for step in result.steps] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6]
        assert peak == 2 and active == 0

    asyncio.run(scenario())


def test_best_of_n_candidate_cancellation_is_not_a_recoverable_failure():
    async def scenario():
        sibling_started = asyncio.Event()
        cleaned = asyncio.Event()
        started = []

        async def runner(agent, text):
            started.append(text)
            if text == "q#1":
                await sibling_started.wait()
                raise asyncio.CancelledError()
            sibling_started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        workflow = BestOfN(
            object(), object(), n=10, runner=runner, max_concurrency=2,
            variant_prompt=lambda q, i: f"{q}#{i}",
        )
        with pytest.raises(asyncio.CancelledError):
            await workflow.run("q")
        assert cleaned.is_set()
        assert started == ["q#1", "q#2"]

    asyncio.run(scenario())
