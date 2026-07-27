"""Console logging with compact handling for streamed command output."""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
from typing import Any, TextIO


class _PlainMarkupError(Exception):
    pass


try:
    from rich.console import Console as RichConsole
    from rich.errors import MarkupError as RichMarkupError
    from rich.text import Text as RichText
except ImportError:
    RichConsole = None  # type: ignore[assignment,misc]
    RichMarkupError = _PlainMarkupError  # type: ignore[assignment,misc]
    RichText = None  # type: ignore[assignment,misc]


class _PlainCapture:
    def __init__(self, console: "_PlainConsole") -> None:
        self.console = console
        self.output = io.StringIO()
        self.previous_file: TextIO | None = None

    def __enter__(self) -> "_PlainCapture":
        self.previous_file = self.console.file
        self.console.file = self.output
        return self

    def __exit__(self, *args: object) -> None:
        assert self.previous_file is not None
        self.console.file = self.previous_file

    def get(self) -> str:
        return self.output.getvalue()


class _PlainConsole:
    def __init__(
        self,
        *,
        file: TextIO | None = None,
        stderr: bool = False,
        force_terminal: bool | None = None,
        **_: object,
    ) -> None:
        self.file = file if file is not None else (sys.stderr if stderr else sys.stdout)
        self.force_terminal = force_terminal

    @property
    def is_terminal(self) -> bool:
        if self.force_terminal is not None:
            return self.force_terminal
        isatty = getattr(self.file, "isatty", None)
        return bool(isatty and isatty())

    def print(
        self,
        *objects: object,
        sep: str = " ",
        end: str = "\n",
        **_: object,
    ) -> None:
        self.file.write(sep.join(str(item) for item in objects) + end)

    def capture(self) -> _PlainCapture:
        return _PlainCapture(self)


console = RichConsole() if RichConsole is not None else _PlainConsole()
error_console = (
    RichConsole(stderr=True)
    if RichConsole is not None
    else _PlainConsole(stderr=True)
)

AGENT = (
    os.environ.get("CODEX_CI") == "1"
    or os.environ.get("AGENT") == "1"
    or bool(os.environ.get("GITHUB_ACTIONS"))
)
STREAM_HISTORY_LIMIT = 15
STREAM_TRUNCATED_LINE = "| ... <truncated>"


def run_streamed(
    command: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_chunks: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            output_chunks.append(line)
            stream(line)
        returncode = process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()

    output = "".join(output_chunks)
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, command, output=output)
    return subprocess.CompletedProcess(command, returncode, stdout=output)


class SpacesHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._stream_lines: list[str] = []
        self._stream_rendered_lines = 0
        self._stream_rendered_snapshot: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "spaces_stream", False):
            self._emit_stream(record)
            return

        self._bake_stream_history()
        self._emit_record(record)

    def _emit_record(self, record: logging.LogRecord) -> None:
        lines = record.getMessage().splitlines() or [""]
        self._emit_lines(record.levelno, record.levelname, lines)

    def _emit_lines(
        self, levelno: int, levelname: str, lines: list[str]
    ) -> None:
        target = error_console if levelno >= logging.WARNING else console
        for index, line in enumerate(lines):
            line_prefix = (
                ""
                if levelno < logging.WARNING and line.startswith("| ")
                else "| "
            )
            if index == 0 and levelno >= logging.WARNING:
                line_prefix += f"{levelname}: "

            rendered_prefix: object = line_prefix
            if RichText is not None:
                rendered_prefix = RichText(line_prefix, no_wrap=True)
                if index == 0 and levelno >= logging.WARNING:
                    label = f"{levelname}:"
                    start = line_prefix.index(label)
                    style = "red" if levelno >= logging.ERROR else "yellow"
                    rendered_prefix.stylize(style, start, start + len(label))
            self._print_line(target, rendered_prefix, line)
        target.file.flush()

    def _print_line(self, target: Any, line_prefix: object, line: str) -> None:
        prefix = (
            RichText(line_prefix, no_wrap=True)
            if RichText is not None and not isinstance(line_prefix, RichText)
            else line_prefix
        )
        try:
            target.print(prefix, line, sep="")
        except RichMarkupError:
            target.print(prefix, line, sep="", markup=False)

    def _emit_stream(self, record: logging.LogRecord) -> None:
        lines = self._stream_record_lines(record.getMessage())
        if not self._supports_ephemeral_stream():
            self._emit_lines(logging.INFO, "INFO", lines)
            return

        self._stream_lines.extend(lines)
        self._render_stream()

    def _stream_record_lines(self, message: str) -> list[str]:
        return [f"| {line}" for line in message.splitlines() or [""]]

    def _supports_ephemeral_stream(self) -> bool:
        return console.is_terminal and not AGENT

    def _stream_display_limit(self) -> int:
        terminal_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
        return max(1, terminal_lines - 1)

    def _stream_snapshot(self, limit: int) -> list[str]:
        if len(self._stream_lines) <= limit:
            return self._stream_lines.copy()
        if limit == 1:
            return [STREAM_TRUNCATED_LINE]
        return [STREAM_TRUNCATED_LINE, *self._stream_lines[-(limit - 1) :]]

    def _render_stream(self) -> None:
        lines = self._stream_snapshot(self._stream_display_limit())
        if lines == self._stream_rendered_snapshot:
            return

        old_lines = self._stream_rendered_snapshot
        shared_lines = self._shared_prefix_length(old_lines, lines)
        output: list[str] = []
        if old_lines:
            output.append("\033[F" * (len(old_lines) - shared_lines))

        rendered_suffix = self._render_stream_lines(lines[shared_lines:])
        if rendered_suffix:
            for rendered_line in rendered_suffix.splitlines(keepends=True):
                output.append("\033[2K")
                output.append(rendered_line)

        stale_lines = len(old_lines) - len(lines)
        if stale_lines > 0:
            output.append("\033[2K\n" * stale_lines)
            output.append("\033[F" * stale_lines)

        console.file.write("".join(output))
        self._stream_rendered_snapshot = lines
        self._stream_rendered_lines = len(lines)
        console.file.flush()

    def _render_stream_lines(self, lines: list[str]) -> str:
        if not lines:
            return ""
        with console.capture() as capture:
            for line in lines:
                console.print(
                    line,
                    markup=False,
                    no_wrap=True,
                    overflow="crop",
                )
        return capture.get()

    def _shared_prefix_length(self, old_lines: list[str], new_lines: list[str]) -> int:
        shared_lines = 0
        for old_line, new_line in zip(old_lines, new_lines):
            if old_line != new_line:
                break
            shared_lines += 1
        return shared_lines

    def _clear_stream_display(self) -> None:
        if self._stream_rendered_lines <= 0:
            return
        console.file.write("\033[F\033[2K" * self._stream_rendered_lines)
        console.file.flush()
        self._stream_rendered_lines = 0
        self._stream_rendered_snapshot = []

    def _bake_stream_history(self) -> None:
        if not self._stream_lines:
            return

        self._clear_stream_display()
        lines = self._stream_snapshot(STREAM_HISTORY_LIMIT)
        self._stream_lines.clear()
        self._emit_lines(logging.INFO, "INFO", lines)


logger = logging.getLogger("spaces")
_logging_configured = False


def configure_logging() -> None:
    global _logging_configured

    if _logging_configured:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    logger.addHandler(SpacesHandler())
    _logging_configured = True


def log(message: object = "") -> None:
    logger.info("%s", message)


def warning(message: object = "") -> None:
    logger.warning("%s", message)


def error(message: object = "") -> None:
    logger.error("%s", message)


def stream(message: str) -> None:
    logger.info("%s", message, extra={"spaces_stream": True})
