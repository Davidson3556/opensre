"""Tests for the GitHub Copilot CLI adapter (non-interactive ``copilot -p``)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.llm_cli.copilot import CopilotAdapter
from app.integrations.llm_cli.runner import CLIBackedLLMClient
from tests.integrations.llm_cli.testing_helpers import write_fake_runnable_cli_bin


def _version_proc() -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    m.stdout = "copilot 1.4.2\n"
    m.stderr = ""
    return m


def _clean_copilot_env(monkeypatch: pytest.MonkeyPatch, *, home: Path | None = None) -> None:
    for key in (
        "COPILOT_BIN",
        "COPILOT_MODEL",
        "COPILOT_HOME",
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    if home is not None:
        monkeypatch.setenv("COPILOT_HOME", str(home))


def _run_with_keychain_missing(args: list[str], **_kwargs: object) -> MagicMock:
    """side_effect: copilot --version OK, every credential-store probe a miss.

    Used by tests that exercise the fall-through branches (config.json, gh,
    token env, none-of-the-above). The probes mocked here are:

    * ``security`` — macOS Keychain miss (exit 44, matching real ``security``)
    * ``secret-tool`` — Linux libsecret miss (exit 1)
    * ``gh auth token`` — gh CLI miss (exit 1, "not logged in")

    Keeping these in one helper means a future probe addition fails noisily
    in tests that haven't accounted for it (via the AssertionError fallback
    in tests that use stricter side_effect mocks).
    """
    if args and args[0] == "security":
        return MagicMock(returncode=44, stdout="", stderr="not found")
    if args and args[0] == "secret-tool":
        return MagicMock(returncode=1, stdout="", stderr="not found")
    if args and args[0] == "gh":
        return MagicMock(returncode=1, stdout="", stderr="not logged in")
    return _version_proc()


# Note on host-PATH independence: the adapter calls ``shutil.which`` before
# spawning ``gh`` / ``secret-tool``, and we deliberately do NOT monkey-patch
# the shared ``shutil`` module here (doing so leaks across tests because the
# revert path doesn't always replace the original ``shutil.which`` cleanly).
# Instead, both possibilities are covered by ``_run_with_keychain_missing``
# returning non-zero for ``gh`` and ``secret-tool``: if a probe runs because
# the binary is on PATH, the mock returns "not logged in"; if the binary is
# absent, the probe returns False before subprocess.run is invoked.


@patch("app.integrations.llm_cli.copilot.subprocess.run", side_effect=_run_with_keychain_missing)
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_with_config_json_is_logged_in(
    mock_which: MagicMock,
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A populated $COPILOT_HOME/config.json is a positive auth signal."""
    del mock_run
    mock_which.return_value = "/usr/bin/copilot"

    home = tmp_path / "copilot_home"
    home.mkdir()
    # Plaintext fallback per the Copilot CLI docs — must be a real JSON object.
    (home / "config.json").write_text('{"github_token": "ghu_realtoken"}')

    _clean_copilot_env(monkeypatch, home=home)
    probe = CopilotAdapter().detect()

    assert probe.installed is True
    assert probe.logged_in is True
    assert probe.bin_path == "/usr/bin/copilot"
    assert probe.version == "1.4.2"
    assert "config.json" in probe.detail


