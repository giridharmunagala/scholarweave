from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.providers.logging import ScheduledTransport
from backend.providers.inference import InferenceScheduler
from backend.core.errors import ConflictError


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


@pytest.mark.anyio
async def test_priority_is_fifo_with_bounded_background_fairness() -> None:
    scheduler = InferenceScheduler()
    order = []
    lease = scheduler.request(priority="background")
    await lease.__aenter__()

    async def run(name, priority):
        async with scheduler.request(priority=priority):
            order.append(name)

    tasks = [asyncio.create_task(run("background", "background"))]
    tasks += [asyncio.create_task(run(f"chat-{n}", "interactive")) for n in range(5)]
    await asyncio.sleep(0)
    await lease.release()
    await asyncio.gather(*tasks)
    assert order == ["chat-0", "chat-1", "chat-2", "background", "chat-3", "chat-4"]


@pytest.mark.anyio
async def test_residency_waits_for_explicit_drained_confirmation_and_cancellation() -> None:
    scheduler = InferenceScheduler()
    scheduler.restore("local", "main")
    assert not scheduler.confirmed
    await scheduler.confirm("local", "main", "interactive")
    lease = scheduler.request(profile_id="local", model="main")
    await lease.__aenter__()
    order = []

    async def run(model):
        async with scheduler.request(profile_id="local", model=model):
            order.append(model)

    wrong = asyncio.create_task(run("small"))
    cancelled = asyncio.create_task(run("other"))
    await asyncio.sleep(0)
    assert scheduler.snapshot()["queue"][0]["blocked_by_residency"]
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert len(scheduler.snapshot()["queue"]) == 1
    with pytest.raises(ConflictError):
        await scheduler.confirm("local", "small", "batch")
    drain = asyncio.create_task(scheduler.begin_switch("local"))
    await asyncio.sleep(0)
    assert not drain.done()
    assert scheduler.paused
    await lease.release()
    await drain
    assert not wrong.done()
    await scheduler.confirm("local", "small", "batch")
    await asyncio.wait_for(wrong, 1)
    assert order == ["small"]


@pytest.mark.anyio
async def test_wrong_model_does_not_block_resident_or_reach_transport() -> None:
    scheduler = InferenceScheduler()
    scheduler.restore("local", None)
    await scheduler.confirm("local", "main", "interactive")
    calls = []

    async def provider(request):
        calls.append(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = ScheduledTransport(httpx.MockTransport(provider), scheduler, "local")
    async with httpx.AsyncClient(transport=transport, base_url="http://local/v1") as client:
        wrong = asyncio.create_task(client.post("/chat/completions", json={"model": "small"}))
        await asyncio.sleep(0)
        await asyncio.wait_for(client.post("/chat/completions", json={"model": "main"}), 1)
        assert len(calls) == 1
        wrong.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrong
    assert scheduler.snapshot()["queue"] == []


@pytest.mark.anyio
async def test_cancelled_drain_stays_paused_and_single_gpu_ignores_parallel_hint() -> None:
    scheduler = InferenceScheduler()
    scheduler.restore("local", None)
    await scheduler.confirm("local", "main", "batch")
    lease = scheduler.request(profile_id="local", model="main")
    await lease.__aenter__()
    drain = asyncio.create_task(scheduler.begin_switch("local"))
    await asyncio.sleep(0)
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    await lease.release()
    assert scheduler.paused and not scheduler.confirmed
    await scheduler.confirm("local", "main", "batch")
    maximum = active = 0

    async def run():
        nonlocal active, maximum
        async with scheduler.parallel("batch", 4):
            async with scheduler.request(profile_id="local", model="main"):
                active += 1
                maximum = max(active, maximum)
                await asyncio.sleep(0)
                active -= 1

    await asyncio.gather(*(run() for _ in range(4)))
    assert maximum == 1


@pytest.mark.anyio
async def test_stream_is_closed_before_cancelled_request_releases_lane() -> None:
    scheduler = InferenceScheduler()
    closed = False

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"chunk"
            raise asyncio.CancelledError

        async def aclose(self):
            nonlocal closed
            assert scheduler.snapshot()["active_requests"] == 1
            closed = True

    transport = ScheduledTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())), scheduler,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.post("http://local/v1/chat/completions", json={"model": "main"})
    assert closed
    assert scheduler.snapshot()["active_requests"] == 0
