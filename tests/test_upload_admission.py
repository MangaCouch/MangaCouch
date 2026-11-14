"""Upload queue deadlines leave active transfers alone and release resources on exit."""

from __future__ import annotations

import asyncio

import anyio
import httpx
import pytest
from helpers import make_zip

from mangacouch import api_scans


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("outcome", ["timeout", "released", "cancelled"])
def test_upload_admission_deadline_and_recovery(client, auth, files, tmp_path, monkeypatch, paired, outcome):
    monkeypatch.setattr(api_scans, "SLOT_TIMEOUT", 0.15)
    payload = make_zip(tmp_path / "upload.cbz").read_bytes()
    packed = httpx.Request("POST", "http://test", files={"archive": ("upload.cbz", payload)})
    body = packed.read() if paired else payload
    headers = {"Content-Type": packed.headers["content-type"]} if paired else {}
    _, key = auth.create_key("management", "upload-admission-test")

    def directories():
        return list(files.library.path.parent.glob("mangacouch-upload-*"))

    async def run():
        started = 0
        both_started, release = asyncio.Event(), asyncio.Event()
        consumed = []
        waiting_scope = None
        response = None

        async def slow():
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            yield body[:100]
            await release.wait()
            yield body[100:]

        async def complete():
            consumed.append(True)
            yield body

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + key},
        ) as connection:
            active = [
                asyncio.create_task(
                    connection.post("/api/import?filename=slow.cbz", content=slow(), headers=headers)
                )
                for _ in range(2)
            ]

            async def waiting():
                nonlocal waiting_scope, response
                with anyio.CancelScope() as scope:
                    waiting_scope = scope
                    response = await connection.post(
                        "/api/import?filename=normal.cbz", content=complete(), headers=headers
                    )

            pending = None
            try:
                await asyncio.wait_for(both_started.wait(), 2)
                pending = asyncio.create_task(waiting())
                with anyio.fail_after(2):
                    while len(directories()) != 3:
                        await asyncio.sleep(0.001)
                if outcome == "released":
                    release.set()
                elif outcome == "cancelled":
                    assert waiting_scope is not None
                    waiting_scope.cancel()
                await asyncio.wait_for(pending, 2)
                if outcome == "timeout":
                    assert response is not None and response.status_code == 503
                    assert response.json()["error"] == "library_busy"
                    assert response.headers["retry-after"] == "5"
                elif outcome == "cancelled":
                    assert response is None
                else:
                    assert response is not None and response.status_code == 200
                if outcome != "released":
                    assert not consumed and len(directories()) == 2
                    assert all(not task.done() for task in active)
                    assert (await connection.get("/api/auth/me")).status_code == 200
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*active), 5)
                assert all(result.status_code == 200 for result in results)
                retry = await connection.post("/api/import?filename=retry.cbz", content=payload)
                assert retry.status_code == 200
                assert not directories()
            finally:
                release.set()
                for task in [*active, *([pending] if pending else [])]:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*active, *([pending] if pending else []), return_exceptions=True)

    asyncio.run(run())


pytestmark = pytest.mark.usefixtures("temporary_cleanup")
