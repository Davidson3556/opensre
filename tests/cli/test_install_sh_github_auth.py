"""GitHub API authentication in install.sh: release metadata lookups only."""

from __future__ import annotations

import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Same Windows-skip rationale as ``test_install_sh_resolution.py`` — install.sh
# is POSIX-only and the Windows runner has no usable bash. See issue #1099.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="install.sh is POSIX-only; the Windows runner has no usable bash. See issue #1099.",
)

INSTALL_SH = Path(__file__).parents[2] / "install.sh"


def _fake_curl(tmp_path: Path) -> Path:
    """A curl stand-in that records its argv and the config it reads on stdin."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf '%s\\n' "$@" > {shlex.quote(str(tmp_path))}/argv.txt
            cat > {shlex.quote(str(tmp_path))}/stdin.txt
            printf '{{}}\\n'
            """
        ),
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return bin_dir


def _run_download_text(tmp_path: Path, *, token: str) -> subprocess.CompletedProcess[str]:
    bin_dir = _fake_curl(tmp_path)
    script = textwrap.dedent(f"""\
        __fn=$(awk '/^download_text\\(\\)/{{p=1}} p{{print}} p&&/^}}$/{{exit}}' {shlex.quote(str(INSTALL_SH))})
        if [ -z "$__fn" ]; then
            echo "download_text not found in install.sh" >&2
            exit 1
        fi
        eval "$__fn"
        CURL_FLAGS=(--fail --silent)
        GITHUB_API_TOKEN={shlex.quote(token)}
        PATH={shlex.quote(str(bin_dir))}:$PATH
        download_text https://api.github.com/repos/o/r/releases/latest
    """)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_token_reaches_curl_without_appearing_in_argv(tmp_path: Path) -> None:
    # A token on the command line is readable by any local `ps`, so it has to
    # arrive through the config stdin instead.
    result = _run_download_text(tmp_path, token="secret-token-value")

    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "argv.txt").read_text(encoding="utf-8")
    config = (tmp_path / "stdin.txt").read_text(encoding="utf-8")
    assert "secret-token-value" not in argv
    assert "Authorization: Bearer secret-token-value" in config


def test_no_authorization_header_without_a_token(tmp_path: Path) -> None:
    result = _run_download_text(tmp_path, token="")

    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "argv.txt").read_text(encoding="utf-8")
    assert "Authorization" not in argv
    assert not (tmp_path / "stdin.txt").read_text(encoding="utf-8").strip()


def _run_hint(*, token: str) -> subprocess.CompletedProcess[str]:
    script = textwrap.dedent(f"""\
        __fn=$(awk '/^github_api_failure_hint\\(\\)/{{p=1}} p{{print}} p&&/^}}$/{{exit}}' \
{shlex.quote(str(INSTALL_SH))})
        eval "$__fn"
        GITHUB_API_TOKEN={shlex.quote(token)}
        github_api_failure_hint
    """)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_rate_limit_hint_is_offered_only_when_anonymous() -> None:
    # 403 here is almost always the anonymous 60/hour limit rather than a
    # missing release, and the curl error alone does not say so.
    anonymous = _run_hint(token="")
    authenticated = _run_hint(token="a-token")

    assert "60 requests per hour" in anonymous.stdout
    assert "GITHUB_TOKEN" in anonymous.stdout
    assert authenticated.stdout.strip() == ""
