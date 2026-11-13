from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "promote_main_image", REPOSITORY / "scripts/promote_main_image.py"
)
assert spec and spec.loader
promote_main_image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote_main_image)
promote = promote_main_image.promote
image_exists = promote_main_image.image_exists


class Registry:
    """The branch head and the pushed tags, as the promote job sees them."""

    def __init__(self, head: str):
        self.heads = [head]
        self.built: set[str] = set()
        self.main: str | None = None

    def head(self) -> str:
        return self.heads[0] if len(self.heads) == 1 else self.heads.pop(0)

    def promote(self) -> str | None:
        return promote(self.head, self.built.__contains__, self.retag)

    def retag(self, sha: str) -> None:
        assert sha in self.built
        self.main = sha


def test_builds_finishing_out_of_order_never_move_main_backwards():
    registry = Registry("b")
    # B was pushed after A, but its CI and build finish first.
    registry.built.add("b")
    assert registry.promote() == "b"
    registry.built.add("a")
    assert registry.promote() == "b"
    assert registry.main == "b"


def test_older_build_leaves_main_alone_until_the_head_is_built():
    registry = Registry("b")
    registry.built.add("a")
    registry.main = "z"
    assert registry.promote() is None
    assert registry.main == "z"
    registry.built.add("b")
    assert registry.promote() == "b"


def test_main_moving_during_the_retag_is_followed():
    registry = Registry("a")
    registry.built.update({"a", "b"})
    # Head is a when read, b when checked after retagging, then stays b.
    registry.heads = ["a", "b", "b", "b"]
    assert registry.promote() == "b"
    assert registry.main == "b"


def test_main_moving_to_an_unbuilt_commit_keeps_the_newest_built_head():
    registry = Registry("a")
    registry.built.add("a")
    registry.heads = ["a", "c", "c"]
    assert registry.promote() is None
    assert registry.main == "a"


def test_endlessly_moving_main_gives_up():
    registry = Registry("a")
    registry.built.update(str(n) for n in range(20))
    registry.heads = [str(n) for n in range(20)]
    with pytest.raises(RuntimeError):
        registry.promote()


def test_main_image_workflow_gates():
    workflow = (REPOSITORY / ".github/workflows/main-image.yml").read_text()
    jobs = workflow[workflow.index("jobs:") :]
    build = jobs[: jobs.index("  promote:")]
    promote_job = jobs[jobs.index("  promote:") :]
    # The build never pushes the mutable tag and never cancels another commit's build.
    assert not re.search(r"type=raw,value=main\s*$", build, re.M)
    assert "type=raw,value=main-${{ steps.rev.outputs.short }}" in build
    assert "cancel-in-progress: true" not in workflow
    assert "group: main-image-${{ github.event.workflow_run.head_sha || github.sha }}" in build
    # Manual runs publish only from main; fork pull requests named main are excluded.
    assert "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'" in build
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in build
    assert "needs: docker" in promote_job
    assert "group: main-image-promote" in promote_job
    assert "scripts/promote_main_image.py" in promote_job


def inspected(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], returncode, stdout="", stderr=stderr)


def test_only_a_missing_tag_counts_as_no_image():
    assert image_exists(inspected(0)) is True
    assert image_exists(inspected(1, "ERROR: ghcr.io/mangacouch/mangacouch:main-abc1234: not found")) is False
    assert image_exists(inspected(1, "ERROR: MANIFEST_UNKNOWN: manifest unknown")) is False
    # A registry or network failure must fail the job, not silently skip the promotion.
    for failure in ("ERROR: unexpected status: 401 Unauthorized", "toomanyrequests: rate limit", ""):
        with pytest.raises(RuntimeError):
            image_exists(inspected(1, failure))


def test_workflow_and_script_agree_on_the_short_commit_id():
    workflow = (REPOSITORY / ".github/workflows/main-image.yml").read_text()
    # Both take the first 7 characters of the full id; `rev-parse --short` may print more.
    assert 'sha="$(git rev-parse HEAD)"' in workflow
    assert 'echo "short=${sha:0:7}"' in workflow
    assert "--short=" not in workflow
    assert promote_main_image.short("0123456789abcdef") == "0123456"


def test_the_smoke_build_never_overwrites_the_multi_platform_cache():
    action = (REPOSITORY / ".github/actions/build-image/action.yml").read_text()
    assert action.count("cache-to:") == 1
    assert action.count("cache-from:") == 2
    assert "scope=${{ inputs.cache-scope }}" in action
    for name in ("main-image.yml", "release.yml"):
        assert "uses: ./.github/actions/build-image" in (REPOSITORY / ".github/workflows" / name).read_text()


def test_release_only_moves_the_latest_patch_of_its_minor():
    workflow = (REPOSITORY / ".github/workflows/release.yml").read_text()
    assert 'if [[ "v$VERSION" == "$newest_minor" ]]' in workflow
    assert 'minor_pattern="${minor//./\\\\.}"' in workflow
    assert "tags=()" in workflow
    assert "sort -V | tail -n 1" in workflow


@pytest.mark.parametrize(
    "version,latest,tags,expected",
    [
        ("1.2.3", "false", "v1.2.3\nv1.2.4", ""),
        ("1.2.4", "false", "v1.2.3\nv1.2.4\nv1.20.99", "example:1.2"),
        ("1.2.9", "false", "v1.2.9\nv1.2.10", ""),
        ("1.2.10", "auto", "v1.2.9\nv1.2.10", "example:latest"),
    ],
)
@pytest.mark.skipif(
    os.name == "nt", reason="the release job runs this script on Linux; `bash` on Windows may be WSL's"
)
def test_release_minor_promotion_executes_version_order_correctly(version, latest, tags, expected):
    import textwrap

    workflow = (REPOSITORY / ".github/workflows/release.yml").read_text()
    script = 'minor="${VERSION%.*}"' + workflow.split('minor="${VERSION%.*}"', 1)[1]
    script = textwrap.dedent("          " + script)
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'gh() { printf "%s\\n" "$FIXTURE_TAGS"; }\ndocker() { printf "%s\\n" "$@"; }\n' + script,
        ],
        env={
            **os.environ,
            "VERSION": version,
            "LATEST": latest,
            "FIXTURE_TAGS": tags,
            "IMAGE": "example",
            "GH_REPO": "fixture",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    if expected:
        assert expected in result.stdout.splitlines()
        assert f"example:{version}" in result.stdout.splitlines()
    else:
        assert result.stdout == ""
