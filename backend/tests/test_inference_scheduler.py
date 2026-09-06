from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.providers.inference import InferenceScheduler, inference_priority
from backend.providers.logging import ScheduledTransport


@pytest.mark.anyio
@pytest.mark.parametrize("configure_in_thread", [False, True])
async def test_same_model_waits_for_active_stream(
    configure_in_thread,
) -> None:
    scheduler = InferenceScheduler()
    if configure_in_thread:
        await asyncio.to_thread(scheduler.configure, "local", serialize_model_switches=True)
    else:
        scheduler.configure("local", serialize_model_switches=True)
    first = await scheduler.request(profile_id="local", model="main").__aenter__()
    second = asyncio.create_task(
        scheduler.request(profile_id="local", model="main").__aenter__(),
    )
    switch = asyncio.create_task(
        scheduler.request(profile_id="local", model="small").__aenter__(),
    )
    await asyncio.sleep(0)
    assert not second.done()
    assert not switch.done()
    await first.release()
    second_lease = await asyncio.wait_for(second, 1)
    await asyncio.sleep(0)
    assert not switch.done()
    await second_lease.release()
    switched = await asyncio.wait_for(switch, 1)
    assert scheduler.snapshot()["providers"]["local"]["active_models"] == {"small": 1}
    await switched.release()
    assert scheduler.snapshot()["active_requests"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("configure", [False, True])
async def test_unconfigured_or_disabled_provider_still_serializes(configure) -> None:
    scheduler = InferenceScheduler()
    if configure:
        scheduler.configure("local", serialize_model_switches=False)
    async with scheduler.request(profile_id="local", model="main"):
        next_request = asyncio.create_task(
            scheduler.request(profile_id="local", model="small").__aenter__(),
        )
        await asyncio.sleep(0)
        assert not next_request.done()
        assert scheduler.snapshot()["active_requests"] == 1
    await (await asyncio.wait_for(next_request, 1)).release()


@pytest.mark.anyio
@pytest.mark.parametrize("server_url", ["http://local/v1", "https://remote/v1"])
async def test_all_providers_share_one_lane(server_url) -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("first", serialize_model_switches=True)
    scheduler.configure("second", serialize_model_switches=True)
    async with scheduler.request(profile_id="first", model="main", server_url="http://local/v1"):
        blocked = asyncio.create_task(
            scheduler.request(profile_id="first", model="small").__aenter__(),
        )
        await asyncio.sleep(0)
        other_provider = asyncio.create_task(
            scheduler.request(
                profile_id="second", model="other", server_url=server_url,
            ).__aenter__(),
        )
        await asyncio.sleep(0)
        assert not blocked.done()
        assert not other_provider.done()
        assert scheduler.snapshot()["active_requests"] == 1
    await (await asyncio.wait_for(blocked, 1)).release()
    await (await asyncio.wait_for(other_provider, 1)).release()


@pytest.mark.anyio
async def test_different_model_closes_same_model_admission_batch() -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)
    first = await scheduler.request(profile_id="local", model="main").__aenter__()
    order = []

    async def run(model):
        async with scheduler.request(profile_id="local", model=model):
            order.append(model)

    switch = asyncio.create_task(run("small"))
    await asyncio.sleep(0)
    followers = [asyncio.create_task(run("main")) for _ in range(20)]
    await asyncio.sleep(0)
    assert order == []
    await first.release()
    await asyncio.wait_for(asyncio.gather(switch, *followers), 1)
    assert order[0] == "small"
    assert len(order) == 21


@pytest.mark.anyio
async def test_priority_is_global_fifo_with_bounded_background_fairness() -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)
    order = []
    lease = await scheduler.request(
        profile_id="local", model="initial", priority="background",
    ).__aenter__()

    async def run(name, priority):
        with inference_priority(priority):
            async with scheduler.request(profile_id=name, model=name):
                order.append(name)

    tasks = [asyncio.create_task(run("background", "background"))]
    tasks += [asyncio.create_task(run(f"chat-{n}", "interactive")) for n in range(5)]
    await asyncio.sleep(0)
    await lease.release()
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert order == ["chat-0", "chat-1", "chat-2", "background", "chat-3", "chat-4"]


@pytest.mark.anyio
async def test_cancelling_queued_request_keeps_lane_occupied_until_release() -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)
    async with scheduler.request(profile_id="local", model="main"):
        switch = asyncio.create_task(
            scheduler.request(profile_id="local", model="small").__aenter__(),
        )
        await asyncio.sleep(0)
        same = asyncio.create_task(
            scheduler.request(profile_id="local", model="main").__aenter__(),
        )
        await asyncio.sleep(0)
        assert not same.done()
        switch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await switch
        assert not same.done()
        assert len(scheduler.snapshot()["queue"]) == 1
    await (await asyncio.wait_for(same, 1)).release()
    assert scheduler.snapshot()["queue"] == []


