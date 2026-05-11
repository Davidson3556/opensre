"""GitHub Copilot CLI adapter (``copilot -p``, non-interactive / programmatic mode).

Env vars
--------
COPILOT_BIN     Optional explicit path to the ``copilot`` binary.
                Blank or non-runnable paths are ignored; PATH + fallbacks apply.
COPILOT_MODEL   Optional model override. Unset or empty → omit ``--model``;
                the CLI default applies.
COPILOT_HOME    Optional config directory override. Defaults to ``~/.copilot``.

Auth probe
----------
Copilot CLI does **not** expose a non-interactive auth-status subcommand.
The documented commands (per
https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli)
are ``copilot login`` (opens an OAuth device flow — would hang ``detect()``)
and the in-session slash commands ``/login``, ``/user``, ``/logout``. None
return a scriptable status string. Per the same docs, credentials live in:

* the system credential store (``copilot-cli`` service) when one is
  available — macOS Keychain, Linux libsecret, or Windows Credential
  Manager; or
* the plaintext fallback ``$COPILOT_HOME/config.json`` when no system
  store is reachable; or
* the ``gh`` CLI's stored token, which Copilot CLI delegates to when no
  Copilot-specific login exists.

Probe order — **CLI auth state first, env tokens last**. The default first
flow for a Copilot user is ``copilot login`` (or ``gh auth login``); env
tokens are the documented bypass for automation. Per reviewer guidance on
PR #1533, the probe reflects that intent:

1. Platform credential store entry (macOS Keychain via ``security``, Linux
   libsecret via ``secret-tool``) under one of the documented service names
   → ``True``. Silent metadata lookup — no TouchID / unlock prompt; the
   secret value itself is never read into this Python process.
2. ``$COPILOT_HOME/config.json`` exists and parses as a non-empty JSON
   object → ``True``. We validate the *content*, not just the file's
   existence, so leftover / empty / junk files do not cause a false
   positive.
3. ``gh auth token`` exits 0 → ``True``. Copilot CLI delegates to ``gh``'s
   token when no Copilot-specific login exists, so a logged-in ``gh`` is
   sufficient for ``copilot -p``. Stdout is discarded so the token value
   never enters this process. Mirrors the Hermes Direct API fallback.
4. ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN`` env var set
   → ``True`` (documented automation bypass).
5. Otherwise → ``None`` (auth state cannot be verified). Windows
   Credential Manager has no reliable silent probe wired up; on those
   systems we defer to invocation: ``CLIBackedLLMClient`` runs Copilot
   anyway, and ``explain_failure`` surfaces ``_AUTH_HINT`` when stderr
   matches an auth pattern. The wizard offers retry / repick.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from app.integrations.llm_cli.base import CLIInvocation, CLIProbe
from app.integrations.llm_cli.binary_resolver import (
    candidate_binary_names as _candidate_binary_names,
)
from app.integrations.llm_cli.binary_resolver import (
    default_cli_fallback_paths as _default_cli_fallback_paths,
)
from app.integrations.llm_cli.binary_resolver import (
    resolve_cli_binary,
)
from app.integrations.llm_cli.env_overrides import (
    COPILOT_CLI_CONFIG_ENV_KEYS,
    COPILOT_CLI_ENV_KEYS,
    nonempty_env_values,
)

_COPILOT_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_PROBE_TIMEOUT_SEC = 5.0
_KEYCHAIN_PROBE_TIMEOUT_SEC = 2.0
_GH_PROBE_TIMEOUT_SEC = 2.0
# Service names the system credential-store probes accept as a positive auth signal:
# - ``copilot-cli`` is what the GitHub docs name for a copilot-specific login;
# - ``github-copilot-cli`` / ``gh-copilot`` are historical variants;
# - ``gh:github.com`` is the entry written by the standalone ``gh`` CLI.
#   The Copilot CLI delegates to ``gh``'s stored token when no copilot-specific
#   login exists, so a logged-in ``gh`` is sufficient for ``copilot -p`` to work.
_KEYCHAIN_SERVICES: tuple[str, ...] = (
    "copilot-cli",
    "github-copilot-cli",
    "gh-copilot",
    "gh:github.com",
)
_AUTH_HINT = (
    "Run `copilot login` (or `gh auth login`), "
    "or set COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN."
)


def _parse_semver(text: str) -> str | None:
    m = _COPILOT_VERSION_RE.search(text)
    return m.group(1) if m else None


def _copilot_home() -> Path:
    override = os.environ.get("COPILOT_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".copilot"


def _config_json_has_credentials() -> bool:
    """Return True only when the plaintext credential fallback is real and populated.

    Per the Copilot CLI docs, ``$COPILOT_HOME/config.json`` is the plaintext
    fallback used when no system keychain is available. It is a JSON object;
    an empty file, an empty object, or unreadable bytes do **not** count as
    being logged in. This is stricter than the previous "any file in the
    directory" heuristic, which the reviewer flagged as a false-positive
    risk for leftover/junk files.
    """
    path = _copilot_home() / "config.json"
    try:
        if not path.is_file() or path.stat().st_size <= 2:
            return False
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(data, dict) and bool(data)


def _has_token_env() -> str | None:
    """Return the first set token env var name, if any."""
    for key in COPILOT_CLI_ENV_KEYS:
        if os.environ.get(key, "").strip():
            return key
    return None


def _macos_keychain_has_copilot_entry() -> str | None:
    """Return the matching service name iff macOS Keychain has a Copilot entry.

    ``security find-generic-password -s <service>`` (no ``-w``/``-g``) queries
    entry *metadata* only and does not require Keychain unlock or TouchID. The
    process returns 0 when an entry exists, 44 when it is missing. We try the
    documented service name and a couple of historical variants so a working
    ``/login`` is detected regardless of which CLI version wrote the entry.
    Returns the service name on the first hit, or ``None`` when no match.
    """
    if sys.platform != "darwin":
        return None
    for service in _KEYCHAIN_SERVICES:
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", service],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_KEYCHAIN_PROBE_TIMEOUT_SEC,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode == 0:
            return service
    return None


def _linux_libsecret_has_copilot_entry() -> str | None:
    """Return the matching service name iff Linux libsecret has a Copilot entry.

    ``secret-tool lookup service <name>`` exits 0 when an entry with that
    attribute exists and prints the secret on stdout. We discard stdout so
    the secret value never enters this process; only the exit code is read.
    If ``secret-tool`` is not installed, or no D-Bus session is available,
    the call fails fast and the probe falls through to the next signal.
    """
    if sys.platform != "linux":
        return None
    if shutil.which("secret-tool") is None:
        return None
    for service in _KEYCHAIN_SERVICES:
        try:
            proc = subprocess.run(
                ["secret-tool", "lookup", "service", service],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_KEYCHAIN_PROBE_TIMEOUT_SEC,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode == 0:
            return service
    return None


def _system_credential_store_has_copilot_entry() -> tuple[str, str] | None:
    """Return ``(store_label, service_name)`` if the platform store has a hit.

    Dispatches to the per-platform probe; ``None`` when the platform has no
    silent probe wired up (currently Windows Credential Manager — see the
    module docstring for the rationale).
    """
    if sys.platform == "darwin":
        service = _macos_keychain_has_copilot_entry()
        return ("macOS Keychain", service) if service else None
    if sys.platform == "linux":
        service = _linux_libsecret_has_copilot_entry()
        return ("Linux libsecret", service) if service else None
    return None


def _gh_cli_has_token() -> bool:
    """Return True iff ``gh auth token`` exits 0 (user is logged in via gh CLI).

    Copilot CLI accepts ``gh``'s stored token as a documented credential
    source, so a logged-in ``gh`` is sufficient for ``copilot -p``. Stdout
    is discarded so the token value itself never enters this process —
    only the exit code is consulted. Mirrors the Hermes Direct-API probe
    order (env tokens → ``gh auth token`` → OAuth) for parity.
    """
    if shutil.which("gh") is None:
        return False
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_GH_PROBE_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _classify_copilot_auth() -> tuple[bool | None, str]:
    """Resolve auth state without spawning the Copilot CLI itself.

    Probe order — **CLI auth first, env tokens last** (per PR #1533 review
    guidance: the canonical Copilot flow is ``copilot login`` / ``gh auth
    login``; env tokens are the documented automation bypass):

      1. Platform credential store (macOS Keychain, Linux libsecret) under
         one of the documented service names. Silent metadata lookup; the
         secret value is never read.
      2. ``$COPILOT_HOME/config.json`` plaintext credential file — used
         when no system store is available (some CI / Linux setups).
         Validated as a non-empty JSON object, not just file existence.
      3. ``gh auth token`` exits 0 — the cross-platform CLI signal. Works
         on Windows / Linux where the silent credential-store probe may
         miss but the user is logged into ``gh``.
      4. Token env var (``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` /
         ``GITHUB_TOKEN``) — the CLI's documented automation bypass.
      5. Unknown — Windows Credential Manager has no silent probe wired
         up here; the runner verifies at invocation time and
         ``explain_failure`` surfaces the auth hint on real failure.
    """
    store_hit = _system_credential_store_has_copilot_entry()
    if store_hit is not None:
        store_label, service = store_hit
        return True, f"Authenticated via {store_label} (service '{service}')."
    if _config_json_has_credentials():
        return True, f"Authenticated via {_copilot_home() / 'config.json'}."
    if _gh_cli_has_token():
        return True, "Authenticated via `gh auth token` (gh CLI session)."
    token_key = _has_token_env()
    if token_key:
        return True, f"Authenticated via {token_key} (env var fallback)."
    return (
        None,
        "Could not verify Copilot CLI auth from the host (no introspectable "
        f"credential store on this platform). {_AUTH_HINT}",
    )


def _fallback_copilot_paths() -> list[str]:
    return _default_cli_fallback_paths("copilot")


class CopilotAdapter:
    """Non-interactive GitHub Copilot CLI (``copilot -p``, programmatic mode)."""

    name = "copilot"
    binary_env_key = "COPILOT_BIN"
    install_hint = "npm i -g @github/copilot"
    auth_hint = _AUTH_HINT.removesuffix(".")
    min_version: str | None = None
    default_exec_timeout_sec = 180.0

    def _resolve_binary(self) -> str | None:
        return resolve_cli_binary(
            explicit_env_key="COPILOT_BIN",
            binary_names=_candidate_binary_names("copilot"),
            fallback_paths=_fallback_copilot_paths,
        )

    def _probe_binary(self, binary_path: str) -> CLIProbe:
        try:
            ver_proc = subprocess.run(
                [binary_path, "--version"],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SEC,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CLIProbe(
                installed=False,
                version=None,
                logged_in=None,
                bin_path=None,
                detail=f"Could not run `{binary_path} --version`: {exc}",
            )

        if ver_proc.returncode != 0:
            err = (ver_proc.stderr or ver_proc.stdout or "").strip()
            return CLIProbe(
                installed=False,
                version=None,
                logged_in=None,
                bin_path=None,
                detail=f"`{binary_path} --version` failed: {err or 'unknown error'}",
            )

        version = _parse_semver(ver_proc.stdout + ver_proc.stderr)
        logged_in, auth_detail = _classify_copilot_auth()
        return CLIProbe(
            installed=True,
            version=version,
            logged_in=logged_in,
            bin_path=binary_path,
            detail=auth_detail,
        )

    def detect(self) -> CLIProbe:
        binary = self._resolve_binary()
        if not binary:
            return CLIProbe(
                installed=False,
                version=None,
                logged_in=None,
                bin_path=None,
                detail=(
                    "Copilot CLI not found on PATH or known install locations. "
                    f"Install with: {self.install_hint} or set COPILOT_BIN."
                ),
            )
        return self._probe_binary(binary)

    def build(
        self,
        *,
        prompt: str,
        model: str | None,
        workspace: str,
        reasoning_effort: str | None = None,
    ) -> CLIInvocation:
        # Copilot CLI does not expose a reasoning-effort knob; accept the param
        # for protocol parity and discard it (same shape as ClaudeCodeAdapter).
        del reasoning_effort
        binary = self._resolve_binary()
        if not binary:
            raise RuntimeError(
                f"Copilot CLI not found. {self.install_hint} "
                "or set COPILOT_BIN to the full binary path."
            )

        ws = (workspace or "").strip()
        cwd = str(Path(ws).expanduser()) if ws else os.getcwd()

        # Each flag is required for a non-interactive run; do not drop these
        # without checking `copilot --help`:
        #   -p PROMPT       enters one-shot mode (without it, copilot opens a TUI).
        #   --no-color      strips ANSI so stdout is parseable.
        #   --no-ask-user   disables the agent's `ask_user` tool, otherwise the
        #                   agent can pause waiting for input even with -p.
        #   --silent        emits only the agent response, not stats / banner.
        argv: list[str] = [
            binary,
            "-p",
            prompt,
            "--no-color",
            "--no-ask-user",
            "--silent",
        ]

        resolved_model = (model or "").strip()
        if resolved_model:
            argv.extend(["--model", resolved_model])

        # Forward Copilot's config + credential envs exclusively via this
        # invocation env. The global subprocess prefix allowlist deliberately
        # does NOT include ``COPILOT_`` (would leak ``COPILOT_GITHUB_TOKEN``,
        # a GitHub PAT, into every other CLI subprocess).
        env = {
            **nonempty_env_values(COPILOT_CLI_CONFIG_ENV_KEYS),
            **nonempty_env_values(COPILOT_CLI_ENV_KEYS),
        }
        return CLIInvocation(
            argv=tuple(argv),
            stdin=None,
            cwd=cwd,
            env=env or None,
            timeout_sec=self.default_exec_timeout_sec,
        )

    def parse(self, *, stdout: str, stderr: str, returncode: int) -> str:
        del stderr, returncode
        return (stdout or "").strip()

    def explain_failure(self, *, stdout: str, stderr: str, returncode: int) -> str:
        err = (stderr or "").strip()
        out = (stdout or "").strip()
        bits = [f"copilot -p exited with code {returncode}"]
        text = f"{err}\n{out}".lower()
        # Match only specific auth phrases so we never mask a real error that
        # happens to contain the substring "login" (e.g. "Your current login:
        # alice — Error: model 'X' not found in your plan").
        auth_markers = (
            "not logged in",
            "not authenticated",
            "no credentials",
            "please /login",
            "unauthorized",
            "401",
        )
        is_auth_error = any(marker in text for marker in auth_markers)
        if err:
            bits.append(err[:2000])
        elif out:
            bits.append(out[:2000])
        if is_auth_error:
            bits.append(_AUTH_HINT)
        return ". ".join(bits)
