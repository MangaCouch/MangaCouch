from __future__ import annotations

import httpx
import pytest
from helpers import download_step
from source_fixtures import EH

from mangacouch.database import RevisionConflict
from mangacouch.source_config import SourceSettings


def test_fake_ip_setting_round_trips_through_source_settings(queue):
    service, _ = queue
    revision = service.vault.load()[2]
    public = service.save_settings(SourceSettings(allow_fake_ip=True), {}, revision)
    assert public["settings"]["allow_fake_ip"] is True
    assert service.http.settings.allow_fake_ip is True


def test_saved_settings_apply_to_requests_without_closing_an_active_stream(queue):
    service, remote = queue
    old_client = service.http.client
    with service.http.stream("GET", EH) as response:
        result = service.save_settings(
            SourceSettings(user_agent="new-agent", comments=False), {"eh_cookie": "new-cookie"}, 0
        )
        assert result["revision"] == 1
        # Header-only changes reuse the client; only a proxy change needs a new one.
        assert service.http.client is old_client
        replacement = httpx.Client(transport=service.http.transport)
        service.http.apply(service.http.settings, service.http.secrets, replacement)
        assert not old_client.is_closed
        assert response.read()
        service.http.read("GET", EH)
        assert remote.requests[-1].headers["Cookie"] == "new-cookie; nw=1"
        assert remote.requests[-1].headers["User-Agent"] == "new-agent"
        assert not service.settings.comments
    assert old_client.is_closed
    with pytest.raises(RevisionConflict):
        service.save_settings(SourceSettings(), {}, 0)
    assert service.http.secrets["eh_cookie"] == "new-cookie"
    with pytest.raises(ValueError):
        service.save_settings(SourceSettings(), {"unexpected": "value"}, 1)
    assert service.config_revision == service.vault.load()[2] == 1
    service.save_settings(SourceSettings(), {"eh_cookie": ""}, 1)
    service.http.read("GET", EH)
    assert remote.requests[-1].headers["Cookie"] == "nw=1"


def test_unchanged_settings_save_is_a_no_op_and_proxy_is_validated_first(queue):
    service, _ = queue
    service.save_settings(SourceSettings(comments=False), {"eh_cookie": "cookie"}, 0)
    client = service.http.client
    # Same values from a stale tab: no revision bump, no conflict, no client rebuild.
    result = service.save_settings(SourceSettings(comments=False), {"eh_cookie": "cookie"}, 0)
    assert result["revision"] == 1 and service.http.client is client
    with pytest.raises(ValueError, match="proxy"):
        service.save_settings(SourceSettings(), {"proxy": "ftp://proxy.example"}, 1)
    assert service.http.client is client and service.vault.load()[2] == 1


def test_source_settings_without_secrets_and_explicit_reset(files):
    from mangacouch.source_config import SourceSettings, SourceVault

    vault = SourceVault(files.library)
    revision = vault.save(SourceSettings(comments=False), {}, 0)
    assert not vault.path.exists()
    assert vault.load()[0].comments is False
    vault.save(SourceSettings(comments=False), {"eh_cookie": "secret"}, revision)
    vault.path.unlink()
    with pytest.raises(ValueError):
        vault.load()
    vault.reset_credentials()
    assert vault.load()[1] == {}
    assert vault.load()[0].comments is False


def test_source_reset_refuses_live_worker(files, api):
    from mangacouch.source_config import SourceVault

    with api, pytest.raises(ValueError, match="Stop serve"):
        SourceVault(files.library).reset_credentials()


