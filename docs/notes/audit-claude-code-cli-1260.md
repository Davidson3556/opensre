# Audit: Claude Code CLI — subscription login vs API key, cross-OS parity

Tracking: [#1260](https://github.com/Tracer-Cloud/opensre/issues/1260) — follow-up
to [#1199 (comment)](https://github.com/Tracer-Cloud/opensre/issues/1199) and the
[#1217](https://github.com/Tracer-Cloud/opensre/pull/1217) onboarding fix.

## TL;DR

- **#1217 fully addresses the `#1199` false-negative path.** When the binary is
  on disk, `claude auth status` is the source of truth on every platform; macOS
  Keychain auth no longer reads as "uncertain" during onboarding.
- **Subscription is architecturally first-class** (live probe, JSON parsing,
  wizard handles `True/False/None`) and **first-class in user-facing copy**
  (`auth_hint`, probe detail strings, `.env.example`, `docs/llm-providers.mdx`).
  One stale internal docstring put `ANTHROPIC_API_KEY` first; fixed in this
  branch.
- **Cross-OS coverage is complete in code** (binary names, fallback dirs,
  subprocess env keys, no-binary fallback ordering). One **test gap**
  (Windows-no-binary case) — added in this branch.
- **Two real bugs found while verifying the live CLI** — both fixed in this
  branch:
  1. ``claude auth status`` exits **1** when ``loggedIn`` is false. The probe
     short-circuited on ``returncode != 0`` and returned ``None`` before
     parsing JSON, so the wizard showed "Could not verify login" instead of
     "requires login" — users had no clear signal that they needed to run
     ``claude login``. **User-visible.**
  2. The probe used ``apiKeySource`` as the primary discriminator between
     subscription and API-key auth. Verified on CLI 2.1.123 that
     ``apiKeySource`` is reported even when the active auth method is the
     subscription (whenever ``ANTHROPIC_API_KEY`` is also in the env). The
     authoritative discriminator is ``authMethod`` (``claude.ai`` /
     ``api_key`` / ``none``). Detail string was previously misleading for
     subscription-with-env-key users.
- Optional follow-ups listed below; none of them block closing this issue.

## 1. Goal 1 — confirm #1217 fully addresses #1199

`#1199` reported that on macOS, when OAuth lives in the Keychain (no
`~/.claude/.credentials.json` on disk), the wizard treated the unknown auth
state as a hard failure with no recovery path.

`#1217` fixed this by:

1. Adding `_probe_cli_auth()` in [claude_code.py:47-83](../../app/integrations/llm_cli/claude_code.py#L47-L83)
   that runs `claude auth status` and parses the JSON. The `loggedIn` boolean
   is the source of truth for the wizard verdict — definitive `True`/`False`
   when the probe succeeds, `None` only when the probe itself fails (timeout,
   OS error, non-zero exit, malformed JSON). (The detail-string parser also
   reads `apiKeySource` and `email`; see the latent-bug note in §2 — `loggedIn`
   parsing is correct, the auxiliary fields are partly stale.)
2. Routing `_classify_claude_code_auth()` through `_probe_cli_auth()` whenever
   a binary is resolved ([claude_code.py:97-98](../../app/integrations/llm_cli/claude_code.py#L97-L98)).
   The Keychain question never has to be answered by inspecting the filesystem
   — the CLI itself reports auth state.
3. Wiring the wizard to handle all three states distinctly
   ([flow.py:1654-1682](../../app/cli/wizard/flow.py#L1654-L1682)):
   - `installed && logged_in is True` → success
   - `installed && logged_in is False` → "requires login" + retry/repick
   - `installed && logged_in is None` → "could not verify" + retry/repick
   The previous behaviour (treat `None` as failure) is gone.

**Verdict:** ✅ The original bug class is closed. The wizard no longer
short-circuits on uncertain Keychain state, and the live probe gives a
definitive answer in the common case (binary present + reachable).

**Edge cases that still surface as `None` (by design):**

- `claude auth status` exits non-zero (e.g. an older CLI without the `auth`
  subcommand, or a partly-installed binary). The wizard prompts retry.
- `claude auth status` exceeds the 8 s probe timeout
  ([claude_code.py:39](../../app/integrations/llm_cli/claude_code.py#L39)). The
  wizard prompts retry.
- No binary on PATH/fallback locations on macOS, no env var, no creds file.
  Returns `None` because the Keychain may still hold creds; the wizard tells
  the user to install or run `claude login`.

These are all intentional and recoverable through the wizard's retry/repick
menu — none of them re-introduce the original false-negative.

## 2. Goal 2 — auth ordering and messaging

The intent is "subscription/CLI login is first-class; API key is an explicit
fallback." Audit of every user-visible string:

| Surface | String | Treatment |
| --- | --- | --- |
| `auth_hint` ([claude_code.py:128](../../app/integrations/llm_cli/claude_code.py#L128)) | `Run: claude login  or set ANTHROPIC_API_KEY` | Subscription first ✅ |
| Live probe — not authenticated ([claude_code.py:75](../../app/integrations/llm_cli/claude_code.py#L75)) | `Not authenticated. Run: claude auth login  or set ANTHROPIC_API_KEY.` | Subscription first ✅ |
| Live probe — API key auth ([claude_code.py:78](../../app/integrations/llm_cli/claude_code.py#L78)) | `Authenticated via {apiKeySource}.` | Branch is unreachable in the current CLI — see latent-bug note ⚠️ |
| Live probe — subscription auth ([claude_code.py:80](../../app/integrations/llm_cli/claude_code.py#L80)) | `Authenticated via Claude subscription ({email}).` | Subscription called out by name ✅ |
| No-binary fallback ([claude_code.py:107-115](../../app/integrations/llm_cli/claude_code.py#L107-L115)) | `Run: claude auth login  or set ANTHROPIC_API_KEY.` | Subscription first ✅ |
| `.env.example` ([line 23](../../.env.example)) | `Claude Code CLI works for opensre investigate after claude login or setting ANTHROPIC_API_KEY.` | Subscription first ✅ |
| `docs/llm-providers.mdx` ([table row](../llm-providers.mdx)) | `Auth: claude login (CLI)` (API key not even listed for `claude-code`) | Subscription only ✅ |
| `docs/llm-providers.mdx` ([Claude Code section](../llm-providers.mdx)) | Mentions only `claude login` | Subscription only ✅ |

**Found gap:** the module-level docstring at the top of `claude_code.py` was
the one place that put `ANTHROPIC_API_KEY` first, which contradicts the rest of
the codebase. **Fixed in this branch.**

**Found bug (auth detail discriminator):** `_probe_cli_auth` originally used
`apiKeySource` as the primary discriminator between subscription and API-key
auth, with `email` as the subscription fallback. Live verification on CLI
2.1.123 across all four auth states showed this is wrong:

| Auth state | exit | `loggedIn` | `authMethod` | `apiKeySource` | `email` |
| --- | --- | --- | --- | --- | --- |
| Subscription only | 0 | `true` | `claude.ai` | absent | present |
| API key only (env, fresh HOME) | 0 | `true` | `api_key` | `"ANTHROPIC_API_KEY"` | absent |
| Subscription + API key in env | 0 | `true` | `claude.ai` | `"ANTHROPIC_API_KEY"` | present |
| No auth | **1** | `false` | `none` | absent | absent |

The third row is the proof: `apiKeySource` is set even though the CLI is
actively using the subscription. The previous code branched on
`apiKeySource`-presence and would have reported "Authenticated via
ANTHROPIC_API_KEY" for those users. Fixed by branching on `authMethod`
instead, with `apiKeySource` and `email` used only as supporting detail.

## 3. Goal 3 — cross-OS matrix

| Aspect | macOS (`darwin`) | Linux | Windows (`win32`) |
| --- | --- | --- | --- |
| **Install detection (PATH)** | `shutil.which("claude")` | `shutil.which("claude")` | `shutil.which` over `claude.{cmd,exe,ps1,bat}` |
| **Install detection (fallback dirs)** | `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, `~/.npm-global/bin`, `~/.volta/bin`, `$PNPM_HOME`, `$XDG_DATA_HOME/pnpm`, npm prefix | same as macOS minus Homebrew | `%APPDATA%\npm`, `%LOCALAPPDATA%\Programs\claude`, npm prefix |
| **Login detection (binary present)** | `claude auth status` (live probe) | `claude auth status` | `claude auth status` |
| **Login detection (no binary)** | env → file → `None` (Keychain may hold creds) | env → file → `False` | env → file → `False` |
| **OAuth credential storage** | macOS Keychain | `~/.claude/.credentials.json` | `~/.claude/.credentials.json` |
| **Env vars forwarded into subprocess** | adapter forwards `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`; `runner.py` also passes through the platform-agnostic [allowlist](../../app/integrations/llm_cli/runner.py) (`HOME`, `USER`, `LOGNAME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`, `COMSPEC`, `SHELL`, `TMP`, `TEMP`, `TMPDIR`, `LANG`, `TERM`, `TZ`, proxy vars, CA-bundle vars, `NO_COLOR`/`FORCE_COLOR`/`COLORTERM`, `XDG_*`, plus prefixes `LC_*`, `CODEX_*`, `CLAUDE_*`). Allowlist is one shared frozenset; OS-specific keys (Windows `APPDATA`/`LOCALAPPDATA`/`USERPROFILE`, POSIX `LOGNAME`) are present in the allowlist on every platform but only have values on the platform that sets them. | same | same |
| **Failure: no binary** | Recovery menu → install / enter path / repick | same | same |
| **Failure: not logged in** | `requires login` → `claude login` / set env / repick | same | same |
| **Failure: probe timeout / unknown** | `could not verify` → retry / repick | same | same |
| **Failure: ANTHROPIC_API_KEY invalid at runtime** | Surfaced by `claude -p` exit code; `explain_failure` returns CLI stderr | same | same |

**Verdict:** ✅ End-to-end parity holds across the three platforms. The only
intentional divergence is the no-binary fallback returning `None` on macOS vs
`False` on Linux/Windows, which exists because Keychain creds can outlive a
missing binary on macOS but cannot on the other two platforms.

**Test-coverage gap:** Linux and macOS no-binary cases were tested; Windows
was not. **Added `test_classify_auth_no_credentials_windows` in this branch.**

## 4. Prioritised fixes

### Landed in this branch

- **P0 (user-visible bug fix)** — `_probe_cli_auth` now parses the JSON output
  before consulting the exit code. Real CLI returns exit 1 with valid JSON
  when `loggedIn` is false; the previous code returned `None` (uncertain)
  on any non-zero exit, so the wizard's "Could not verify login" branch hid
  the actual "user is not logged in" state. Now `loggedIn: false` resolves to
  a definitive `False`, and the wizard routes the user to the "requires
  login" recovery path with the correct hint.
- **P1 (auth detail bug fix)** — `_probe_cli_auth` now branches on
  `authMethod` (`claude.ai` / `api_key`) instead of `apiKeySource` to choose
  the detail string, so subscription users with `ANTHROPIC_API_KEY` also set
  in the env no longer get reported as "Authenticated via ANTHROPIC_API_KEY".
- **P1** — `claude_code.py` module docstring updated to lead with `claude login`
  (subscription) and treat `ANTHROPIC_API_KEY` as fallback, matching the rest
  of the codebase.
- **P1** — Added `test_classify_auth_no_credentials_windows` mirroring the
  Linux test, closing the cross-OS test-coverage gap.
- **P1** — Added `test_probe_cli_auth_subscription_with_env_api_key_reports_subscription`
  as a regression guard for the third row in the auth-state table above, plus
  a refreshed `test_probe_cli_auth_not_logged_in` that uses the real CLI's
  `exit 1 + loggedIn:false` shape (regression guard for the P0 fix above).
  Updated existing tests to include `authMethod` in their mocked JSON so they
  match the live CLI shape rather than the partial shape inferred at the time
  of `#1217`.

### Optional follow-ups (separate issues)

- **P2** — `opensre doctor` does not surface anything for CLI-backed providers.
  When `LLM_PROVIDER=claude-code` it currently passes silently
  ([doctor.py:46-64](../../app/cli/commands/doctor.py#L46-L64)). A small
  enhancement would call into the existing adapter's `detect()` and report
  installed/logged-in state. Low-risk, user-visible improvement.
- **P3** — On macOS, when `claude auth status` times out, the wizard surfaces
  `auth state unknown` and offers retry. If the underlying cause is a locked
  Keychain, a hint pointing at `security unlock-keychain` would shorten
  diagnosis. Niche; only worth doing if support tickets show this in the wild.
- **P3** — The probe timeout is a single 8-second budget that has to absorb
  Claude Code's cold-start cache init. If real-world telemetry shows it firing,
  consider extending it for the first-run case only. Premature otherwise.

## Files changed in this branch

- [`app/integrations/llm_cli/claude_code.py`](../../app/integrations/llm_cli/claude_code.py)
  — docstring ordering and `_probe_cli_auth` rewrite (parse JSON before
  consulting exit code; branch on `authMethod`; clearer docstring on the
  three return states and why each happens).
- [`tests/integrations/llm_cli/test_claude_code_adapter.py`](../../tests/integrations/llm_cli/test_claude_code_adapter.py)
  — added the Windows no-binary test, the subscription-with-env-key regression
  test, the exit-1 `loggedIn:false` regression test; updated existing mocked
  JSON to include `authMethod` so it matches the live CLI shape; helper
  `_auth_status_proc` extended with an `auth_method` parameter and now
  mirrors the real CLI's exit code (`1` when not logged in).
- This note.
