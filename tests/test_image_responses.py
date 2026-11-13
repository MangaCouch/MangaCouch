from __future__ import annotations

import asyncio

import anyio
import pytest
from starlette.requests import ClientDisconnect, Request

from mangacouch import services
from mangacouch.errors import LibraryBusy


def request(method="GET", headers=()):
    async def receive():
        await anyio.sleep_forever()
        return {"type": "http.disconnect"}

    return Request(
        {"type": "http", "method": method, "path": "/", "headers": headers, "asgi": {"spec_version": "2.4"}},
        receive,
    )


@pytest.mark.parametrize("cached", [False, True])
def test_slow_image_sends_do_not_hold_generation_slots(cached):
    async def run():
        limiter = anyio.CapacityLimiter(2)
        release = asyncio.Event()
        reads = []
        chunks = []

        def read():
            reads.append(1)
            return b"x" * (256 * 1024)

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(len(message["body"]))
                await release.wait()

        async def serve():
            incoming = request()
            response = await services.image_response(
                incoming,
                read,
                media_type="image/png",
                limiter=limiter,
                cache_control="no-store",
                cached=read if cached else None,
            )
            await response(incoming.scope, incoming.receive, send)

        tasks = [asyncio.create_task(serve()) for _ in range(3)]
        try:
            with anyio.fail_after(3):
                while len(chunks) < 3:
                    await anyio.sleep(0.001)
            assert reads == [1, 1, 1]
            assert limiter.borrowed_tokens == 0
            release.set()
            await asyncio.gather(*tasks)
            assert reads == [1, 1, 1]
            assert max(chunks) <= 64 * 1024
            assert sum(chunks) == 3 * 256 * 1024
            assert limiter.borrowed_tokens == 0
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    anyio.run(run)


@pytest.mark.parametrize("failure", ["render", "send", "cancel"])
def test_image_slots_are_released_on_errors_and_cancellation(failure):
    async def run():
        limiter = anyio.CapacityLimiter(1)
        blocked = asyncio.Event()
        incoming = request()

        def read():
            if failure == "render":
                raise ValueError("decode failed")
            return b"page"

        async def send(message):
            if message["type"] == "http.response.body":
                if failure == "send":
                    raise OSError("disconnected")
                blocked.set()
                await anyio.sleep_forever()

        response = await services.image_response(
            incoming,
            read,
            media_type="image/png",
            limiter=limiter,
            cache_control="no-store",
        )
        task = asyncio.create_task(response(incoming.scope, incoming.receive, send))
        try:
            if failure == "cancel":
                with anyio.fail_after(3):
                    await blocked.wait()
                task.cancel()
            error = {"render": ValueError, "send": ClientDisconnect, "cancel": asyncio.CancelledError}[
                failure
            ]
            with pytest.raises(error):
                await task
            assert limiter.borrowed_tokens == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    anyio.run(run)


def test_image_slot_timeout_never_reads_or_leaks_a_slot(monkeypatch):
    monkeypatch.setattr(services, "SLOT_TIMEOUT", 0.01)

    async def run():
        limiter = anyio.CapacityLimiter(1)
        owner = object()
        await limiter.acquire_on_behalf_of(owner)
        incoming = request()
        response = await services.image_response(
            incoming,
            lambda: pytest.fail("timed-out request read an image"),
            media_type="image/png",
            limiter=limiter,
            cache_control="no-store",
        )

        async def send(message):
            pytest.fail("response began before admission")

        try:
            with pytest.raises(LibraryBusy):
                await response(incoming.scope, incoming.receive, send)
            assert limiter.borrowed_tokens == 1
        finally:
            limiter.release_on_behalf_of(owner)

    anyio.run(run)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_cached_image_bypasses_a_full_generation_limiter(method):
    async def run():
        limiter = anyio.CapacityLimiter(1)
        owner = object()
        await limiter.acquire_on_behalf_of(owner)
        incoming = request(method)
        sent = []

        async def send(message):
            assert limiter.borrowed_tokens == 1
            sent.append(message)

        response = await services.image_response(
            incoming,
            lambda: pytest.fail("cache hit generated an image"),
            cached=lambda: b"cached",
            media_type="image/webp",
            limiter=limiter,
            cache_control="no-store",
        )
        try:
            with anyio.fail_after(1):
                await response(incoming.scope, incoming.receive, send)
            assert sent[0]["status"] == 200
            assert (b"content-length", b"6") in sent[0]["headers"]
            body = b"".join(message.get("body", b"") for message in sent)
            assert body == (b"cached" if method == "GET" else b"")
        finally:
            limiter.release_on_behalf_of(owner)

    anyio.run(run)


def test_cache_misses_still_bound_concurrent_generation():
    import threading

    async def run():
        limiter = anyio.CapacityLimiter(2)
        release = threading.Event()
        reads = []

        def render():
            reads.append(1)
            assert release.wait(3)
            return b"page"

        async def serve():
            incoming = request()
            response = await services.image_response(
                incoming,
                render,
                cached=lambda: None,
                media_type="image/png",
                limiter=limiter,
                cache_control="no-store",
            )

            async def send(message):
                pass

            await response(incoming.scope, incoming.receive, send)

        tasks = [asyncio.create_task(serve()) for _ in range(3)]
        try:
            with anyio.fail_after(2):
                while len(reads) != 2 or limiter.statistics().tasks_waiting != 1:
                    await anyio.sleep(0.001)
            assert limiter.borrowed_tokens == 2
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert len(reads) == 3 and limiter.borrowed_tokens == 0

    anyio.run(run)