@pytest.mark.anyio
async def test_cancelling_active_request_releases_next_model_and_release_is_idempotent() -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)
    started = asyncio.Event()

    async def run():
        async with scheduler.request(profile_id="local", model="main"):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await started.wait()
    next_model = asyncio.create_task(
        scheduler.request(profile_id="local", model="small").__aenter__(),
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lease = await asyncio.wait_for(next_model, 1)
    await asyncio.gather(lease.release(), lease.release())
    assert scheduler.snapshot()["active_requests"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("configure_in_thread", [False, True])
async def test_disabling_legacy_switch_policy_does_not_bypass_global_lane(
    configure_in_thread,
) -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)
    async with scheduler.request(profile_id="local", model="main"):
        switch = asyncio.create_task(
            scheduler.request(profile_id="local", model="small").__aenter__(),
        )
        await asyncio.sleep(0)
        assert not switch.done()
        if configure_in_thread:
            loop = asyncio.get_running_loop()
            debug = loop.get_debug()
            loop.set_debug(True)
            try:
                await asyncio.wait_for(asyncio.to_thread(
                    scheduler.configure, "local", serialize_model_switches=False,
                ), 1)
            finally:
                loop.set_debug(debug)
        else:
            scheduler.configure("local", serialize_model_switches=False)
        await asyncio.sleep(0)
        assert not switch.done()
        assert scheduler.snapshot()["active_requests"] == 1
    await (await asyncio.wait_for(switch, 1)).release()


@pytest.mark.anyio
@pytest.mark.parametrize("configure_in_thread", [False, True])
async def test_enabling_legacy_switch_policy_preserves_global_queue(configure_in_thread) -> None:
    scheduler = InferenceScheduler()
    first = await scheduler.request(profile_id="local", model="main").__aenter__()
    second = asyncio.create_task(
        scheduler.request(profile_id="local", model="small").__aenter__(),
    )
    if configure_in_thread:
        await asyncio.to_thread(scheduler.configure, "local", serialize_model_switches=True)
    else:
        scheduler.configure("local", serialize_model_switches=True)
    switch = asyncio.create_task(
        scheduler.request(profile_id="local", model="other").__aenter__(),
    )
    await asyncio.sleep(0)
    assert not switch.done()
    await first.release()
    second_lease = await asyncio.wait_for(second, 1)
    await asyncio.sleep(0)
    assert not switch.done()
    await second_lease.release()
    await (await asyncio.wait_for(switch, 1)).release()


@pytest.mark.anyio
async def test_scheduled_transport_holds_model_until_stream_is_consumed() -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)
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

    transport = ScheduledTransport(httpx.MockTransport(provider), scheduler, "local")
    async with httpx.AsyncClient(transport=transport) as client:
        async with client.stream(
            "POST", "https://provider.test/v1/chat/completions", json={"model": "main"},
        ) as response:
            chunks = response.aiter_bytes()
            assert await anext(chunks) == b"first"
            second = asyncio.create_task(client.post(
                "https://provider.test/v1/chat/completions",
                headers={"x-request": "second"}, json={"model": "small"},
            ))
            await asyncio.sleep(0)
            assert not second_started.is_set()
            release_stream.set()
            assert await anext(chunks) == b"second"
            with pytest.raises(StopAsyncIteration):
                await anext(chunks)
        assert (await asyncio.wait_for(second, 1)).status_code == 200
    assert scheduler.snapshot()["active_requests"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_stream_is_closed_before_failed_request_releases_model(failure) -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)
    closed = False

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"chunk"
            raise failure

        async def aclose(self):
            nonlocal closed
            assert scheduler.snapshot()["active_requests"] == 1
            closed = True

    transport = ScheduledTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())),
        scheduler, "local",
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(failure):
            await client.post("http://local/v1/chat/completions", json={"model": "main"})
    assert closed
    assert scheduler.snapshot()["active_requests"] == 0
    await (await asyncio.wait_for(
        scheduler.request(profile_id="local", model="small").__aenter__(), 1,
    )).release()


@pytest.mark.anyio
async def test_transport_error_before_response_releases_model() -> None:
    scheduler = InferenceScheduler()
    scheduler.configure("local", serialize_model_switches=True)

    async def provider(request):
        raise httpx.ConnectError("unavailable", request=request)

    transport = ScheduledTransport(httpx.MockTransport(provider), scheduler, "local")
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post("http://local/v1/chat/completions", json={"model": "main"})
    assert scheduler.snapshot()["active_requests"] == 0
    await (await asyncio.wait_for(
        scheduler.request(profile_id="local", model="small").__aenter__(), 1,
    )).release()
