from __future__ import annotations

import time

import httpx
from helpers import action, download_step, ready
from test_downloads import archive_post, due, refuse

from mangacouch.source_http import SourceError


def test_uncertain_paid_submission_recovers_without_user_action(queue):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    switch = refuse(remote, service, archive_post, httpx.ReadTimeout("lost answer"))
    download_step(service)
    current = service.get(job["id"])
    assert current["state"] == "queued"
    assert current["details"]["phase"] == "recover"
    switch["on"] = False
    for _ in range(8):
        due(service)
        download_step(service)
    assert service.get(job["id"])["state"] == "done"


def test_transient_download_error_stops_after_five_failures(queue, monkeypatch):
    service, _ = queue
    job = ready(service)
    action(service, job, "approve")
    monkeypatch.setattr(service, "_submit", lambda *a: (_ for _ in ()).throw(SourceError("source_network")))
    for _ in range(5):
        due(service)
        assert download_step(service)
    assert service.get(job["id"])["state"] == "error"
    assert not download_step(service)


def test_idle_download_worker_uses_no_write_transaction(queue, monkeypatch):
    service, _ = queue
    monkeypatch.setattr(
        service.library, "transaction", lambda **kw: (_ for _ in ()).throw(AssertionError("idle write"))
    )
    assert not service.step("meta")


def test_recovery_price_increase_requires_new_approval(queue):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    current = service.get(job["id"], private=True)
    current["details"].update(phase="recover", submission_uncertain=True)
    service._update(job["id"], details=current["details"])
    remote.cost = 100
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval"
    assert latest["details"]["may_be_charged"] is True


def test_gp_waiters_share_one_backing_off_balance_probe(queue, monkeypatch):
    from mangacouch import downloads

    service, _ = queue
    clock = [1000.0]
    monkeypatch.setattr(downloads.time, "time", lambda: clock[0])
    waiting = []
    for n in range(10):
        job = service.create(f"https://e-hentai.org/g/{100 + n}/abcdef/")
        service._update(
            job["id"],
            state="queued",
            error="insufficient_gp",
            retry_at=downloads.GP_PARKED,
            details={"phase": "submit", "approved_gp": 25, "mode": "org"},
        )
        waiting.append(job)
    calls, balance = [], [0]

    def quote(*args, **kwargs):
        calls.append(clock[0])
        return {"original_gp": 25, "resample_gp": 0, "balance_gp": balance[0]}, None

    monkeypatch.setattr(service.sources, "quote", quote)
    for now in range(1000, 1601, 10):
        clock[0] = now
        service._gp_probe()
    assert len(calls) <= 5
    assert service.summary()["waiting"] == 0
    balance[0] = 250
    assert service._gp_probe(force=True)["released"] == 10
    assert service.summary()["waiting"] == 10
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_account").fetchone()[0] == 1


def test_post_cancelled_before_admission_never_becomes_uncertain(queue, monkeypatch):
    from source_fixtures import archiver_posts

    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    submit = service.sources.submit

    def pause(*args, **kwargs):
        action(service, job, "pause")
        return submit(*args, **kwargs)

    monkeypatch.setattr(service.sources, "submit", pause)
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "paused"
    assert not latest["details"]["submission_uncertain"]
    assert not archiver_posts(remote)


def test_bulk_approval_preserves_each_frozen_revision(queue, files, auth, monkeypatch):
    from fastapi.testclient import TestClient

    from mangacouch.downloads import Downloads
    from mangacouch.http import create_app

    service, _ = queue
    first = ready(service)
    second = ready(service, "https://e-hentai.org/g/101/abcdef/")
    frozen = [
        {"id": job["id"], "expected_revision": job["revision"], "mode": "org"} for job in (first, second)
    ]
    service._update(
        second["id"], details={**second["details"], "quote": {"original_gp": 100, "resample_gp": 0}}
    )
    monkeypatch.setattr(Downloads, "start", lambda self: None)
    _, key = auth.create_key("management", "bulk-test")
    with TestClient(create_app(files, auth=auth), base_url="http://127.0.0.1:8780") as client:
        result = client.post(
            "/api/downloads/actions",
            json={"action": "approve", "items": frozen},
            headers={"Authorization": f"Bearer {key}"},
        ).json()["results"]
    assert [item["ok"] for item in result] == [True, False]
    assert result[1]["error"] == "revision_conflict"
    assert service.get(second["id"])["state"] == "needs_approval"


def test_eh_exh_refresh_keeps_one_gallery_observation(queue):
    from helpers import finish_download

    from mangacouch.models import Source

    service, _ = queue
    job = ready(service, "https://exhentai.org/g/100/abcdef/")
    finished = finish_download(service, job)
    book = service.library.get_book(finished["book_id"])
    source = Source(site="ehentai", gallery_id=100, gallery_token="abcdef")
    observation = service.sources.fetch(source)
    service.online.apply(book.id, observation, book.metadata_revision)
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_note WHERE book_id=?", (book.id,)).fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) FROM source_observation WHERE book_id=?", (book.id,)).fetchone()[0]
            == 1
        )
    assert service.online.notes(book.id)["notes"][0]["source"]["site"] == "exhentai"


def test_range_416_validates_and_imports_existing_complete_archive(queue):
    from test_downloads import transferring

    service, remote = queue
    job = transferring(service)
    path = service._path(job["id"])
    path.write_bytes(remote.payload)
    current = service.get(job["id"], private=True)
    current["details"]["etag"] = '"fixture-v1"'
    service._update(job["id"], details=current["details"], total=None)
    refuse(remote, service, lambda request: request.url.host == "archive.example", httpx.Response(416))
    download_step(service)
    assert service.get(job["id"])["details"]["phase"] == "import"
    download_step(service)
    assert service.get(job["id"])["state"] == "done"


def test_every_download_phase_belongs_to_exactly_one_worker_pool(queue):
    import ast
    from pathlib import Path

    from mangacouch import downloads

    tree = ast.parse(Path(downloads.__file__).read_text())
    phases = {"inspect", "submit", "recover", "transfer", "import", "done", "uncertain"}
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "phase" and isinstance(node.value, ast.Constant):
            phases.add(str(node.value.value))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "phase"
                ):
                    phases.add(str(node.value.value))
    service, _ = queue
    job = service.create("https://e-hentai.org/g/100/abcdef/")
    with service.library.transaction() as db:
        for phase in phases:
            db.execute(
                "UPDATE download_job SET details_json=json_set(details_json,'$.phase',?) WHERE id=?",
                (phase, job["id"]),
            )
            membership = db.execute(
                f"SELECT phase IN {downloads.META_PHASES_SQL},phase NOT IN {downloads.META_PHASES_SQL}"
                " FROM download_job WHERE id=?",
                (job["id"],),
            ).fetchone()
            assert sum(membership) == 1, phase
            assert bool(membership[0]) == (phase in downloads.META_PHASES)


def test_jobs_retrying_later_are_not_counted_as_waiting(queue):
    service, _ = queue
    soon, later = (service.create(f"https://e-hentai.org/g/{n}/abcdef/") for n in (301, 302))
    now = int(time.time())
    with service.library.transaction() as db:
        db.execute("UPDATE download_job SET state='queued',retry_at=? WHERE id=?", (now + 15, soon["id"]))
        db.execute("UPDATE download_job SET state='queued',retry_at=? WHERE id=?", (now + 1800, later["id"]))
    summary = service.summary()
    assert (summary["waiting"], summary["running"], summary["total"]) == (1, 0, 2)
