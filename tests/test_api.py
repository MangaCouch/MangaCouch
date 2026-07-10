"""End-to-end API tests over the real app (read flow, single-owner auth, upload, search)."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from mangacouch.auth.security import hash_api_key, hash_passcode
from mangacouch.config import Config
from mangacouch.core import sidecars
from mangacouch.db.base import session_scope
from mangacouch.db.models import AuthCredential
from mangacouch.state import AppContext, build_context

from .conftest import make_zip


def _bearer(raw_key: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + base64.b64encode(raw_key.encode()).decode()}


@dataclass
class Env:
    client: TestClient
    ctx: AppContext
    owner_key: str
    archive_id: str


@pytest.fixture
def env(roots: Config, sample_pages) -> Iterator[Env]:
    from mangacouch.app import create_app

    ctx = build_context(roots, use_process_pool=False)
    # Provision credentials with known keys.
    with session_scope() as session:
        session.add(
            AuthCredential(
                role="owner",
                passcode_hash=hash_passcode("ownerpass"),
                api_key_hash=hash_api_key("owner-key"),
                enabled=True,
            )
        )
        # A legacy "reader" credential row must be ignored by the single-user auth model.
        session.add(
            AuthCredential(
                role="reader",
                passcode_hash=hash_passcode("readerpass"),
                api_key_hash=hash_api_key("legacy-reader-key"),
                enabled=True,
            )
        )

    # Tag the gallery via a native sidecar so search/tags have something to chew on.
    path = make_zip(roots.manga_root / "Test Gallery.zip", sample_pages)
    sidecars.write_mc(
        path,
        sidecars.McSidecar(
            archive_id="", fingerprint=None, format="zip", page_count=0,
            original_filename="Test Gallery.zip", title="Test Gallery",
            tags=["artist:tester", "female:lolicon", "language:english"],
        ),
    )
    archive_id = ctx.ingestor.index_file(path)
    assert archive_id

    app = create_app(context=ctx)
    with TestClient(app) as client:
        yield Env(client, ctx, "owner-key", archive_id)


def test_health(env: Env):
    r = env.client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_auth_required(env: Env):
    assert env.client.get("/api/archives").status_code == 401


def test_login_and_list(env: Env):
    # The legacy reader passcode no longer logs in.
    assert env.client.post("/api/auth/login", json={"passcode": "readerpass"}).status_code == 401
    r = env.client.post("/api/auth/login", json={"passcode": "ownerpass"})
    assert r.status_code == 200
    token = r.json()
    assert token["role"] == "owner"
    # Use the returned session token.
    r2 = env.client.get("/api/archives", headers=_bearer(token["api_key"]))
    assert r2.status_code == 200
    body = r2.json()
    assert body["total"] == 1
    assert body["archives"][0]["id"] == env.archive_id


def test_detail_pages_and_image(env: Env):
    h = _bearer(env.owner_key)
    detail = env.client.get(f"/api/archives/{env.archive_id}", headers=h).json()
    assert detail["title"] == "Test Gallery"
    assert any(t["namespace"] == "artist" for t in detail["tags"])

    pages = env.client.get(f"/api/archives/{env.archive_id}/pages", headers=h).json()
    assert len(pages["pages"]) == 3
    first = pages["pages"][0]["path"]

    img = env.client.get(
        f"/api/archives/{env.archive_id}/page", params={"path": first}, headers=h
    )
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/")
    assert len(img.content) > 0


def test_media_auth_via_query_key(env: Env):
    """`<img>` can't set headers, so ?key= must authenticate media routes."""
    key = base64.b64encode(env.owner_key.encode()).decode()
    r = env.client.get(f"/api/archives/{env.archive_id}/thumbnail", params={"key": key})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"


def test_search_by_namespace(env: Env):
    h = _bearer(env.owner_key)
    hit = env.client.get("/api/archives", params={"q": "artist:tester"}, headers=h).json()
    assert hit["total"] == 1
    miss = env.client.get("/api/archives", params={"q": "artist:nobody"}, headers=h).json()
    assert miss["total"] == 0


