from __future__ import annotations

import asyncio
import json
import socket
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx
import pytest


def test_shared_loop_keeps_provider_servers_scripts_and_requests_isolated(_stub_server):
    with _stub_server.provider() as first, _stub_server.provider() as second:
        first.reply = "First test."
        second.reply = "Second test."
        first.embedding_vectors["paper"] = [1.0, 0.0]
        assert first.base_url != second.base_url
        with httpx.Client(trust_env=False) as client:
            for provider in (first, second):
                response = client.post(
                    f"{provider.base_url}/v1/chat/completions",
                    json={"model": "stub-model", "messages": []},
                )
                response.raise_for_status()
                assert response.json()["choices"][0]["message"]["content"] == provider.reply
        assert len(first.requests) == len(second.requests) == 1
        assert first.request_started.is_set() and second.request_started.is_set()
        assert second.embedding_vectors == {}


def test_retired_provider_url_cannot_reach_the_next_test(_stub_server):
    with httpx.Client(trust_env=False) as client:
        with _stub_server.provider() as first:
            old_url = first.base_url
            assert client.get(f"{old_url}/v1/models").is_success
        with _stub_server.provider() as second:
            assert second.requests == []
            assert not second.request_started.is_set()
            with pytest.raises(httpx.ConnectError):
                client.post(
                    f"{old_url}/v1/chat/completions",
                    json={"model": "stub-model", "messages": []},
                )
            assert client.get(f"{second.base_url}/v1/models").is_success
            assert second.requests == []


def test_provider_cleanup_cancels_only_its_own_unfinished_requests(_stub_server):
    with ThreadPoolExecutor(max_workers=1) as executor, httpx.Client(trust_env=False, timeout=5) as client:
        with _stub_server.provider() as other:
            with _stub_server.provider() as blocked:
                blocked.stream_delay_seconds = 60
                request = executor.submit(
                    client.post, f"{blocked.base_url}/v1/chat/completions",
                    json={"model": "stub-model", "messages": [], "stream": True},
                )
                assert blocked.request_started.wait(timeout=3)
            with pytest.raises(httpx.RemoteProtocolError):
                request.result(timeout=3)
            assert blocked.active_inference == 0
            assert client.get(f"{other.base_url}/v1/models").is_success


def test_provider_cleanup_allows_an_inflight_response_to_finish(_stub_server):
    with ThreadPoolExecutor(max_workers=1) as executor, httpx.Client(trust_env=False, timeout=5) as client:
        with _stub_server.provider() as provider:
            provider.stream_delay_seconds = 0.05
            request = executor.submit(
                client.post, f"{provider.base_url}/v1/chat/completions",
                json={"model": "stub-model", "messages": [], "stream": True},
            )
            assert provider.request_started.wait(timeout=3)
        response = request.result(timeout=3)
        response.raise_for_status()
        assert response.text.endswith("data: [DONE]\n\n")
        assert provider.active_inference == 0


def test_provider_cleanup_after_client_resets_an_active_stream(_stub_server, monkeypatch):
    cancelled = threading.Event()

    async def wait_for_disconnect(_payload):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with _stub_server.provider() as provider:
        provider.wait_for_stream = wait_for_disconnect
        url = urlsplit(provider.base_url)
        if sys.platform == "win32":
            # Make the OS reset/shutdown race deterministic without mocking HTTP.
            original_shutdown = socket.socket.shutdown

            def reset_shutdown(sock, how):
                if sock.getsockname()[1] == url.port:
                    raise ConnectionResetError(10054, "Peer reset during transport shutdown")
                return original_shutdown(sock, how)

            monkeypatch.setattr(socket.socket, "shutdown", reset_shutdown)
        payload = json.dumps({"model": "stub-model", "messages": [], "stream": True}).encode()
        with socket.create_connection((url.hostname, url.port), timeout=3) as client:
            client.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
            )
            headers = b""
            while b"\r\n\r\n" not in headers:
                chunk = client.recv(4096)
                assert chunk, "The stream closed before sending response headers."
                headers += chunk
            assert headers.startswith(b"HTTP/1.1 200")
            linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        assert cancelled.wait(timeout=3)
    assert provider.active_inference == 0
    with _stub_server.provider() as next_provider, httpx.Client(trust_env=False) as client:
        assert client.get(f"{next_provider.base_url}/v1/models").is_success
