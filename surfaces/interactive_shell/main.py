"""Public REPL entrypoints."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import click
from rich.console import Console

from config.repl_config import ReplConfig

if TYPE_CHECKING:
    from surfaces.interactive_shell.controller import InteractiveShellController
    from surfaces.interactive_shell.runtime.context import ReplRuntime
    from surfaces.interactive_shell.runtime.core.state import ReplState
    from surfaces.interactive_shell.session import Session

# Fallback when a caller does not supply one. Forces a terminal because the
# shell owns the screen; an embedding caller passes its own instead.
_DEFAULT_CONSOLE = Console(
    highlight=False, force_terminal=True, color_system="truecolor", legacy_windows=False
)


def _new_shell_session() -> Session:
    """The shell's first session, under the process analytics session id when still unclaimed.

    Sharing it keeps ``cli_invoked``, startup onboarding and the shell's turns
    in one analytics session; ``/new`` and ``/resume`` still change the id.
    """
    from infrastructure.analytics.usage_context import claim_process_session_id
    from surfaces.interactive_shell.session import Session

    session_id = claim_process_session_id()
    return Session(session_id=session_id) if session_id else Session()


def _create_repl_runtime(session: Session) -> ReplRuntime:
    """Build the full REPL runtime after startup feedback is visible."""
    from surfaces.interactive_shell.runtime.context import create_repl_runtime

    return create_repl_runtime(session=session)


def _build_interactive_shell_controller(
    runtime_context: ReplRuntime,
    *,
    config: ReplConfig,
    console: Console,
) -> InteractiveShellController:
    """Build the controller without importing its stack during entrypoint import."""
    from surfaces.interactive_shell.controller import InteractiveShellController

    return InteractiveShellController(runtime_context, config=config, console=console)


def _close_repl_session(session: Session, state: ReplState) -> None:
    """Persist final session state, including an interrupted goal-pause boundary."""
    from core.agent_harness import SessionManager
    from core.agent_harness.spi.session_goal import pause_active_session_goal
    from infrastructure.turn_host.session_lock import session_execution_lock

    pause_requested = state.is_goal_pause_requested()
    manager = SessionManager.for_session(session)
    with session_execution_lock(session.session_id):
        manager.refresh_from_storage(session)
        if pause_requested:
            pause_active_session_goal(session)
        manager.close(session)


async def run_repl_async(
    initial_input: str | None = None,
    config: ReplConfig | None = None,
    resume_session_id: str | None = None,
    console: Console | None = None,
    cli_command_group: click.Command | None = None,
    finish_banner: Callable[[], None] | None = None,
    after_banner: Callable[[], None] | None = None,
) -> int:
    """Run the shell on an existing event loop and return its exit code.

    ``cli_command_group`` is the ``opensre`` Click group the shell documents to
    the model; the process entrypoint passes it, embedders may leave it out.
    ``after_banner`` is launch work the CLI held back until the banner is on
    screen (error-reporting start); it runs once the runtime is booted.
    """
    from core.agent_harness import SessionManager
    from infrastructure.analytics.github_identity import identify_saved_github_username
    from infrastructure.logging import (
        install_shell_log_handler,
        quiet_noisy_third_party_loggers,
    )
    from infrastructure.terminal.theme import set_active_theme
    from surfaces.interactive_shell.runtime.startup.demo_picker import offer_demo
    from surfaces.interactive_shell.runtime.startup.initial_input import run_initial_input

    # Keep MCP schema-cache warnings / httpx chatter off the transcript —
    # progress is soft status lines, not library WARNINGs.
    quiet_noisy_third_party_loggers()
    identify_saved_github_username()

    cfg = config or ReplConfig.load()
    set_active_theme(cfg.theme)
    out = console or _DEFAULT_CONSOLE
    # WARNING+ records print through the shell console, so one emitted from a
    # probe thread while a status spinner animates lands whole above it instead
    # of racing the spinner's redraw on the tty and staircasing what follows.
    install_shell_log_handler(lambda: out)
    # Let PromptBuilder build the prompt session so it can wire the
    # composer-hide (needs the session + REPL state, which do not exist yet).
    runtime_context = _create_repl_runtime(_new_shell_session())
    session = runtime_context.session
    session.terminal.cli_command_group = cli_command_group

    if initial_input:
        if after_banner is not None:
            after_banner()
        session.warm_resolved_integrations()
        return run_initial_input(initial_input, session, out)

    # The sign-in gate runs once, in the synchronous ``run_repl`` entrypoint,
    # where it interleaves with the launch-banner paint. This coroutine is the
    # shell body only; embedders driving it directly manage their own auth.

    # Open the session file now that we know this is an interactive REPL run.
    SessionManager.for_session(session).open_store(session)
    try:
        # Controller construction imports the remaining prompt and turn stack.
        # Keep that work behind the launch animation; it retains this live
        # Session object, which startup resume may rebind below.
        controller = _build_interactive_shell_controller(
            runtime_context,
            config=cfg,
            console=out,
        )
        # The runtime is booted; nothing has printed yet. Stop the launch spin
        # and paint the static banner before anything below can write.
        if finish_banner is not None:
            finish_banner()
        # Held-back work no longer competes with launch imports.
        if after_banner is not None:
            after_banner()

        if resume_session_id:
            from surfaces.interactive_shell.command_registry.session_cmds.resume import (
                resume_session_by_prefix,
            )

            slash_command = f"/resume {resume_session_id.strip()}"
            if not resume_session_by_prefix(
                resume_session_id.strip(),
                session,
                out,
                slash_command=slash_command,
            ):
                return 1
        else:
            # Entering the master skill queues its menu; the first model turn is the answer.
            offer_demo(session, out)

        await controller.start_interactive_shell()
        return 0
    finally:
        # True end-of-run teardown: persist and release the session's resources.
        _close_repl_session(session, runtime_context.state)


class _LaunchBannerHandle:
    """Stop the launch animation exactly once, with or without painting the banner."""

    def __init__(
        self,
        *,
        console: Console,
        stop: threading.Event,
        spinner: threading.Thread,
        on_painted: Callable[[], None] | None,
    ) -> None:
        self._console = console
        self._stop = stop
        self._spinner = spinner
        self._on_painted = on_painted
        self._settled = False
        self._settle_lock = threading.Lock()

    def _settle(self) -> bool:
        with self._settle_lock:
            if self._settled:
                return False
            self._settled = True
            self._stop.set()
            self._spinner.join()
            return True

    def __call__(self) -> None:
        """Stop the animation and replace it with the static shell banner."""
        if not self._settle():
            return

        from surfaces.interactive_shell.ui.terminal_ui import render_terminal_ui

        render_terminal_ui(self._console, animate=False)
        if self._on_painted is not None:
            self._on_painted()

    def cancel(self) -> None:
        """Stop the animation without painting startup UI over an error."""
        self._settle()


def _start_launch_banner(
    console: Console, *, on_painted: Callable[[], None] | None = None
) -> _LaunchBannerHandle:
    """Spin the wordmark on a thread while the runtime boots; return its handle.

    Calling the handle stops the spin (after its minimum frames), waits for it,
    and prints the static banner before anything else writes to the screen.
    Cancelling it stops the thread without painting over a startup error. Off
    a TTY the spin is a no-op and only the static banner prints.
    """
    stop = threading.Event()

    def animate() -> None:
        from surfaces.shared.terminal.banner import animate_launch_wordmark

        animate_launch_wordmark(console, stop=stop)

    spinner = threading.Thread(
        target=animate,
        name="launch-banner-spin",
        daemon=True,
    )
    spinner.start()

    return _LaunchBannerHandle(
        console=console,
        stop=stop,
        spinner=spinner,
        on_painted=on_painted,
    )


def run_repl(
    initial_input: str | None = None,
    config: ReplConfig | None = None,
    *,
    resume_session_id: str | None = None,
    console: Console | None = None,
    cli_command_group: click.Command | None = None,
    after_banner: Callable[[], None] | None = None,
    capture_shell_rendered: bool = True,
) -> int:
    """Run the shell on a new event loop and return its exit code.

    ``interactive_shell_rendered`` fires at the sign-in screen when that is
    painted, or at first banner paint when the user is already signed in.
    ``--resume`` and an auto-launch after ``opensre onboard`` do not record it.
    """
    from infrastructure.analytics.capture import capture_interactive_shell_rendered
    from infrastructure.terminal.theme import set_active_theme
    from surfaces.interactive_shell.runtime.startup.account_gate import pass_sign_in_gate
    from surfaces.shared.terminal.components.rendering import repl_clear_screen

    cfg = config or ReplConfig.load()
    set_active_theme(cfg.theme)
    out = console or _DEFAULT_CONSOLE
    if not cfg.enabled and not resume_session_id:
        return 0
    if not sys.stdin.isatty() and initial_input is None:
        return 0

    record_shell = capture_shell_rendered and not resume_session_id
    shell_rendered = False

    def record_shell_rendered() -> None:
        nonlocal shell_rendered
        if not record_shell or shell_rendered:
            return
        shell_rendered = True
        capture_interactive_shell_rendered(entrypoint="opensre_binary")

    launch_banner: _LaunchBannerHandle | None = None
    try:
        if not initial_input:
            if not pass_sign_in_gate(
                out, on_screen=record_shell_rendered if record_shell else None
            ):
                return 0
            # Wipe the calling shell or completed sign-in screen so the REPL
            # reads as its own screen, then boot it under the launch animation.
            repl_clear_screen()
            launch_banner = _start_launch_banner(
                out, on_painted=record_shell_rendered if record_shell else None
            )

        return asyncio.run(
            run_repl_async(
                initial_input=initial_input,
                config=cfg,
                resume_session_id=resume_session_id,
                console=out,
                cli_command_group=cli_command_group,
                finish_banner=launch_banner,
                after_banner=after_banner,
            )
        )
    except (EOFError, KeyboardInterrupt):
        return 0
    finally:
        if launch_banner is not None:
            launch_banner.cancel()


__all__ = ["run_repl", "run_repl_async"]
