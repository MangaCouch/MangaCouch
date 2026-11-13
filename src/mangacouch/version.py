"""Build identity shared by source checkouts, Python packages and frozen applications."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from functools import cache
from pathlib import Path

BUILD_FILE = "_build_version.txt"


def _git(root: Path, *arguments: str) -> str:
    if not (root / ".git").exists():
        return ""
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *arguments], stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_version(root: Path) -> str:
    stored = root / "src" / "mangacouch" / BUILD_FILE
    sha = os.environ.get("MANGACOUCH_BUILD_SHA", "")
    # A wheel rebuilt from an sdist keeps that sdist's identity, even inside another CI job.
    if not sha and not (root / ".git").exists() and stored.exists():
        return stored.read_text(encoding="utf-8").strip()
    sha = sha or _git(root, "rev-parse", "HEAD")
    ref = (
        os.environ.get("MANGACOUCH_BUILD_REF")
        or os.environ.get("GITHUB_REF")
        or _git(root, "symbolic-ref", "--short", "HEAD")
        or _git(root, "describe", "--tags", "--exact-match")
    )
    tag = ref.removeprefix("refs/tags/")
    prefix = "main"
    if re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        with (root / "pyproject.toml").open("rb") as stream:
            version = tomllib.load(stream)["project"]["version"]
        if tag != f"v{version}":
            raise ValueError(f"Build tag {tag} does not match project version {version}")
        prefix = tag
    if sha and not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ValueError("Build commit must be a full Git commit hash")
    return f"{prefix}-{sha[:7] if sha else 'unknown'}"


def write_build_version(root: Path) -> None:
    (root / "src" / "mangacouch" / BUILD_FILE).write_text(build_version(root) + "\n", encoding="utf-8")


@cache
def current_version() -> str:
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists():
        return build_version(root)
    try:
        return Path(__file__).with_name(BUILD_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "main-unknown"