@pytest.mark.parametrize("damage", ["missing", "replaced"])
def test_a_lost_source_key_turns_online_sources_off_until_reset_from_the_web(files, auth, tmp_path, damage):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from helpers import ORIGIN, make_zip
    from source_fixtures import SourceFixture

    from mangacouch.configuration import HTTPSettings
    from mangacouch.downloads import Downloads
    from mangacouch.http import create_app
    from mangacouch.lifecycle import startup_status
    from mangacouch.source_config import SourceVault
    from mangacouch.source_http import SourceError, SourceHTTP

    vault = SourceVault(files.library)
    vault.save(SourceSettings(comments=False), {"eh_cookie": "secret", "proxy": "socks5h://proxy.example"}, 0)
    if damage == "missing":
        vault.path.unlink()
    else:
        vault.path.write_bytes(Fernet.generate_key())  # another library's key
    # The server still starts; only online sources are off.
    report = startup_status(files, HTTPSettings(), host="127.0.0.1", port=8000)
    assert report["source_key_lost"] is True
    assert not any("key" in blocker.lower() for blocker in report["blockers"])

    remote = SourceFixture(make_zip(tmp_path / "remote.zip").read_bytes())
    service = Downloads(files, http=SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(remote)))
    assert service.http.blocked == "source_key_lost" and service.settings.comments is False
    with pytest.raises(SourceError, match="source_key_lost"):
        service.http.read("GET", EH)
    assert remote.requests == []
    job = service.create(EH)
    assert not download_step(service) and service.get(job["id"])["state"] == "queued"
    assert not service.checks.step()

    _, key = auth.create_key("management", "key reset")
    client = TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth, downloads=service),
        base_url=ORIGIN,
        headers={"Authorization": f"Bearer {key}"},
    )
    with client:
        current = client.get("/api/sources/settings").json()
        assert current["key_lost"] is True and not any(current["configured"].values())
        assert current["settings"]["comments"] is False
        # Saving would overwrite credentials it cannot read: reset first.
        refused = client.put(
            "/api/sources/settings",
            params={"expected_revision": current["revision"]},
            json={"settings": current["settings"], "secrets": {"eh_cookie": "new"}},
        )
        assert refused.status_code == 422 and refused.json()["error"] == "source_key_lost"

        reset = client.post("/api/sources/reset-credentials")
        assert reset.status_code == 200
        body = reset.json()
        assert body["key_lost"] is False and not any(body["configured"].values())
        assert body["settings"]["comments"] is False and "budget" in body
        assert vault.path.is_file() and service.http.blocked == "source_restore_paused"
        assert service.http.secrets == {}
        saved = client.put(
            "/api/sources/settings",
            params={"expected_revision": body["revision"]},
            json={"settings": body["settings"], "secrets": {"eh_cookie": "new"}},
        )
        assert saved.status_code == 200 and saved.json()["configured"]["eh_cookie"]
    assert vault.load()[1] == {"eh_cookie": "new"}
    assert startup_status(files, HTTPSettings(), host="127.0.0.1", port=8000)["source_key_lost"] is False


def test_verify_checks_the_typed_credentials_without_storing_them(queue, api):
    service, remote = queue
    service.save_settings(SourceSettings(), {"eh_cookie": "stored"}, 0)
    stored = service.vault.load()
    original = remote.__call__

    def answer(request):
        if request.url.path == "/home.php":
            remote.requests.append(request)
            return httpx.Response(200, content=b"Image Limits")
        return original(request)

    service.http.transport.handler = answer

    def verify(site, secrets=None):
        response = api.post(
            f"/api/sources/verify/{site}", **({} if secrets is None else {"json": {"secrets": secrets}})
        )
        assert response.status_code == 200, response.text
        return remote.requests[-1].headers

    with api:
        assert verify("ehentai", {"eh_cookie": " typed "})["Cookie"] == "typed"
        # An empty field, or no body at all, checks the stored value.
        assert verify("ehentai", {"eh_cookie": ""})["Cookie"] == "stored"
        assert verify("ehentai")["Cookie"] == "stored"
        refused = api.post("/api/sources/verify/ehentai", json={"secrets": {"proxy": "ftp://proxy.example"}})
        assert refused.status_code == 422
        unknown = api.post("/api/sources/verify/ehentai", json={"secrets": {"other": "value"}})
        assert unknown.status_code == 422
        assert api.get("/api/sources/settings").json()["revision"] == stored[2]
        service.http.read("GET", EH)
        assert remote.requests[-1].headers["Cookie"] == "stored; nw=1"
    assert service.vault.load() == stored
    assert service.http.secrets == stored[1]
