from __future__ import annotations

import io
import logging

import httpx
import pytest
from source_fixtures import EH

from mangacouch.auth import Auth, Unauthorized
from mangacouch.logs import configure, parse_level, short_id
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import SourceError, SourceHTTP


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    names = ("mangacouch", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx")
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    root.handlers[:], root.level = saved
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def test_configure_formats_and_filters(restore_logging):
    stream = io.StringIO()
    configure("info", stream)
    logging.getLogger("mangacouch.downloads").info("Download abc queued")
    logging.getLogger("mangacouch.downloads").debug("hidden at info")
    logging.getLogger("httpx").info("HTTP Request: GET https://archive.example/secret-key")
    logging.getLogger("uvicorn.error").info("Uvicorn running")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("INFO    mangacouch.downloads: Download abc queued")
    assert "uvicorn.error: Uvicorn running" in lines[1]

    stream = io.StringIO()
    configure("debug", stream)
    logging.getLogger("mangacouch.source_http").debug("GET e-hentai.org/g/1/a/ -> 200")
    assert "DEBUG   mangacouch.source_http" in stream.getvalue()


def test_parse_level_and_short_id():
    assert parse_level(None, "info") == "info"
    assert parse_level(" DEBUG ", "info") == "debug"
    with pytest.raises(ValueError):
        parse_level("verbose", "info")
    assert short_id("4fcc3da3-0000-4000-8000-000000000000") == "4fcc3da3"


def test_source_requests_log_without_archive_keys(caplog):
    caplog.set_level(logging.DEBUG, logger="mangacouch")
    ban = b"temporarily banned due to an excessive request rate. The ban expires in 9 seconds"
    responses = {
        "e-hentai.org": httpx.Response(200, content=b"<html></html>"),
        "archive.example": httpx.Response(200, content=b"zip"),
        "api.e-hentai.org": httpx.Response(200, content=ban),
    }
    client = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(lambda request: responses[request.url.host]),
    )
    try:
        client.read("GET", EH + "?p=1")
        client.read("GET", "https://archive.example/archive/1/secret-key/org/2")
        with pytest.raises(SourceError, match="source_rate_limited"):
            client.read("POST", "https://api.e-hentai.org/api.php")
    finally:
        client.close()
    text = caplog.text
    assert "GET e-hentai.org/g/" in text and "?p=1" not in text
    assert "GET archive.example/... -> 200" in text and "secret-key" not in text
    warning = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warning) == 1
    assert warning[0].getMessage().startswith("ehentai/exhentai: IP temporarily banned")


def test_sign_in_outcomes_are_logged(caplog, library):
    caplog.set_level(logging.INFO, logger="mangacouch")
    auth = Auth(library)
    auth.initialize("fixture-passcode")
    with pytest.raises(Unauthorized):
        auth.login("wrong-passcode", "phone", client="192.0.2.7")
    auth.login("fixture-passcode", "phone", client="192.0.2.7")
    messages = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert ("WARNING", "Sign-in failed from 192.0.2.7") in messages
    assert ("INFO", "Signed in from 192.0.2.7 as 'phone'") in messages
    assert "fixture-passcode" not in caplog.text and "wrong-passcode" not in caplog.text
