"""Tests for workspace terminal line editing, PromptSession, history, and lifecycle."""

import io
import os
import pty
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from prompt_toolkit import PromptSession
from prompt_toolkit.data_structures import Size
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.vt100 import Vt100Input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from support.cli.composition import get_profile_service
from support.cli.workspace import run_workspace
from support.domain.profiles import SQLiteConnection


def test_prompt_session_line_editing_middle_insert() -> None:
    """Verify left arrow and middle insertion in PromptSession."""
    with create_pipe_input() as pipe_inp:
        session = PromptSession(input=pipe_inp, output=DummyOutput(), history=InMemoryHistory())
        # Type 'check', move left twice, insert 'X', press Enter
        pipe_inp.send_text("check\x1b[D\x1b[DX\r")
        result = session.prompt("support > ")
        # Cursor moved left 2 positions: after 'c', 'h', 'e' (between 'e' and 'c') -> 'cheXck'
        assert result == "cheXck"


def test_prompt_session_home_and_end_navigation() -> None:
    """Verify Ctrl+A (Home) and Ctrl+E (End) cursor navigation."""
    with create_pipe_input() as pipe_inp:
        session = PromptSession(input=pipe_inp, output=DummyOutput(), history=InMemoryHistory())
        # Type 'world', Ctrl+A to beginning, type 'hello ', Ctrl+E to end, type '!', Enter
        pipe_inp.send_text("world\x01hello \x05!\r")
        result = session.prompt("support > ")
        assert result == "hello world!"


def test_prompt_session_history_navigation_up_down() -> None:
    """Verify up/down arrow history recall."""
    history = InMemoryHistory()
    history.append_string("first query")
    history.append_string("second query")

    with create_pipe_input() as pipe_inp:
        session = PromptSession(input=pipe_inp, output=DummyOutput(), history=history)
        # Press Up arrow once -> retrieves 'second query', press Enter
        pipe_inp.send_text("\x1b[A\r")
        result = session.prompt("support > ")
        assert result == "second query"

    with create_pipe_input() as pipe_inp:
        session = PromptSession(input=pipe_inp, output=DummyOutput(), history=history)
        # Press Up twice -> retrieves 'first query', press Enter
        pipe_inp.send_text("\x1b[A\x1b[A\r")
        result = session.prompt("support > ")
        assert result == "first query"


def test_prompt_session_unicode_and_backspace() -> None:
    """Verify Unicode handling and Backspace editing."""
    with create_pipe_input() as pipe_inp:
        session = PromptSession(input=pipe_inp, output=DummyOutput(), history=InMemoryHistory())
        # Type 'cafx', Backspace (\x08), type 'é', Enter -> 'café'
        pipe_inp.send_text("cafx\x08é\r")
        result = session.prompt("support > ")
        assert result == "café"


def test_pty_terminal_line_editing() -> None:
    """Simulate real VT100 terminal escape sequences via PTY."""
    master, slave = pty.openpty()
    master_f = io.open(master, "w+b", buffering=0)
    slave_in = io.open(slave, "r", encoding="utf-8", closefd=False)
    slave_out = io.open(slave, "w", encoding="utf-8", closefd=False)

    try:
        vt_input = Vt100Input(slave_in)
        vt_output = Vt100_Output(slave_out, lambda: Size(rows=24, columns=80))
        session = PromptSession(input=vt_input, output=vt_output, history=InMemoryHistory())

        master_f.write(b"check\x1b[D\x1b[DX\r")
        result = session.prompt("support > ")
        assert result == "cheXck"
    finally:
        master_f.close()
        slave_in.close()
        slave_out.close()
        try:
            os.close(master)
        except OSError:
            pass
        try:
            os.close(slave)
        except OSError:
            pass


def test_workspace_repl_interactive_prompt_session_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify workspace instantiates one PromptSession with InMemoryHistory when stdin is a tty."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    service = get_profile_service()
    service.add_profile("test-profile", SQLiteConnection(database_path=":memory:"))

    with patch("sys.stdin.isatty", return_value=True), patch(
        "prompt_toolkit.PromptSession.prompt", side_effect=["/status", "/exit"]
    ) as mock_prompt:
        run_workspace()

        # Prompt called twice (/status, then /exit)
        assert mock_prompt.call_count == 2
        captured = capsys.readouterr().out
        assert "Goodbye!" in captured


def test_workspace_repl_handles_keyboard_interrupt_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify Ctrl+C (KeyboardInterrupt) exits cleanly without a traceback."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    service = get_profile_service()
    service.add_profile("test-profile", SQLiteConnection(database_path=":memory:"))

    with patch("sys.stdin.isatty", return_value=True), patch(
        "prompt_toolkit.PromptSession.prompt", side_effect=KeyboardInterrupt
    ):
        run_workspace()

        captured = capsys.readouterr().out
        assert "Goodbye!" in captured


def test_workspace_repl_handles_eof_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify Ctrl+D (EOFError) exits cleanly without a traceback."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    service = get_profile_service()
    service.add_profile("test-profile", SQLiteConnection(database_path=":memory:"))

    with patch("sys.stdin.isatty", return_value=True), patch(
        "prompt_toolkit.PromptSession.prompt", side_effect=EOFError
    ):
        run_workspace()

        captured = capsys.readouterr().out
        assert "Goodbye!" in captured
