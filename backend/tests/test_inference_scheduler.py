from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.providers.logging import ScheduledTransport
from backend.providers.inference import InferenceScheduler


@pytest.mark.anyio
async def test_exclusive_summary_is_contiguous_and_prioritized() -> None:
    scheduler = InferenceScheduler()
    first_active = asyncio.Event()
    release_first = asyncio.Event()
    summary_waiting = asyncio.Event()
    release_summary = asyncio.Event()
    order: list[str] = []

    async def first_request() -> None:
        async with scheduler.request():
            order.append("first-start")
            first_active.set()
            await release_first.wait()
            order.append("first-end")

    async def summary() -> None:
        await first_active.wait()
        summary_waiting.set()
        async with scheduler.exclusive():
            order.append("summary-start")
            async with scheduler.request():
                order.append("summary-call-1")
            await asyncio.sleep(0)
            async with scheduler.request():
                order.append("summary-call-2")
            await release_summary.wait()
            order.append("summary-end")

    async def later_request() -> None:
        await summary_waiting.wait()
        async with scheduler.request():
            order.append("later")

    tasks = [
        asyncio.create_task(first_request()),
        asyncio.create_task(summary()),
        asyncio.create_task(later_request()),
    ]
    await first_active.wait()
    await summary_waiting.wait()
    await asyncio.sleep(0)
    release_first.set()
    while "summary-start" not in order:
        await asyncio.sleep(0)
    assert "later" not in order
    release_summary.set()
    await asyncio.gather(*tasks)

    assert order == [
        "first-start",
        "first-end",
        "summary-start",
        "summary-call-1",
        "summary-call-2",
        "summary-end",
        "later",
    ]


@pytest.mark.anyio
async def test_scheduled_transport_holds_lane_until_stream_is_consumed() -> None:
    scheduler = InferenceScheduler()
    release_stream = asyncio.Event()
    second_started = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            await release_stream.wait()
            yield b"second"

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-request") == "second":
            second_started.set()
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, stream=SlowStream())

    transport = ScheduledTransport(httpx.MockTransport(provider), scheduler)
    async with httpx.AsyncClient(transport=transport) as client:
        async with client.stream(
            "POST",
            "https://provider.test/v1/chat/completions",
            headers={"x-request": "first"},
        ) as response:
            chunks = response.aiter_bytes()
            assert await anext(chunks) == b"first"
            second = asyncio.create_task(
                client.post(
                    "https://provider.test/v1/chat/completions",
                    headers={"x-request": "second"},
                )
            )
            await asyncio.sleep(0.01)
            assert not second_started.is_set()
            release_stream.set()
            assert await anext(chunks) == b"second"
            with pytest.raises(StopAsyncIteration):
                await anext(chunks)
        assert (await second).status_code == 200


@pytest.mark.anyio
async def test_exclusive_owner_still_runs_only_one_inference_call() -> None:
    scheduler = InferenceScheduler()
    active = 0
    maximum_active = 0

    async def inference_call() -> None:
        nonlocal active, maximum_active
        async with scheduler.request():
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    async with scheduler.exclusive():
        await asyncio.gather(inference_call(), inference_call())

    assert maximum_active == 1


@pytest.mark.anyio
async def test_opt_in_parallel_group_runs_up_to_its_limit_inside_exclusive_lane() -> None:
    scheduler = InferenceScheduler()
    group = object()
    active = 0
    maximum_active = 0

    async def inference_call() -> None:
        nonlocal active, maximum_active
        async with scheduler.parallel(group, 4):
            async with scheduler.request():
                active += 1
                maximum_active = max(maximum_active, active)
                await asyncio.sleep(0.01)
                active -= 1

    async with scheduler.exclusive():
        await asyncio.gather(*(inference_call() for _ in range(9)))

    assert maximum_active == 4
