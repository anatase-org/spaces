from __future__ import annotations

import io
import logging
import subprocess
import unittest
from unittest import mock
from unittest.mock import patch

from spaces import logging as spaces_logging
from spaces.logging import SpacesHandler, _PlainConsole


class SpacesLoggingTests(unittest.TestCase):
    def test_plain_logging_does_not_enable_rich(self) -> None:
        with (
            patch.object(spaces_logging, "_logging_configured", False),
            patch.object(spaces_logging, "_enable_rich") as enable_rich,
            patch.object(spaces_logging.logger, "handlers", []),
        ):
            spaces_logging.configure_logging(rich=False)

        enable_rich.assert_not_called()

    def test_rich_logging_enables_rich(self) -> None:
        with (
            patch.object(spaces_logging, "_logging_configured", False),
            patch.object(spaces_logging, "_enable_rich") as enable_rich,
            patch.object(spaces_logging.logger, "handlers", []),
        ):
            spaces_logging.configure_logging(rich=True)

        enable_rich.assert_called_once_with()

    def test_run_streamed_combines_stdout_and_stderr(self) -> None:
        process = mock.Mock()
        process.stdout = io.StringIO("stdout line\nstderr line\n")
        process.wait.return_value = 0
        process.poll.return_value = 0
        command = ["debootstrap", "resolute", "/rootfs"]

        with (
            patch.object(
                spaces_logging.subprocess, "Popen", return_value=process
            ) as popen,
            patch.object(spaces_logging, "stream") as stream,
        ):
            result = spaces_logging.run_streamed(command)

        popen.assert_called_once_with(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(
            stream.call_args_list,
            [mock.call("stdout line\n"), mock.call("stderr line\n")],
        )
        self.assertEqual(result.stdout, "stdout line\nstderr line\n")

    def test_run_streamed_can_return_a_failure_without_raising(self) -> None:
        process = mock.Mock()
        process.stdout = io.StringIO("failed\n")
        process.wait.return_value = 42
        process.poll.return_value = 42

        with (
            patch.object(spaces_logging.subprocess, "Popen", return_value=process),
            patch.object(spaces_logging, "stream"),
        ):
            result = spaces_logging.run_streamed(["false"], check=False)

        self.assertEqual(result.returncode, 42)
        self.assertEqual(result.stdout, "failed\n")

    def test_run_streamed_waits_for_child_cleanup_on_keyboard_interrupt(
        self,
    ) -> None:
        process = mock.Mock()
        process.stdout.__iter__ = mock.Mock(side_effect=KeyboardInterrupt)
        process.wait.return_value = 130
        process.poll.return_value = 130

        with (
            patch.object(
                spaces_logging.subprocess,
                "Popen",
                return_value=process,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            spaces_logging.run_streamed(["spaces.priv", "create", "{}"])

        process.wait.assert_called_once_with()
        process.terminate.assert_not_called()

    def test_stream_records_are_concatenated_until_next_message(self) -> None:
        output = io.StringIO()
        handler = SpacesHandler()

        with (
            patch("spaces.logging.AGENT", False),
            patch(
                "spaces.logging.console",
                _PlainConsole(file=output, force_terminal=True),
            ),
        ):
            first = logging.LogRecord(
                "spaces", logging.INFO, "", 0, "first", (), None
            )
            handler._emit_stream(first)
            second = logging.LogRecord(
                "spaces", logging.INFO, "", 0, "second", (), None
            )
            handler._emit_stream(second)

            self.assertEqual(handler._stream_lines, ["| first", "| second"])
            handler._bake_stream_history()

        self.assertTrue(
            output.getvalue().endswith("| first\n| second\n")
        )

    def test_normal_records_have_no_stream_prefix(self) -> None:
        output = io.StringIO()
        handler = SpacesHandler()
        record = logging.LogRecord(
            "spaces", logging.INFO, "", 0, "first\nsecond", (), None
        )

        with patch(
            "spaces.logging.console",
            _PlainConsole(file=output, force_terminal=False),
        ):
            handler.emit(record)

        self.assertEqual(output.getvalue(), "first\nsecond\n")

    def test_warning_records_have_no_stream_prefix(self) -> None:
        output = io.StringIO()
        handler = SpacesHandler()
        record = logging.LogRecord(
            "spaces", logging.WARNING, "", 0, "careful", (), None
        )

        with patch(
            "spaces.logging.error_console",
            _PlainConsole(file=output, force_terminal=False),
        ):
            handler.emit(record)

        self.assertEqual(output.getvalue(), "WARNING: careful\n")

    def test_non_terminal_stream_is_printed_directly(self) -> None:
        output = io.StringIO()
        handler = SpacesHandler()
        record = logging.LogRecord(
            "spaces", logging.INFO, "", 0, "command output", (), None
        )

        with (
            patch("spaces.logging.AGENT", True),
            patch(
                "spaces.logging.console",
                _PlainConsole(file=output, force_terminal=False),
            ),
        ):
            handler._emit_stream(record)

        self.assertEqual(output.getvalue(), "| command output\n")


if __name__ == "__main__":
    unittest.main()
