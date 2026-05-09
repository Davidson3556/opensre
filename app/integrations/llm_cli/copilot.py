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
``copilot login`` opens an OAuth device flow (so we cannot run it during
``detect()`` without risking a hung browser launch); ``/login``, ``/user``,
``/logout`` are slash commands that only work inside an interactive
session. Per the official docs, credentials live in:

* the system keychain (``copilot-cli`` service) when one is available
  (macOS Keychain, Windows Credential Manager, Linux libsecret), or
* the plaintext fallback ``$COPILOT_HOME/config.json`` when no keychain
  is reachable.

We therefore classify auth in this order:

1. ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN`` env var set
   → ``True`` (the CLI accepts these as a documented fallback).
2. ``$COPILOT_HOME/config.json`` exists and parses as a non-empty JSON
   object → ``True``. We validate the *content*, not just the file's
   existence, so leftover/empty/junk files do not cause a false positive
   (Greptile review).
3. Otherwise → ``None`` (auth state cannot be verified from the host).
   This is the steady state for keychain-stored credentials: the
   credential is in the OS keychain and Python cannot read it without
   triggering a TouchID/password prompt. The runner will surface the
   auth hint if invocation fails; the wizard offers retry / repick.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
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
    COPILOT_CLI_ENV_KEYS,
    nonempty_env_values,
)

_COPILOT_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_PROBE_TIMEOUT_SEC = 5.0
_AUTH_HINT = "Run `copilot` then /login, or set COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN."


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


def _classify_copilot_auth() -> tuple[bool | None, str]:
    """Resolve auth state without spawning the CLI.

    Documented fallbacks (in order):
      1. Token env var (the CLI's own documented fallback).
      2. ``$COPILOT_HOME/config.json`` plaintext credential file.
      3. Unknown — keychain-stored credentials are not introspectable
         from Python without triggering an OS auth prompt.
    """
    token_key = _has_token_env()
    if token_key:
        return True, f"Authenticated via {token_key}."
    if _config_json_has_credentials():
        return True, f"Authenticated via {_copilot_home() / 'config.json'}."
    return (
        None,
        "Could not verify Copilot CLI auth from the host (keychain credentials "
        f"are not introspectable). {_AUTH_HINT}",
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

    def build(self, *, prompt: str, model: str | None, workspace: str) -> CLIInvocation:
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

        env = nonempty_env_values(COPILOT_CLI_ENV_KEYS)
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