@patch("app.integrations.llm_cli.copilot.subprocess.run", side_effect=_run_with_keychain_missing)
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_with_empty_config_json_is_not_logged_in(
    mock_which: MagicMock,
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Greptile review: an empty / leftover config.json must NOT be a false positive."""
    del mock_run
    mock_which.return_value = "/usr/bin/copilot"

    home = tmp_path / "copilot_home"
    home.mkdir()
    # Empty object — Copilot has not been logged in.
    (home / "config.json").write_text("{}")

    _clean_copilot_env(monkeypatch, home=home)
    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is None
    assert "Could not verify" in probe.detail


@patch("app.integrations.llm_cli.copilot.subprocess.run", side_effect=_run_with_keychain_missing)
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_with_unrelated_files_is_not_logged_in(
    mock_which: MagicMock,
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Greptile review: junk files in COPILOT_HOME must not yield logged_in=True.

    Previous heuristic returned True if ANY file existed in the dir; this is a
    regression test for the false-positive case the reviewer flagged.
    """
    del mock_run
    mock_which.return_value = "/usr/bin/copilot"

    home = tmp_path / "copilot_home"
    home.mkdir()
    (home / "telemetry.log").write_text("noise")
    (home / "cache.bin").write_bytes(b"\x00\x01")

    _clean_copilot_env(monkeypatch, home=home)
    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is None
    assert "Could not verify" in probe.detail


@patch("app.integrations.llm_cli.copilot.subprocess.run", side_effect=_run_with_keychain_missing)
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_with_invalid_json_config_is_not_logged_in(
    mock_which: MagicMock,
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A corrupt config.json must not be treated as authenticated."""
    del mock_run
    mock_which.return_value = "/usr/bin/copilot"

    home = tmp_path / "copilot_home"
    home.mkdir()
    (home / "config.json").write_text("not-json{")

    _clean_copilot_env(monkeypatch, home=home)
    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is None


@patch("app.integrations.llm_cli.copilot.sys")
@patch("app.integrations.llm_cli.copilot.subprocess.run")
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_macos_keychain_entry_is_logged_in(
    mock_which: MagicMock,
    mock_run: MagicMock,
    mock_sys: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """macOS Keychain entry for service `copilot-cli` is a positive auth signal.

    The probe is `security find-generic-password -s copilot-cli` (no `-w`),
    which returns 0 when the entry exists without triggering TouchID.
    """
    mock_sys.platform = "darwin"
    mock_which.return_value = "/usr/bin/copilot"

    keychain_proc = MagicMock(returncode=0, stdout="keychain entry...", stderr="")

    def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
        if len(args) >= 2 and args[1] == "--version":
            return _version_proc()
        if args[0] == "security" and "find-generic-password" in args:
            return keychain_proc
        raise AssertionError(f"unexpected: {args}")

    mock_run.side_effect = side_effect

    empty_home = tmp_path / "empty_copilot_home"
    _clean_copilot_env(monkeypatch, home=empty_home)

    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is True
    assert "macOS Keychain" in probe.detail


@patch("app.integrations.llm_cli.copilot.sys")
@patch("app.integrations.llm_cli.copilot.subprocess.run")
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_macos_keychain_missing_falls_through(
    mock_which: MagicMock,
    mock_run: MagicMock,
    mock_sys: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the keychain entry is absent and no other signal fires, classify as None."""
    mock_sys.platform = "darwin"
    mock_which.return_value = "/usr/bin/copilot"

    missing_proc = MagicMock(returncode=44, stdout="", stderr="not found")

    def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
        if len(args) >= 2 and args[1] == "--version":
            return _version_proc()
        if args[0] == "security":
            return missing_proc
        if args[0] == "gh" and args[1:] == ["auth", "token"]:
            # `gh` may be on the test host's PATH; force a miss so the test
            # exercises the "no signal" branch deterministically.
            return MagicMock(returncode=1, stdout="", stderr="not logged in")
        raise AssertionError(f"unexpected: {args}")

    mock_run.side_effect = side_effect

    empty_home = tmp_path / "empty_copilot_home"
    _clean_copilot_env(monkeypatch, home=empty_home)

    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is None


@patch("app.integrations.llm_cli.copilot.sys")
@patch("app.integrations.llm_cli.copilot.subprocess.run")
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_skips_keychain_probe_on_non_darwin(
    mock_which: MagicMock,
    mock_run: MagicMock,
    mock_sys: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On Linux: macOS `security` is never spawned; libsecret/gh probes miss → None."""
    mock_sys.platform = "linux"
    mock_which.return_value = "/usr/bin/copilot"

    def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
        if len(args) >= 2 and args[1] == "--version":
            return _version_proc()
        if args[0] == "secret-tool":
            # If host has libsecret installed and shutil.which returns a path,
            # the probe spawns secret-tool; force a miss so the platform
            # fall-through branch is exercised deterministically.
            return MagicMock(returncode=1, stdout="", stderr="not found")
        if args[0] == "gh" and args[1:] == ["auth", "token"]:
            return MagicMock(returncode=1, stdout="", stderr="not logged in")
        raise AssertionError(f"unexpected: {args}")

    mock_run.side_effect = side_effect

    empty_home = tmp_path / "empty_copilot_home"
    _clean_copilot_env(monkeypatch, home=empty_home)

    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is None
    # `security` must not have been spawned on Linux.
    for call in mock_run.call_args_list:
        argv = call.args[0]
        assert argv[0] != "security"


@patch("app.integrations.llm_cli.copilot.sys")
@patch("app.integrations.llm_cli.copilot.subprocess.run")
def test_detect_linux_libsecret_entry_is_logged_in(
    mock_run: MagicMock,
    mock_sys: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Linux libsecret entry under `service copilot-cli` is a positive auth signal.

    Probes via `secret-tool lookup service <name>` with stdout discarded so
    the secret never enters the test process.
    """
    mock_sys.platform = "linux"

    import app.integrations.llm_cli.copilot as copilot_mod

    # `shutil.which` is shared across modules; patch it in one place.
    def fake_which(cmd: str, *a: object, **kw: object) -> str | None:
        if cmd in ("copilot", "copilot.cmd"):
            return "/usr/bin/copilot"
        if cmd == "secret-tool":
            return "/usr/bin/secret-tool"
        return None

    monkeypatch.setattr(copilot_mod.shutil, "which", fake_which)

    libsecret_hit = MagicMock(returncode=0, stdout="", stderr="")

    def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
        if len(args) >= 2 and args[1] == "--version":
            return _version_proc()
        if args[0] == "secret-tool":
            return libsecret_hit
        raise AssertionError(f"unexpected: {args}")

    mock_run.side_effect = side_effect

    empty_home = tmp_path / "empty_copilot_home"
    _clean_copilot_env(monkeypatch, home=empty_home)

    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is True
    assert "libsecret" in probe.detail


@patch("app.integrations.llm_cli.copilot.subprocess.run")
def test_detect_gh_auth_token_is_logged_in_fallback(
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`gh auth token` exit 0 is a positive auth signal (Hermes-parity fallback).

    Stdout is discarded by the adapter, so we assert returncode-only behavior.
    """
    import app.integrations.llm_cli.copilot as copilot_mod

    # `shutil.which` is shared across modules — patch in one place. The resolver
    # checks copilot binary names first, then the adapter checks `gh` and
    # `secret-tool`. Returning a real-looking path for `copilot` and `gh` only
    # is the minimum surface needed.
    def fake_which(cmd: str, *a: object, **kw: object) -> str | None:
        if cmd in ("copilot", "copilot.cmd"):
            return "/usr/bin/copilot"
        if cmd == "gh":
            return "/usr/bin/gh"
        return None

    monkeypatch.setattr(copilot_mod.shutil, "which", fake_which)

    def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
        if len(args) >= 2 and args[1] == "--version":
            return _version_proc()
        if args[0] == "security":
            return MagicMock(returncode=44, stdout="", stderr="not found")
        if args[0] == "gh" and args[1:] == ["auth", "token"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected: {args}")

    mock_run.side_effect = side_effect

    empty_home = tmp_path / "empty_copilot_home"
    _clean_copilot_env(monkeypatch, home=empty_home)

    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is True
    assert "gh auth token" in probe.detail


@patch("app.integrations.llm_cli.copilot.sys")
@patch("app.integrations.llm_cli.copilot.subprocess.run")
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_prefers_keychain_over_token_env(
    mock_which: MagicMock,
    mock_run: MagicMock,
    mock_sys: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reviewer (PR #1533): CLI auth state must win over env tokens.

    When BOTH a keychain entry and a token env var are present, the probe
    must report the CLI flow (canonical) — not the env-var bypass.
    """
    mock_sys.platform = "darwin"
    mock_which.return_value = "/usr/bin/copilot"

    keychain_hit = MagicMock(returncode=0, stdout="", stderr="")

    def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
        if len(args) >= 2 and args[1] == "--version":
            return _version_proc()
        if args[0] == "security":
            return keychain_hit
        raise AssertionError(f"unexpected: {args}")

    mock_run.side_effect = side_effect

    empty_home = tmp_path / "empty_copilot_home"
    _clean_copilot_env(monkeypatch, home=empty_home)
    # Both signals present — keychain must win.
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_should_not_win")

    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is True
    assert "Keychain" in probe.detail
    assert "COPILOT_GITHUB_TOKEN" not in probe.detail


@patch("app.integrations.llm_cli.copilot.subprocess.run", side_effect=_run_with_keychain_missing)
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_with_token_env_is_logged_in_fallback(
    mock_which: MagicMock,
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When no stored credentials exist, a token env counts as authenticated."""
    del mock_run
    mock_which.return_value = "/usr/bin/copilot"

    empty_home = tmp_path / "empty_copilot_home"
    _clean_copilot_env(monkeypatch, home=empty_home)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_test")

    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is True
    assert "COPILOT_GITHUB_TOKEN" in probe.detail
    # Reviewer (PR #1533): tokens are the documented automation bypass,
    # so the detail must label them as such (the new probe order tests
    # for ordering are covered separately).
    assert "env var fallback" in probe.detail


@patch("app.integrations.llm_cli.copilot.subprocess.run", side_effect=_run_with_keychain_missing)
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_no_creds_no_token_is_unclear(
    mock_which: MagicMock,
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without stored credentials or token env, auth state is unclear (None)."""
    del mock_run
    mock_which.return_value = "/usr/bin/copilot"

    empty_home = tmp_path / "empty_copilot_home"
    _clean_copilot_env(monkeypatch, home=empty_home)

    probe = CopilotAdapter().detect()
    assert probe.installed is True
    assert probe.logged_in is None
    assert "Could not verify" in probe.detail


@patch("app.integrations.llm_cli.copilot.subprocess.run")
@patch("app.integrations.llm_cli.binary_resolver.shutil.which", return_value=None)
def test_detect_binary_not_found(
    mock_which: MagicMock,
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_copilot_env(monkeypatch)
    monkeypatch.setattr(
        "app.integrations.llm_cli.copilot._fallback_copilot_paths",
        lambda: [],
    )
    probe = CopilotAdapter().detect()
    assert probe.installed is False
    assert probe.bin_path is None
    assert "Copilot CLI not found" in probe.detail
    mock_which.assert_called()
    mock_run.assert_not_called()


@patch("app.integrations.llm_cli.copilot.subprocess.run")
@patch("app.integrations.llm_cli.binary_resolver.shutil.which")
def test_detect_version_failure_marks_not_installed(
    mock_which: MagicMock,
    mock_run: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_which.return_value = "/usr/bin/copilot"
    failed = MagicMock()
    failed.returncode = 1
    failed.stdout = ""
    failed.stderr = "boom"
    mock_run.return_value = failed
    _clean_copilot_env(monkeypatch)
    probe = CopilotAdapter().detect()
    assert probe.installed is False
    assert probe.logged_in is None
    assert "boom" in probe.detail


@patch("app.integrations.llm_cli.binary_resolver.shutil.which", return_value="/usr/bin/copilot")
def test_build_argv_uses_non_interactive_flags(
    mock_which: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_copilot_env(monkeypatch)
    inv = CopilotAdapter().build(prompt="hello world", model=None, workspace="")

    assert inv.argv[0] == "/usr/bin/copilot"
    assert "-p" in inv.argv
    idx = inv.argv.index("-p")
    assert inv.argv[idx + 1] == "hello world"
    # Each flag is essential for a non-interactive run; see comment in build().
    assert "--no-color" in inv.argv
    assert "--no-ask-user" in inv.argv
    assert "--silent" in inv.argv
    assert inv.stdin is None
    assert inv.cwd  # not empty — runner cannot pass cwd="" to subprocess.run
    assert inv.env is None  # no token env set
    mock_which.assert_called()


@patch("app.integrations.llm_cli.binary_resolver.shutil.which", return_value="/usr/bin/copilot")
def test_build_uses_workspace_when_provided(
    _mock_which: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clean_copilot_env(monkeypatch)
    ws = tmp_path / "repo"
    ws.mkdir()
    inv = CopilotAdapter().build(prompt="p", model=None, workspace=str(ws))
    assert inv.cwd == str(ws)


@patch("app.integrations.llm_cli.binary_resolver.shutil.which", return_value="/usr/bin/copilot")
def test_build_adds_model_flag_when_provided(
    _mock_which: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_copilot_env(monkeypatch)
    inv = CopilotAdapter().build(prompt="p", model="claude-sonnet-4.6", workspace="")
    assert "--model" in inv.argv
    idx = inv.argv.index("--model")
    assert inv.argv[idx + 1] == "claude-sonnet-4.6"


@patch("app.integrations.llm_cli.binary_resolver.shutil.which", return_value="/usr/bin/copilot")
def test_build_forwards_token_env_keys(
    _mock_which: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_copilot_env(monkeypatch)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_a")
    monkeypatch.setenv("GH_TOKEN", "ghp_b")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_c")
    inv = CopilotAdapter().build(prompt="p", model=None, workspace="")
    assert inv.env is not None
    assert inv.env["COPILOT_GITHUB_TOKEN"] == "ghp_a"
    assert inv.env["GH_TOKEN"] == "ghp_b"
    assert inv.env["GITHUB_TOKEN"] == "ghp_c"


@patch("app.integrations.llm_cli.binary_resolver.shutil.which", return_value="/usr/bin/copilot")
def test_build_forwards_copilot_config_envs(
    _mock_which: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COPILOT_HOME / COPILOT_MODEL flow through the adapter's invocation env."""
    _clean_copilot_env(monkeypatch)
    monkeypatch.setenv("COPILOT_HOME", "/x/copilot")
    monkeypatch.setenv("COPILOT_MODEL", "gpt-5.2")
    inv = CopilotAdapter().build(prompt="p", model=None, workspace="")
    assert inv.env is not None
    assert inv.env["COPILOT_HOME"] == "/x/copilot"
    assert inv.env["COPILOT_MODEL"] == "gpt-5.2"


def test_build_raises_when_binary_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_copilot_env(monkeypatch)
    with (
        patch(
            "app.integrations.llm_cli.binary_resolver.shutil.which", return_value=None
        ) as mock_which,
        patch(
            "app.integrations.llm_cli.copilot._fallback_copilot_paths",
            return_value=[],
        ),
        pytest.raises(RuntimeError, match="Copilot CLI not found"),
    ):
        CopilotAdapter().build(prompt="p", model=None, workspace="")
    mock_which.assert_called()


def test_explicit_copilot_bin_used_when_runnable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clean_copilot_env(monkeypatch)
    bin_path = write_fake_runnable_cli_bin(tmp_path, "copilot")
    monkeypatch.setenv("COPILOT_BIN", str(bin_path))
    resolved = CopilotAdapter()._resolve_binary()
    assert resolved == str(bin_path)


def test_parse_strips_whitespace() -> None:
    adapter = CopilotAdapter()
    assert adapter.parse(stdout="  hello  \n", stderr="", returncode=0) == "hello"


def test_explain_failure_includes_auth_hint_on_unauthorized() -> None:
    adapter = CopilotAdapter()
    msg = adapter.explain_failure(
        stdout="",
        stderr="error: unauthorized — please /login",
        returncode=1,
    )
    assert "code 1" in msg
    # New hint cites `copilot login` (and `gh auth login`) rather than `/login`.
    assert "copilot login" in msg or "COPILOT_GITHUB_TOKEN" in msg
    # Original error is preserved so the user does not lose context.
    assert "unauthorized" in msg


def test_explain_failure_does_not_mask_unrelated_error_with_login_in_text() -> None:
    """Greptile P1 regression: the substring 'login' must not steal a real error.

    A model-not-found error that happens to print the user's GitHub login should
    surface verbatim, not be replaced with the auth hint.
    """
    adapter = CopilotAdapter()
    err = "Your current login: johndoe@github.com — Error: model 'gpt-5.2' not found in your plan"
    msg = adapter.explain_failure(stdout="", stderr=err, returncode=1)
    # Real error text reaches the user.
    assert "model 'gpt-5.2' not found" in msg
    # Auth hint is NOT appended for a non-auth failure.
    assert "COPILOT_GITHUB_TOKEN" not in msg
    assert "copilot login" not in msg


def test_explain_failure_truncates_long_output() -> None:
    adapter = CopilotAdapter()
    err = "x" * 5000
    msg = adapter.explain_failure(stdout="", stderr=err, returncode=2)
    assert "code 2" in msg
    assert "x" * 2000 in msg


@patch("app.integrations.llm_cli.runner.subprocess.run")
def test_cli_backed_client_invokes_copilot_and_forwards_token_env(
    mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runner merges adapter env (token vars) and forwards COPILOT_* via prefix allowlist."""
    _clean_copilot_env(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_runner")
    monkeypatch.setenv("COPILOT_HOME", "/custom/copilot")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")

    mock_adapter = MagicMock()
    mock_adapter.name = "copilot"
    mock_adapter.detect.return_value = MagicMock(
        installed=True,
        bin_path="/usr/bin/copilot",
        logged_in=True,
        detail="ok",
    )
    # Realistic invocation env — the real CopilotAdapter.build merges both
    # the config tuple (COPILOT_HOME / COPILOT_MODEL) and the credential tuple
    # (COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN). The mock mirrors that
    # so the test reflects the actual code path.
    mock_adapter.build.return_value = MagicMock(
        argv=["/usr/bin/copilot", "-p", "hi", "--silent"],
        stdin=None,
        cwd="/tmp",
        env={"COPILOT_GITHUB_TOKEN": "ghp_runner", "COPILOT_HOME": "/custom/copilot"},
        timeout_sec=30.0,
    )
    mock_adapter.parse.return_value = "answer"
    mock_adapter.explain_failure.return_value = "fail"

    mock_run.return_value = MagicMock(returncode=0, stdout="answer\n", stderr="")

    with patch("app.guardrails.engine.get_guardrail_engine") as gr:
        gr.return_value.is_active = False
        client = CLIBackedLLMClient(mock_adapter, model=None, max_tokens=256)
        resp = client.invoke("hello")

    assert resp.content == "answer"
    env = mock_run.call_args.kwargs["env"]
    # All Copilot envs reach the subprocess via CLIInvocation.env, NOT via the
    # global prefix allowlist (which deliberately excludes ``COPILOT_``).
    assert env["COPILOT_HOME"] == "/custom/copilot"
    assert env["COPILOT_GITHUB_TOKEN"] == "ghp_runner"
    # Other CLI auth must not leak into the Copilot subprocess env.
    assert "ANTHROPIC_API_KEY" not in env


def test_registry_resolves_copilot_provider() -> None:
    from app.integrations.llm_cli.registry import (
        CLI_PROVIDER_REGISTRY,
        get_cli_provider_registration,
    )

    reg = get_cli_provider_registration("copilot")
    assert reg is not None
    assert reg.model_env_key == "COPILOT_MODEL"
    assert "copilot" in CLI_PROVIDER_REGISTRY
    adapter = reg.adapter_factory()
    assert isinstance(adapter, CopilotAdapter)


def test_subprocess_env_does_not_leak_copilot_token_via_prefix_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Greptile P1 SECURITY regression: ``COPILOT_*`` must NOT be a prefix entry.

    A blanket ``COPILOT_`` prefix in ``_SAFE_SUBPROCESS_ENV_PREFIXES`` would
    forward ``COPILOT_GITHUB_TOKEN`` (a GitHub PAT) into every other CLI
    subprocess (Codex, Kimi, Claude Code, etc.). All Copilot envs must reach
    the Copilot subprocess via ``CLIInvocation.env``, never via the global
    prefix allowlist.
    """
    from app.integrations.llm_cli.subprocess_env import build_cli_subprocess_env

    monkeypatch.setenv("COPILOT_HOME", "/x/copilot")
    monkeypatch.setenv("COPILOT_MODEL", "gpt-5.2")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_super_secret")
    monkeypatch.setenv("COPILOT_BIN", "/usr/local/bin/copilot")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak")

    # Empty overrides simulates a non-Copilot adapter (e.g. Codex) running.
    env = build_cli_subprocess_env(None)

    # No COPILOT_* may flow into a generic CLI subprocess env.
    assert "COPILOT_GITHUB_TOKEN" not in env
    assert "COPILOT_HOME" not in env
    assert "COPILOT_MODEL" not in env
    assert "COPILOT_BIN" not in env
    # Cross-provider credentials still don't leak either.
    assert "ANTHROPIC_API_KEY" not in env
    # Sanity: PATH always forwarded for binary resolution.
    assert "PATH" in env or os.environ.get("PATH") is None
