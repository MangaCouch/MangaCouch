"""Real loopback HTTP regressions for cancellation, resume and uncertain payments.
All metadata/payment responses are synthetic. No Internet requests are made.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import random
import socket
import ssl
import threading
import time
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from helpers import action, download_step, drive, ready
from source_fixtures import SourceFixture, archiver_posts

from mangacouch.downloads import Downloads
from mangacouch.errors import InvalidInput
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import SourceHTTP


def due(service):
    with service.library.transaction() as db:
        db.execute("UPDATE download_job SET retry_at=0")


def prepare(service):
    job = ready(service)
    action(service, job, "approve")
    assert download_step(service)
    return job


@dataclass
class State:
    payload: bytes
    mode: str = "normal"
    cut: int = 393216
    etag: str | None = '"network-fixture-v1"'
    ignore_range: bool = False
    wrong_etag: bool = False
    close_delimited: bool = False
    paid_socket: bool = False
    paid_calls: int = 0
    calls: list[dict] = field(default_factory=list)
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)


class LocalArchiveTransport(httpx.BaseTransport):
    """Route only the synthetic archive host onto a real loopback TCP connection."""

    def __init__(self, remote, port, state, tls=None):
        self.remote, self.port, self.state = remote, port, state
        self.scheme = "https" if tls else "http"
        self.network = httpx.HTTPTransport(retries=0, verify=tls or True)

    def handle_request(self, request):
        routed_payment = (
            self.state.paid_socket and request.url.path == "/archiver.php" and request.method == "POST"
        )
        if request.url.host != "archive.example" and not routed_payment:
            return self.remote(request)
        if not routed_payment:
            self.remote.requests.append(request)
        mapped = httpx.Request(
            request.method,
            f"{self.scheme}://127.0.0.1:{self.port}{request.url.raw_path.decode()}",
            headers=request.headers,
            content=request.content,
            extensions=request.extensions,
        )
        return self.network.handle_request(mapped)

    def close(self):
        self.network.close()


@pytest.fixture(params=[False, True], ids=["http", "https"])
def local_queue(files, remote, tmp_path, request):
    # Padding gives real partial writes while keeping a small valid three-page archive.
    buffer = io.BytesIO(remote.payload)
    with zipfile.ZipFile(buffer, "a", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("synthetic-padding.bin", random.Random(92817).randbytes(1048576))
    payload = buffer.getvalue()
    remote = SourceFixture(payload)
    state = State(payload)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def do_POST(self):
            state.paid_calls += 1
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response = None
            if state.mode != "paid_before":
                request = httpx.Request(
                    "POST", f"https://e-hentai.org{self.path}", headers=dict(self.headers), content=body
                )
                response = remote(request)
            if state.mode in {"paid_before", "paid_after"}:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
                return
            assert response is not None
            self.send_response(response.status_code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            if state.mode == "paid_stall":
                self.wfile.write(response.content[:1])
                self.wfile.flush()
                state.entered.set()
                state.release.wait(5)
                with contextlib.suppress(OSError):
                    self.wfile.write(response.content[1:])
            else:
                self.wfile.write(response.content)

        def do_GET(self):
            entry = {
                "range": self.headers.get("Range"),
                "if_range": self.headers.get("If-Range"),
                "mode": state.mode,
                "at": time.monotonic(),
            }
            state.calls.append(entry)
            if state.mode == "reset_headers":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
                return
            start = 0
            if self.headers.get("Range") and not state.ignore_range:
                start = int(self.headers["Range"].removeprefix("bytes=").removesuffix("-"))
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", "application/zip")
            if state.close_delimited:
                self.close_connection = True
            else:
                self.send_header("Content-Length", str(len(payload) - start))
            if state.etag:
                self.send_header("ETag", '"unexpected-version"' if state.wrong_etag else state.etag)
            if start:
                self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            self.end_headers()
            try:
                if state.mode == "trickle":
                    for offset in range(start, start + 100):
                        self.wfile.write(payload[offset : offset + 1])
                        self.wfile.flush()
                        state.entered.set()
                        if state.release.wait(0.05):
                            break
                    self.close_connection = True
                    return
                if state.mode in {"drop", "gate"}:
                    self.wfile.write(payload[start : start + state.cut])
                    self.wfile.flush()
                    state.entered.set()
                    if state.mode == "gate":
                        state.release.wait(30)
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    self.close_connection = True
                    return
                if state.mode == "stall":
                    state.entered.set()
                    state.release.wait(30)
                self.wfile.write(payload[start:])
                self.wfile.flush()
            except (OSError, BrokenPipeError):
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = None
    if request.param:
        import datetime
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_path, key_path = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(cert_path, key_path)
        server.socket = server_tls.wrap_socket(server.socket, server_side=True)
        tls = ssl.create_default_context(cafile=str(cert_path))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = LocalArchiveTransport(remote, server.server_port, state, tls)
    client = SourceHTTP(SourceSettings(), {}, transport=transport)
    service = Downloads(files, http=client)
    try:
        yield service, remote, state
    finally:
        state.release.set()
        service.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def assert_done(service, job, payload):
    drive(service)
    result = service.get(job["id"], private=True)
    assert result["state"] == "done", result
    with service.library.connection() as db:
        count = db.execute("SELECT count(*) FROM book").fetchone()[0]
    assert count == 1
    book = service.library.get_book(result["book_id"])
    assert book.content.sha256 == hashlib.sha256(payload).hexdigest()
    assert not service.working
    return result


@pytest.mark.parametrize("resume_mode", ["range", "ignored_range", "no_etag"])
def test_real_disconnect_recovers_exact_archive(local_queue, resume_mode):
    service, remote, state = local_queue
    if resume_mode == "no_etag":
        state.etag = None
    job = prepare(service)
    state.mode = "drop"
    assert service.step("transfer")
    failed = service.get(job["id"], private=True)
    partial = service._path(job["id"]).stat().st_size
    assert failed["state"] == "queued" and failed["error_code"] == "source_network", failed
    assert 0 < partial < len(state.payload)
    assert failed["received"] == partial
    assert failed["attempts"] == 1 and failed["retry_at"] > time.time()
    state.mode, state.ignore_range = "normal", resume_mode == "ignored_range"
    due(service)
    assert_done(service, job, state.payload)
    expected_range = None if resume_mode == "no_etag" else f"bytes={partial}-"
    assert state.calls[1]["range"] == expected_range
    assert len(archiver_posts(remote)) == 1


def test_changed_etag_requires_manual_fresh_transfer(local_queue):
    service, remote, state = local_queue
    job = prepare(service)
    state.mode = "drop"
    service.step("transfer")
    state.mode, state.wrong_etag = "normal", True
    due(service)
    service.step("transfer")
    failed = service.get(job["id"])
    assert failed["state"] == "error" and failed["error_code"] == "source_range_invalid", failed
    state.wrong_etag = False
    action(service, job, "resume")
    assert_done(service, job, state.payload)
    assert state.calls[-1]["range"] is None
    assert len(archiver_posts(remote)) == 1


def test_header_disconnect_budget_and_retry(local_queue):
    service, remote, state = local_queue
    job = prepare(service)
    state.mode = "reset_headers"
    observations = []
    for attempt in range(1, 6):
        due(service)
        assert service.step("transfer")
        current = service.get(job["id"])
        observations.append(
            {"state": current["state"], "attempts": current["attempts"], "error": current["error_code"]}
        )
        assert current["attempts"] == attempt and current["error_code"] == "source_network"
        assert current["state"] == ("error" if attempt == 5 else "queued")
    assert not service.step("transfer")
    action(service, job, "resume")
    assert service.get(job["id"])["attempts"] == 0
    state.mode = "normal"
    assert_done(service, job, state.payload)
    assert len(archiver_posts(remote)) == 1


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_control_during_blocked_socket_and_relaunch(local_queue, command):
    service, remote, state = local_queue
    job = prepare(service)
    state.mode = "gate"
    worker = threading.Thread(target=lambda: service.step("transfer"))
    worker.start()
    assert state.entered.wait(3)
    deadline = time.monotonic() + 3
    path = service._path(job["id"])
    while not path.exists() or path.stat().st_size < 65536:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    with pytest.raises(InvalidInput):
        action(service, job, "redownload")
    try:
        action(service, job, command)
        worker.join(2)
        assert not worker.is_alive()
    finally:
        state.release.set()
        worker.join(3)
    settled = service.get(job["id"])
    assert settled["state"] == ("paused" if command == "pause" else "cancelled"), settled
    if command == "cancel":
        assert not path.exists()
        partial = 0
    else:
        partial = path.stat().st_size
        assert 0 < partial < len(state.payload)
        assert settled["received"] == partial
    assert not service.working
    state.mode = "normal"
    action(service, job, "resume" if command == "pause" else "redownload")
    drive(service)
    repeated = service.get(job["id"])
    quality_approval = repeated["state"] == "needs_approval"
    if quality_approval:
        assert repeated["details"]["reusable"]
        action(service, job, "approve", mode="reuse")
    assert_done(service, job, state.payload)
    if command == "pause":
        assert state.calls[-1]["range"] == f"bytes={partial}-"
    assert len(archiver_posts(remote)) == 1


@pytest.mark.parametrize("command", ["pause", "cancel"])
@pytest.mark.parametrize("close_delimited", [False, True])
def test_stalled_body_is_interrupted_without_waiting_for_read_timeout(
    local_queue, command, close_delimited, record_property
):
    service, remote, state = local_queue
    job = prepare(service)
    state.mode = "stall"
    state.close_delimited = close_delimited
    worker = threading.Thread(target=lambda: service.step("transfer"))
    worker.start()
    try:
        assert state.entered.wait(3)
        deadline = time.monotonic() + 3
        while not service._path(job["id"]).exists():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        start = time.monotonic()
        action(service, job, command)
        worker.join(2)
        elapsed = time.monotonic() - start
        record_property("interrupt_seconds", elapsed)
        assert not worker.is_alive(), "Cancellation still waits for the 20-second socket timeout"
        settled = service.get(job["id"])
        assert settled["state"] == ("paused" if command == "pause" else "cancelled")
        assert settled["error_code"] is None and settled["attempts"] == 0
        assert settled["details"]["phase"] == "transfer"
        assert elapsed < 1
        assert not service.working
        if command == "cancel":
            assert not service._path(job["id"]).exists()
        assert len(archiver_posts(remote)) == 1
    finally:
        state.release.set()
        worker.join(3)


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_tiny_trickle_respects_cancellation(local_queue, command):
    service, remote, state = local_queue
    job = prepare(service)
    state.mode = "trickle"
    worker = threading.Thread(target=lambda: service.step("transfer"))
    worker.start()
    assert state.entered.wait(3)
    deadline = time.monotonic() + 3
    while not service.get(job["id"], private=True)["details"].get("etag"):
        assert time.monotonic() < deadline
        time.sleep(0.005)
    start = time.monotonic()
    action(service, job, command)
    worker.join(2)
    elapsed = time.monotonic() - start
    state.release.set()
    assert not worker.is_alive()
    settled = service.get(job["id"])
    assert settled["state"] == ("paused" if command == "pause" else "cancelled")
    assert elapsed < 1
    assert not service.working


@pytest.mark.parametrize("fault", ["paid_before", "paid_after"])
def test_payment_response_disconnect_recovers_without_double_charge(local_queue, fault):
    service, remote, state = local_queue
    job = ready(service)
    action(service, job, "approve")
    state.paid_socket, state.mode = True, fault
    before = remote.balance
    assert service.step("meta")
    uncertain = service.get(job["id"], private=True)
    assert uncertain["state"] == "queued"
    assert uncertain["details"]["phase"] == "recover"
    assert uncertain["error_code"] == "source_network"
    state.mode = "normal"
    due(service)
    assert_done(service, job, state.payload)
    assert before - remote.balance == remote.cost == 25
    assert state.paid_calls == (2 if fault == "paid_before" else 1)


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_cancelled_paid_body_keeps_uncertainty_and_does_not_charge_twice(local_queue, command):
    service, remote, state = local_queue
    job = ready(service)
    action(service, job, "approve")
    before = remote.balance
    state.paid_socket, state.mode = True, "paid_stall"
    worker = threading.Thread(target=lambda: service.step("meta"))
    worker.start()
    try:
        assert state.entered.wait(3)
        action(service, job, command)
        worker.join(2)
        assert not worker.is_alive()
        stopped = service.get(job["id"], private=True)
        assert stopped["state"] == ("paused" if command == "pause" else "cancelled")
        assert stopped["details"]["may_be_charged"]
        assert stopped["details"]["phase"] == "recover"
        assert stopped["error_code"] is None and stopped["attempts"] == 0
    finally:
        state.release.set()
        worker.join(3)
    state.mode = "normal"
    action(service, job, "resume" if command == "pause" else "redownload")
    drive(service)
    pending = service.get(job["id"])
    if pending["state"] == "needs_approval":
        action(service, job, "approve", mode="reuse")
    assert_done(service, job, state.payload)
    assert before - remote.balance == remote.cost == 25
    assert state.paid_calls == 1


def wait_for_streams(http, identifiers):
    deadline = time.monotonic() + 3
    while True:
        with http.interrupt_lock:
            if set(http.active_streams) == set(identifiers):
                return
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_interrupt_is_scoped_to_one_active_job(local_queue):
    service, _, state = local_queue
    http = service.http
    state.mode = "stall"
    stops = {key: threading.Event() for key in ("one", "two")}
    outcomes = {}

    def read(key):
        try:
            with http.interruptible(key):
                outcomes[key] = http.read(
                    "GET", "https://archive.example/file.zip", cancelled=stops[key].is_set
                )
        except InterruptedError:
            outcomes[key] = "cancelled"

    workers = [threading.Thread(target=read, args=(key,)) for key in stops]
    for worker in workers:
        worker.start()
    try:
        wait_for_streams(http, stops)
        stops["one"].set()
        http.interrupt("one")
        workers[0].join(2)
        assert not workers[0].is_alive() and outcomes["one"] == "cancelled"
        assert workers[1].is_alive() and "two" not in outcomes
    finally:
        state.release.set()
        for worker in workers:
            worker.join(3)
    assert outcomes["two"] == state.payload
    assert not http.active_streams


def test_completed_response_cannot_interrupt_a_reused_connection(local_queue):
    service, _, state = local_queue
    http = service.http
    outcome = []

    def read():
        with http.interruptible("new"):
            outcome.append(http.read("GET", "https://archive.example/file.zip", cancelled=lambda: False))

    with http.interruptible("old"), http.stream("GET", "https://archive.example/file.zip") as response:
        assert response.read() == state.payload
        # HTTPX returns the connection to its pool on body exhaustion, before context exit.
        assert not http.active_streams
        state.mode = "stall"
        worker = threading.Thread(target=read)
        worker.start()
        try:
            wait_for_streams(http, {"new"})
            http.interrupt("old")
            worker.join(0.05)
            assert worker.is_alive()
        finally:
            state.release.set()
            worker.join(3)
        assert outcome == [state.payload]
    assert not http.active_streams


def test_cancel_just_before_response_registration_is_not_lost(local_queue, monkeypatch):
    service, _, state = local_queue
    job = prepare(service)
    state.mode = "stall"
    arrived, proceed = threading.Event(), threading.Event()
    track = service.http._track_response

    def delayed(response):
        arrived.set()
        assert proceed.wait(3)
        return track(response)

    monkeypatch.setattr(service.http, "_track_response", delayed)
    worker = threading.Thread(target=lambda: service.step("transfer"))
    worker.start()
    try:
        assert arrived.wait(3)
        action(service, job, "cancel")
        proceed.set()
        worker.join(2)
        assert not worker.is_alive()
        assert service.get(job["id"])["state"] == "cancelled"
        assert not service.http.active_streams
    finally:
        proceed.set()
        state.release.set()
        worker.join(3)
