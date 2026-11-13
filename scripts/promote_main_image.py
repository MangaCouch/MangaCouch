"""Points the mutable `main` image tag at the image of the current head of `main`.

main-image.yml builds every successful CI run of a push to main and pushes only the immutable
`main-<short sha>` tag. CI runs finish in any order, so a build cannot simply push `main` as well: an
older commit finishing last would move the tag backwards. Instead this script runs after each build,
one at a time, and retags `main` to the head of the branch when that commit's image exists. If it
does not exist yet, the head's own build moves the tag when it finishes. It never acts on a commit
other than the current head, so any run, early or late, leaves the tag on the newest built head.

Usage: promote_main_image.py IMAGE REPOSITORY (GH_TOKEN must allow reading the repository).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

ATTEMPTS = 5


def promote(
    head: Callable[[], str],
    exists: Callable[[str], bool],
    retag: Callable[[str], None],
    attempts: int = ATTEMPTS,
) -> str | None:
    """Retags `main` to the head commit's image; returns that commit, or None if it has no image yet.

    The head is read again after retagging: if main moved meanwhile, the newer head is handled too.
    """
    for _ in range(attempts):
        sha = head()
        if not exists(sha):
            return None
        retag(sha)
        if head() == sha:
            return sha
    raise RuntimeError(f"main kept moving; gave up after {attempts} attempts")


def short(sha: str) -> str:
    """The tag suffix: the first 7 characters of the full id, as main-image.yml derives it."""
    return sha[:7]


# How a registry reports a tag that was never pushed; anything else (rate limits, authentication,
# network) is an error, not a missing image.
NOT_FOUND = ("not found", "manifest unknown")


def image_exists(inspected: subprocess.CompletedProcess[str]) -> bool:
    """Reads the result of `docker buildx imagetools inspect`: True if the image exists, False if the
    registry says the tag does not; raises on any other failure."""
    if inspected.returncode == 0:
        return True
    message = (inspected.stderr or "").strip()
    if any(phrase in message.lower() for phrase in NOT_FOUND):
        return False
    raise RuntimeError(f"Could not inspect the image (exit {inspected.returncode}): {message}")


def main(argv: list[str]) -> int:
    image, repository = argv

    def head() -> str:
        return subprocess.run(
            ["gh", "api", f"repos/{repository}/commits/main", "--jq", ".sha"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def exists(sha: str) -> bool:
        return image_exists(
            subprocess.run(
                ["docker", "buildx", "imagetools", "inspect", f"{image}:main-{short(sha)}"],
                capture_output=True,
                text=True,
            )
        )

    def retag(sha: str) -> None:
        subprocess.run(
            [
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                f"{image}:main",
                f"{image}:main-{short(sha)}",
            ],
            check=True,
        )

    promoted = promote(head, exists, retag)
    if promoted:
        print(f"{image}:main now points at main-{short(promoted)}")
    else:
        print("The head of main has no image yet; its own build will move the main tag")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
