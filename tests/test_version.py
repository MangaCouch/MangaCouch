from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mangacouch import version

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def build_root(tmp_path: Path, monkeypatch) -> Path:
    for name in ("MANGACOUCH_BUILD_SHA", "MANGACOUCH_BUILD_REF", "GITHUB_REF"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    (tmp_path / "src" / "mangacouch").mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize("ref,expected", [("refs/heads/main", "main"), ("refs/tags/v1.0.0", "v1.0.0")])
def test_build_inputs_work_without_git(build_root: Path, monkeypatch, ref, expected):
    monkeypatch.setenv("MANGACOUCH_BUILD_SHA", SHA)
    monkeypatch.setenv("MANGACOUCH_BUILD_REF", ref)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    assert version.build_version(build_root) == f"{expected}-0123456"


def test_release_tag_must_match_project_version(build_root: Path, monkeypatch):
    monkeypatch.setenv("MANGACOUCH_BUILD_SHA", SHA)
    monkeypatch.setenv("MANGACOUCH_BUILD_REF", "refs/tags/v1.0.1")
    with pytest.raises(ValueError, match="does not match"):
        version.build_version(build_root)


def test_sdist_rebuild_preserves_identity_in_unrelated_ci(build_root: Path, monkeypatch):
    stored = build_root / "src" / "mangacouch" / version.BUILD_FILE
    stored.write_text("v1.0.0-0123456\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v9.9.9")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    version.write_build_version(build_root)
    assert stored.read_text(encoding="utf-8") == "v1.0.0-0123456\n"


def test_main_and_tag_builds_of_same_commit_have_distinct_identities(build_root: Path):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(build_root), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    git("init", "--initial-branch=main")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    git("-c", "tag.gpgsign=false", "tag", "v1.0.0")
    sha = git("rev-parse", "HEAD")[:7]
    assert version.build_version(build_root) == f"main-{sha}"
    git("checkout", "--detach", "v1.0.0")
    assert version.build_version(build_root) == f"v1.0.0-{sha}"


def test_installed_runtime_uses_embedded_identity(build_root: Path, monkeypatch):
    package = build_root / "src" / "mangacouch"
    (package / version.BUILD_FILE).write_text("v1.0.0-0123456\n", encoding="utf-8")
    monkeypatch.setattr(version, "__file__", str(package / "version.py"))
    monkeypatch.setenv("MANGACOUCH_BUILD_REF", "refs/heads/main")
    monkeypatch.setenv("MANGACOUCH_BUILD_SHA", "a" * 40)
    version.current_version.cache_clear()
    try:
        assert version.current_version() == "v1.0.0-0123456"
    finally:
        version.current_version.cache_clear()
