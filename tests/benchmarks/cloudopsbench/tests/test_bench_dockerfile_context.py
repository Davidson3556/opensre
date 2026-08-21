"""Pin the bench Dockerfile's COPY sources to paths that exist in the repo.

``Dockerfile.bench`` copies hand-listed source trees ("only what the bench
needs") rather than the whole context, so a package rename elsewhere in the
repo silently leaves a dangling ``COPY``. Nothing in PR CI builds this image —
the failure only surfaces post-merge in the benchmark-image workflow, which
turns ``main`` red. Precedent: the ``platform`` → ``infrastructure`` rename
(#5295) left ``COPY platform/`` behind.

Sources are resolved against the *tracked* tree, not the working copy: a
rename leaves untracked ``__pycache__`` behind under the old name, so an
``exists()`` check on disk still passes locally while the build fails in CI
(the dockerignore drops ``__pycache__``, leaving nothing to copy).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DOCKERFILE = _REPO_ROOT / "tests" / "benchmarks" / "cloudopsbench" / "infra" / "Dockerfile.bench"


def _parent_dir(path: str) -> str:
    """Return the parent directory of ``path``, or ``""`` at the tree root."""
    head, sep, _ = path.rpartition("/")
    return head if sep else ""


def _tracked_paths() -> frozenset[str]:
    """Return every tracked file path plus the directories that contain them."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("git ls-files unavailable")

    paths: set[str] = set()
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        paths.add(entry)
        parent = _parent_dir(entry)
        while parent:
            paths.add(parent)
            parent = _parent_dir(parent)
    return frozenset(paths)


def _context_relative_sources() -> list[tuple[int, str]]:
    """Return ``(lineno, source)`` for COPY sources resolved against the build context.

    ``--from=`` copies read from another stage or image, not the build context,
    so they are skipped. The final token of a COPY is the destination.
    """
    sources: list[tuple[int, str]] = []
    for lineno, raw in enumerate(_DOCKERFILE.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line.startswith("COPY "):
            continue
        tokens = line.split()[1:]
        if any(token.startswith("--from=") for token in tokens):
            continue
        operands = [token for token in tokens if not token.startswith("--")]
        sources.extend((lineno, source) for source in operands[:-1])
    return sources


def test_bench_dockerfile_copies_only_tracked_paths() -> None:
    # The benchmark-image workflow builds with ``context: .`` (the repo root).
    tracked = _tracked_paths()
    missing = [
        f"{_DOCKERFILE.name}:{lineno}: COPY {source} -> not tracked in the repo"
        for lineno, source in _context_relative_sources()
        if source.rstrip("/") not in tracked
    ]

    assert missing == [], (
        "Dockerfile.bench copies paths that no longer exist. The bench image "
        "build fails post-merge on main, not in PR CI:\n" + "\n".join(missing)
    )