def test_progress_and_read_flag(env: Env):
    h = _bearer(env.owner_key)
    env.client.put(f"/api/archives/{env.archive_id}/progress/3", headers=h)
    detail = env.client.get(f"/api/archives/{env.archive_id}", headers=h).json()
    assert detail["progress"]["page"] == 3
    assert detail["read"] is True  # 3/3 > 0.85


def test_legacy_reader_key_is_rejected(env: Env):
    h = _bearer("legacy-reader-key")
    assert env.client.get("/api/archives", headers=h).status_code == 401
    assert env.client.get("/api/config", headers=h).status_code == 403


def test_owner_metadata_update(env: Env):
    h = _bearer(env.owner_key)
    r = env.client.put(
        f"/api/archives/{env.archive_id}/metadata",
        json={"title": "Renamed", "rating": 5.0, "tags": ["artist:newguy"]},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"
    # The search index reflects the new tag.
    hit = env.client.get("/api/archives", params={"q": "artist:newguy"}, headers=h).json()
    assert hit["total"] == 1


def test_upload_zip(env: Env, sample_pages, tmp_path):
    h = _bearer(env.owner_key)
    payload = make_zip(tmp_path / "uploaded.zip", sample_pages).read_bytes()
    r = env.client.post(
        "/api/upload",
        files={"file": ("uploaded.zip", payload, "application/zip")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["archive_id"]


def test_categories(env: Env):
    h = _bearer(env.owner_key)
    cat = env.client.post(
        "/api/categories", json={"name": "Faves", "type": "static"}, headers=h
    ).json()
    env.client.put(f"/api/categories/{cat['id']}/{env.archive_id}", headers=h)
    listed = env.client.get(
        "/api/archives", params={"category": cat["id"]}, headers=h
    ).json()
    assert listed["total"] == 1


def test_plugins_listed(env: Env):
    h = _bearer(env.owner_key)
    plugins = env.client.get("/api/plugins", headers=h).json()["plugins"]
    namespaces = {p["namespace"] for p in plugins}
    assert {"ehentai_login", "ehentai_download", "ehentai_metadata"} <= namespaces


def test_change_passcode(env: Env):
    h = _bearer(env.owner_key)
    # The passcode change requires the correct current passcode.
    assert env.client.post(
        "/api/auth/passcode",
        json={"new_passcode": "newownerpw", "current_passcode": "wrong"},
        headers=h,
    ).status_code == 401
    assert env.client.post(
        "/api/auth/passcode",
        json={"new_passcode": "newownerpw", "current_passcode": "ownerpass"},
        headers=h,
    ).status_code == 200
    assert env.client.post("/api/auth/login", json={"passcode": "newownerpw"}).json()["role"] == "owner"

    # Unauthenticated callers may not change passcodes.
    assert env.client.post(
        "/api/auth/passcode", json={"new_passcode": "nope"}
    ).status_code == 403


def test_ehentai_cookie_bootstrap_is_encrypted(roots: Config):
    """Cookies provided in config.toml are imported into the encrypted plugin store (§5.6)."""
    roots.acquisition.ehentai = {
        "ipb_member_id": "12345",
        "ipb_pass_hash": "super-secret",
        "igneous": "abcdef",
    }
    ctx = build_context(roots, use_process_pool=False)
    try:
        ctx._bootstrap_ehentai_cookies()
        decrypted = ctx.plugin_config("ehentai_login")
        assert decrypted["ipb_member_id"] == "12345"
        assert decrypted["ipb_pass_hash"] == "super-secret"  # decrypts back

        from mangacouch.db.models import PluginConfig

        with session_scope() as session:
            row = (
                session.query(PluginConfig)
                .filter_by(namespace="ehentai_login", key="ipb_pass_hash")
                .one()
            )
            assert row.is_secret is True
            assert row.value != "super-secret"  # stored as ciphertext, not plaintext
    finally:
        ctx.shutdown()


def test_gp_balance_without_url(env: Env, monkeypatch):
    """Check GP works with no URL — returns just the account balance (frontend field names)."""
    import mangacouch.api.routers.downloads as dl

    monkeypatch.setattr(env.ctx.download_worker, "login_session", lambda ns: object())
    monkeypatch.setattr(dl, "fetch_funds", lambda session, domain="e-hentai": (1234, 7))

    r = env.client.get("/api/ehentai/balance", headers=_bearer(env.owner_key))
    assert r.status_code == 200
    body = r.json()
    assert body["balance"] == 1234
    assert body["credits"] == 7
    assert body["original_cost"] is None
    assert body["resample_cost"] is None
    assert body["sufficient"] is None


def test_gp_balance_with_url(env: Env, monkeypatch):
    """With a gallery URL the calculator returns balance + per-archive costs + sufficiency."""
    import mangacouch.api.routers.downloads as dl
    from mangacouch.acquisition.ehentai import ArchiverPage

    monkeypatch.setattr(env.ctx.download_worker, "login_session", lambda ns: object())
    monkeypatch.setattr(
        dl,
        "fetch_archiver_page",
        lambda session, ref: ArchiverPage(
            current_gp=1000, credits=5, original_cost=200, resample_cost=10
        ),
    )
    r = env.client.get(
        "/api/ehentai/balance",
        params={"url": "https://e-hentai.org/g/123/0a1b2c3d4e/"},
        headers=_bearer(env.owner_key),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["balance"] == 1000
    assert body["original_cost"] == 200
    assert body["resample_cost"] == 10
    assert body["sufficient"] is True


def test_opds_root(env: Env):
    key = base64.b64encode(env.owner_key.encode()).decode()
    r = env.client.get("/api/opds", params={"key": key})
    assert r.status_code == 200
    assert "<feed" in r.text
    assert env.archive_id in r.text


def test_download_original_archive(env: Env):
    r = env.client.get(f"/api/archives/{env.archive_id}/download", headers=_bearer(env.owner_key))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "Gallery.zip" in r.headers.get("content-disposition", "")
    assert r.content[:2] == b"PK"  # the original bytes, not a re-pack
    # Media-style ?key= auth works too (OPDS readers can't set headers).
    key = base64.b64encode(env.owner_key.encode()).decode()
    r2 = env.client.get(f"/api/archives/{env.archive_id}/download", params={"key": key})
    assert r2.status_code == 200


def test_opds_acquisition_and_key_propagation(env: Env):
    key = base64.b64encode(env.owner_key.encode()).decode()
    r = env.client.get("/api/opds", params={"key": key})
    assert r.status_code == 200
    assert "opds-spec.org/acquisition" in r.text
    # Generated media links must carry the caller's key or readers 401 on every cover/page.
    assert f"/api/archives/{env.archive_id}/download?key=" in r.text
    assert f"/api/archives/{env.archive_id}/thumbnail?key=" in r.text


def test_thumbnail_prewarm_sweep(env: Env):
    from mangacouch.core.thumbnails import VARIANT_PAGE

    stats = env.ctx.prewarm_thumbnails()
    assert stats["generated"] == 3
    assert all(env.ctx.thumbs.has(env.archive_id, p, VARIANT_PAGE) for p in range(3))
    # Idempotent: a second sweep generates nothing.
    stats2 = env.ctx.prewarm_thumbnails()
    assert stats2["generated"] == 0
    assert stats2["skipped"] >= 1


def test_login_returns_client_defaults(env: Env):
    r = env.client.post("/api/auth/login", json={"passcode": "ownerpass"})
    assert r.status_code == 200
    defaults = r.json()["defaults"]
    assert defaults["reader"]["mode"] in ("scroll", "paged")
    assert defaults["reader"]["direction"] in ("rtl", "ltr")
    assert isinstance(defaults["auto_lock_minutes"], int)


# -- new feature coverage -------------------------------------------------------------------------


def test_random_archive(env: Env):
    h = _bearer(env.owner_key)
    r = env.client.get("/api/archives/random", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == env.archive_id
    assert body["new"] is True  # never opened yet

    # Once everything is read, random still returns something (new: False).
    env.client.put(f"/api/archives/{env.archive_id}/progress/3", headers=h)
    body2 = env.client.get("/api/archives/random", headers=h).json()
    assert body2["id"] == env.archive_id
    assert body2["new"] is False


def test_favorite_toggle(env: Env):
    h = _bearer(env.owner_key)
    detail = env.client.get(f"/api/archives/{env.archive_id}", headers=h).json()
    assert detail["favorite"] is False

    assert env.client.put(
        f"/api/archives/{env.archive_id}/favorite", headers=h
    ).json()["favorite"] is True
    detail = env.client.get(f"/api/archives/{env.archive_id}", headers=h).json()
    assert detail["favorite"] is True

    assert env.client.delete(
        f"/api/archives/{env.archive_id}/favorite", headers=h
    ).json()["favorite"] is False
    detail = env.client.get(f"/api/archives/{env.archive_id}", headers=h).json()
    assert detail["favorite"] is False


def test_first_run_flow(env: Env):
    from mangacouch.db.models import AppConfig

    with session_scope() as session:
        session.add(AppConfig(key="first_run_pending", value="true"))

    status = env.client.get("/api/auth/status").json()
    assert status["first_run"] is True
    assert status["owner_configured"] is True

    r = env.client.post("/api/auth/first-run", json={"regenerate": True})
    assert r.status_code == 200
    passcode = r.json()["passcode"]
    assert len(passcode) >= 10

    # The window is single-use.
    assert env.client.post("/api/auth/first-run", json={"regenerate": True}).status_code == 403
    assert env.client.get("/api/auth/status").json()["first_run"] is False

    # The regenerated passcode logs in.
    assert env.client.post("/api/auth/login", json={"passcode": passcode}).status_code == 200


def test_search_translated_tag(env: Env):
    """Searching by the EhTagTranslation (Chinese) name must match the raw English tag."""
    from mangacouch.db.models import TagTranslation

    with session_scope() as session:
        session.add(TagTranslation(namespace="female", raw="lolicon", translated="萝莉"))
        env.ctx.translator.load(session)
    env.ctx.rebuild_search()

    h = _bearer(env.owner_key)
    hit = env.client.get("/api/archives", params={"q": "萝莉"}, headers=h).json()
    assert hit["total"] == 1
    miss = env.client.get("/api/archives", params={"q": "不存在的标签"}, headers=h).json()
    assert miss["total"] == 0


def test_delete_archive_removes_file(env: Env, sample_pages, tmp_path):
    h = _bearer(env.owner_key)
    payload = make_zip(tmp_path / "todelete.zip", sample_pages).read_bytes()
    up = env.client.post(
        "/api/upload",
        files={"file": ("todelete.zip", payload, "application/zip")},
        headers=h,
    ).json()
    archive_id = up["archive_id"]
    rel = env.client.get(f"/api/archives/{archive_id}", headers=h).json()

    r = env.client.delete(f"/api/archives/{archive_id}", headers=h)
    assert r.status_code == 200
    assert r.json()["file_removed"] is True
    assert not (env.ctx.config.manga_root / r.json()["rel_path"]).exists()
    assert rel is not None  # sanity: the row existed before deletion


def test_passcode_generator_mixes_classes():
    from mangacouch.cli import friendly_passcode

    for _ in range(20):
        code = friendly_passcode()
        assert any(c.isdigit() for c in code)
        assert any(c.isupper() for c in code)
        assert any(c.islower() for c in code)


def test_login_returns_stable_media_key(env: Env):
    """The media key must be identical across logins so cached image URLs stay valid."""
    r1 = env.client.post("/api/auth/login", json={"passcode": "ownerpass"}).json()
    r2 = env.client.post("/api/auth/login", json={"passcode": "ownerpass"}).json()
    assert r1["media_key"] == r2["media_key"]
    assert r1["api_key"] != r2["api_key"]  # sessions still rotate

    # The media key authenticates media routes...
    key = base64.b64encode(r1["media_key"].encode()).decode()
    thumb = env.client.get(f"/api/archives/{env.archive_id}/thumbnail", params={"key": key})
    assert thumb.status_code == 200
    # ...but NOT ordinary API routes (it must never grant full API access).
    assert env.client.get("/api/archives", headers=_bearer(r1["media_key"])).status_code == 401
