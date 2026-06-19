"""Prompt-toolkit runtime loop for interactive shell."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import select
import sys
import threading
from collections.abc import Callable
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.file_proxy import FileProxy
from rich.markup import escape

from app.cli.interactive_shell import alert_inbox as _alert_inbox
from app.cli.interactive_shell.alert_renderer import drain_and_render_incoming
from app.cli.interactive_shell.prompting import prompt_surface as _prompt_surface
from app.cli.interactive_shell.runtime.dispatch import (
    DispatchCancelled,
    build_cancel_key_bindings,
    dispatch_needs_exclusive_stdin,
    dispatch_one_turn,
    dispatch_should_show_spinner,
    install_session_key_bindings,
    looks_like_cancel_request,
    looks_like_confirmation_answer,
    route_confirm_through_prompt,
)
from app.cli.interactive_shell.runtime.session import ReplSession
from app.cli.interactive_shell.runtime.state import (
    PROMPT_REFRESH_INTERVAL_S,
    ReplState,
    SpinnerState,
)
from app.cli.interactive_shell.ui import ERROR, WARNING
from app.cli.support.exception_reporting import report_exception
from app.cli.support.prompt_support import repl_prompt_note_ctrl_c, repl_reset_ctrl_c_gate
from app.cli.support.repl_progress import repl_safe_progress_scope
from app.fleet_monitoring.sampler import start_sampler

log = logging.getLogger(__name__)

# A leaked CPR reply is a maximal run drawn only from the CPR alphabet:
# an optional ESC/CSI introducer, then digits, ';', '[' and 'R'. Matching the
# whole run (rather than one well-formed reply) lets us also clear the
# fragmented bursts that high-frequency streaming redraws produce, where the
# ESC is dropped and partial 'row;colR' pieces and collapsed 'RRRR' runs
# concatenate, e.g. '[34;1R57R38;57R'.
_CPR_RUN_RE = re.compile(r"(?:\x1b|\x9b)?[\[0-9;R]+")


def _looks_like_cpr_run(run: str) -> bool:
    """Whether a CPR-alphabet run is a real cursor reply rather than user text.

    Every CPR reply and every fragment of one pairs at least one digit with the
    terminating ``R``. Requiring both lets us strip leaked replies while leaving
    ordinary words that merely contain ``R`` (``Restart``) or a digit (``[1]``)
    untouched.
    """
    return "R" in run and any(ch.isdigit() for ch in run)


def _drain_stale_cpr_bytes() -> None:
    """Discard any CPR escape-sequence bytes left in stdin after a prompt_async teardown.

    When prompt_async returns (e.g. after the user types Y to confirm), the
    prompt_toolkit Application tears down its input-reader thread.  CPR responses
    (ESC[row;colR) that the bottom-toolbar refresh sent but that arrived just after
    the reader stopped are left sitting in the OS stdin buffer.  The *next*
    prompt_async call reads those bytes with a fresh vt100 parser, which has no
    open escape-sequence context; the bytes then appear as literal keystrokes in
    the input field.

    This function does a non-blocking drain of stdin between prompt_async calls —
    exactly when no Application is active and it is safe to read from stdin
    directly.  Only called on TTY stdin on POSIX; silently skipped otherwise.
    """
    if os.name == "nt" or not sys.stdin.isatty():
        return
    try:
        fd = sys.stdin.fileno()
        while select.select([fd], [], [], 0)[0]:
            chunk = os.read(fd, 256)
            if not chunk:
                break
    except OSError:
        # Draining stdin is best-effort; ignore when the fd is not readable.
        pass


def _strip_cpr_sequences(text: str | None) -> str:
    """Remove terminal cursor-position replies that leaked into text.

    Handles both intact replies (``ESC[row;colR``) and the fragmented bursts a
    streaming redraw produces (``[34;1R57R38;57R``)."""
    if not text:
        return ""
    return _CPR_RUN_RE.sub(lambda m: "" if _looks_like_cpr_run(m.group(0)) else m.group(0), text)


def _contains_cpr_sequence(text: str | None) -> bool:
    return bool(text) and _strip_cpr_sequences(text) != text


def _install_cpr_buffer_scrubber(pt_session: PromptSession[str]) -> None:
    """Strip leaked CPR replies out of the live input buffer as they arrive.

    During a streaming dispatch the spinner ticker invalidates the prompt ~10x/s.
    Each redraw under ``patch_stdout(raw=True)`` can trigger a CPR (``ESC[6n``)
    query whose reply lands while the prompt's input reader is active, so the
    bytes are parsed as keystrokes and inserted into the buffer. The
    between-prompt drain (`_drain_stale_cpr_bytes`) cannot help mid-prompt and
    the submit-time strip only fires on Enter, so the garbage is visible in the
    field while the investigation runs. This removes it in place the moment it
    appears, preserving the cursor position relative to the user's real text.
    """
    buffer = pt_session.default_buffer
    scrubbing = False

    def _scrub(_buffer: Buffer) -> None:
        nonlocal scrubbing
        if scrubbing:
            return
        text = buffer.text
        if not _contains_cpr_sequence(text):
            return
        cleaned_before = _strip_cpr_sequences(text[: buffer.cursor_position])
        cleaned_text = _strip_cpr_sequences(text)
        scrubbing = True
        try:
            buffer.document = Document(cleaned_text, cursor_position=len(cleaned_before))
        finally:
            scrubbing = False

    buffer.on_text_changed += _scrub


class StreamingConsole(Console):
    """Console adapter for streaming progress + cancellation checks."""

    def __init__(
        self,
        spinner: SpinnerState,
        cancel_event: threading.Event,
        *,
        prompt_invalidator: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._spinner = spinner
        self._cancel_event = cancel_event
        self._prompt_invalidator = prompt_invalidator

    def update_streaming_progress(self, bytes_received: int) -> None:
        self._spinner.bytes_in = bytes_received

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def suppress_prompt_spinner(self) -> None:
        """Stop the REPL spinner before another live renderer owns the footer."""
        if not self._spinner.streaming:
            return
        self._spinner.stop()
        if self._prompt_invalidator is not None:
            self._prompt_invalidator()

    def print(self, *args: Any, **kwargs: Any) -> None:
        """Reset the TTY column before each print when not streaming.

        Inline menus pad rows to the terminal width, leaving the cursor on a
        high column. Rich output that follows (tables, follow-up status lines,
        section rules) must start at column zero or lines appear broken.
        """
        if not self._spinner.streaming and not isinstance(sys.stdout, FileProxy):
            from app.cli.interactive_shell.ui.choice_menu import (
                ensure_tty_column_zero,
                prepare_repl_output_line,
            )
            from app.cli.interactive_shell.ui.rendering import (
                _repl_output_already_prepared,
                _repl_table_width,
            )

            if not args and not kwargs:
                # ``console.print()`` is used for intentional blank spacer lines.
                # Only reset the column for those calls; do not prepend another
                # line break or they expand into double blank lines.
                ensure_tty_column_zero()
            elif not _repl_output_already_prepared():
                prepare_repl_output_line()
            if sys.stdout.isatty() and "width" not in kwargs:
                kwargs["width"] = _repl_table_width(self)
        super().print(*args, **kwargs)


async def run_interactive(
    session: ReplSession,
    pt_session: PromptSession[str] | None = None,
    inbox: _alert_inbox.AlertInbox | None = None,
) -> None:
    if pt_session is None:
        pt_session = _prompt_surface._build_prompt_session(session)
        session.prompt_history_backend = pt_session.history
    spinner = SpinnerState()
    state = ReplState()
    sampler_task = start_sampler()

    cancel_kb = build_cancel_key_bindings(state)
    install_session_key_bindings(pt_session, cancel_kb)
    _install_cpr_buffer_scrubber(pt_session)

    pt_app = pt_session.app
    main_loop = asyncio.get_running_loop()
    state.bind_loop(main_loop)

    _invalidate_prompt = _prompt_surface.wire_prompt_refresh(session, pt_app, main_loop)

    def _request_exit() -> None:
        state.request_exit()

        def _exit_prompt_app(attempts_left: int = 5) -> None:
            if pt_app.is_running:
                pt_app.exit()
                return
            if attempts_left > 0:
                main_loop.call_later(0.02, _exit_prompt_app, attempts_left - 1)

        main_loop.call_soon_threadsafe(_exit_prompt_app)

    async def _run_one_dispatch(text: str) -> None:
        dispatch_cancel = threading.Event()
        current_task = asyncio.current_task()
        if current_task is not None:
            state.start_dispatch(task=current_task, cancel_event=dispatch_cancel)
        else:
            state.current_cancel_event = dispatch_cancel
        console = StreamingConsole(
            spinner,
            dispatch_cancel,
            prompt_invalidator=_invalidate_prompt,
            highlight=False,
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
        )
        from app.cli.support.output import set_prompt_suppress_fn  # lazy — avoids circular import

        show_spinner = dispatch_should_show_spinner(text, session)
        if show_spinner:
            spinner.start()
            set_prompt_suppress_fn(console.suppress_prompt_spinner)
        try:
            # Commands that take exclusive stdin ownership (e.g. bare
            # ``/investigate`` and other inline pickers) can safely use the
            # full Rich Live investigation stream because prompt_toolkit is not
            # actively reading input while we await ``state.queue.join()``.
            # Keep the REPL-safe append-only renderer for non-exclusive turns
            # to avoid Live redraw contention with the active prompt.
            progress_scope = (
                contextlib.nullcontext()
                if dispatch_needs_exclusive_stdin(text, session)
                else repl_safe_progress_scope()
            )
            with progress_scope:
                await asyncio.to_thread(
                    dispatch_one_turn,
                    text,
                    session,
                    console,
                    on_exit=_request_exit,
                    confirm_fn=lambda prompt: route_confirm_through_prompt(state, prompt),
                )
        except asyncio.CancelledError:
            console.print(f"[{WARNING}]· interrupted[/]")
            raise
        except DispatchCancelled:
            console.print(f"[{WARNING}]· interrupted[/]")
        except Exception as exc:
            report_exception(exc, context="interactive_shell.dispatch_async")
            console.print(f"[{ERROR}]dispatch error:[/] {escape(str(exc))}")
        finally:
            set_prompt_suppress_fn(None)
            if show_spinner:
                spinner.stop()
            state.finish_dispatch(dispatch_cancel)
            # Investigation Rich Live + bottom-toolbar CPR can leave bytes in stdin;
            # drain before the next prompt_async so they are not typed into the field.
            await asyncio.sleep(0.05)
            _drain_stale_cpr_bytes()

    async def _alert_watcher() -> None:
        if inbox is None:
            return
        alert_console = Console(
            highlight=False,
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
        )
        drain_and_render_incoming(session, alert_console, inbox)
        while not state.exit_requested:
            try:
                await asyncio.to_thread(inbox.pending_event.wait, timeout=1)
            except asyncio.CancelledError:
                return
            try:
                drain_and_render_incoming(session, alert_console, inbox)
            except Exception as exc:
                log.warning("Error draining incoming alerts: %s", exc)

    async def _processor() -> None:
        while not state.exit_requested:
            try:
                text = await state.queue.get()
            except asyncio.CancelledError:
                return
            if state.exit_requested:
                state.queue.task_done()
                return
            state.current_task = asyncio.create_task(_run_one_dispatch(text))
            try:
                await state.current_task
            except asyncio.CancelledError:
                # Expected when shutdown/cancel interrupts in-flight dispatch.
                pass
            except Exception as exc:
                log.debug("Processor task ended with dispatch exception: %s", exc)
            state.clear_current_task()
            state.queue.task_done()

    def _message_with_spinner() -> ANSI:
        base = _prompt_surface._prompt_message(session).value
        if state.is_awaiting_confirmation():
            confirm_text = state.confirm_prompt_text
            return ANSI(f"{confirm_text}\n{base}")
        prefix = _prompt_surface.resolve_prompt_prefix_ansi(
            inline_spinner=spinner.inline_spinner_ansi(),
            idle_hint=spinner.idle_hint_ansi(),
        )
        return ANSI(f"{prefix}\n{base}")

    async def _spinner_ticker() -> None:
        # prompt_async's refresh_interval alone is not guaranteed to drive
        # visible prompt redraws while patch_stdout(raw=True) is active and
        # the LLM stream is writing rapidly.  This task explicitly invalidates
        # the prompt at 100 ms intervals so the braille glyph cycles smoothly.
        _TICK = 0.1
        while not state.exit_requested:
            try:
                await asyncio.sleep(_TICK)
            except asyncio.CancelledError:
                return
            if spinner.streaming:
                _invalidate_prompt()

    processor_task = asyncio.create_task(_processor())
    alert_watcher_task = asyncio.create_task(_alert_watcher())
    spinner_ticker_task = asyncio.create_task(_spinner_ticker())
    try:
        with patch_stdout(raw=True):
            echo_console = Console(highlight=False, force_terminal=True, color_system="truecolor")
            while True:
                if state.exit_requested:
                    return
                if inbox is not None:
                    try:
                        drain_and_render_incoming(session, echo_console, inbox)
                    except Exception as exc:
                        log.warning("Error draining alerts at turn start: %s", exc)

                # Drain any CPR bytes (ESC[row;colR) left in stdin from the
                # previous prompt_async's bottom-toolbar refresh cycles.
                # Each prompt_async tears down its Application; CPR responses
                # that arrive after the input-reader thread stops sit in the OS
                # stdin buffer and appear as literal keystrokes in the next
                # Application's fresh vt100 parser.
                # The brief sleep lets in-transit terminal responses land in the
                # buffer before the non-blocking select drain runs.
                await asyncio.sleep(0.05)
                _drain_stale_cpr_bytes()
                try:
                    prefilled = session.take_pending_prompt_default()
                    if prefilled and session.take_pending_autosubmit():
                        # An agent-queued command (e.g. /integrations setup) was
                        # set before this prompt opened; auto-submit it without
                        # waiting for input so it dispatches with exclusive stdin.
                        text = prefilled
                    else:
                        text = await pt_session.prompt_async(
                            message=_message_with_spinner,
                            bottom_toolbar=spinner.toolbar_ansi,
                            refresh_interval=PROMPT_REFRESH_INTERVAL_S,
                            placeholder=lambda: _prompt_surface.resolve_prompt_placeholder(session),
                            default=prefilled,
                        )
                except EOFError:
                    if state.is_dispatch_running():
                        state.cancel_current_dispatch()
                        continue
                    if session.session_id:
                        echo_console.print()
                        echo_console.print("Resume this session with:")
                        echo_console.print(f"/resume {session.session_id}")
                        echo_console.print("Goodbye!")
                    return
                except KeyboardInterrupt:
                    if state.is_dispatch_running():
                        state.cancel_current_dispatch()
                        continue
                    if repl_prompt_note_ctrl_c(echo_console, session.session_id):
                        return
                    continue
                else:
                    repl_reset_ctrl_c_gate()
                    raw_text = text
                    text = _strip_cpr_sequences(text)
                    if not text.strip() and _contains_cpr_sequence(raw_text):
                        continue

                if state.exit_requested:
                    return
                if state.is_dispatch_running() and looks_like_cancel_request(text):
                    stripped = (text or "").strip()
                    _prompt_surface.render_submitted_prompt(echo_console, session, stripped)
                    state.cancel_current_dispatch()
                    continue

                if state.is_awaiting_confirmation():
                    if looks_like_confirmation_answer(text):
                        state.deliver_confirmation(text or "")
                        continue
                    echo_console.print(
                        "[dim](type y/N to confirm the pending action; your input has been queued for after)[/]"
                    )
                    stripped = (text or "").strip()
                    if stripped:
                        _prompt_surface.render_submitted_prompt(echo_console, session, stripped)
                        await state.queue.put(stripped)
                    continue

                stripped = (text or "").strip()
                if not stripped:
                    continue
                _prompt_surface.render_submitted_prompt(echo_console, session, stripped)
                wait_for_dispatch = dispatch_needs_exclusive_stdin(stripped, session)
                await state.queue.put(stripped)
                if wait_for_dispatch:
                    await state.queue.join()
    finally:
        state.request_exit()
        state.cancel_current_dispatch()
        sampler_task.cancel()
        processor_task.cancel()
        alert_watcher_task.cancel()
        spinner_ticker_task.cancel()
        shutdown_labels = (
            "sampler",
            "processor",
            "alert watcher",
            "spinner ticker",
        )
        shutdown_results = await asyncio.gather(
            sampler_task,
            processor_task,
            alert_watcher_task,
            spinner_ticker_task,
            return_exceptions=True,
        )
        for label, result in zip(shutdown_labels, shutdown_results, strict=True):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                log.debug("%s task shutdown raised exception: %s", label, result)


__all__ = ["StreamingConsole", "run_interactive"]
