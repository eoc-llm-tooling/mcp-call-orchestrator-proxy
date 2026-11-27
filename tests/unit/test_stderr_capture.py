"""Unit tests for the stderr pipe a stdio backend's subprocesses write to."""

from __future__ import annotations

import io
import subprocess
import sys
import time

import pytest

from mcp_call_orchestrator_proxy.stderr_capture import BackendStderr


def _wait_for_lines(capture: BackendStderr, count: int) -> None:
    deadline = time.monotonic() + 5.0
    while capture.mark() < count:
        if time.monotonic() > deadline:
            pytest.fail(f"read {capture.mark()} lines, expected {count}")
        time.sleep(0.01)


def _run_child(capture: BackendStderr, text: str) -> None:
    subprocess.run(
        [sys.executable, "-c", f"import sys; sys.stderr.write({text!r})"],
        stderr=capture.sink,
        check=True,
    )


def test_keeps_a_childs_lines_and_numbers_them_across_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", forwarded)

    with BackendStderr() as capture:
        _run_child(capture, "first: one\nfirst: two\n")
        _wait_for_lines(capture, 2)
        second_start = capture.mark()
        _run_child(capture, "second: cannot start\n")
        _wait_for_lines(capture, 3)

        assert capture.lines_between(0) == [
            "first: one",
            "first: two",
            "second: cannot start",
        ]
        assert capture.lines_between(0, second_start) == ["first: one", "first: two"]
        assert capture.lines_between(second_start) == ["second: cannot start"]

    assert forwarded.buffer.getvalue() == (
        b"first: one\nfirst: two\nsecond: cannot start\n"
    )


def test_keeps_only_the_latest_lines_each_cut_to_length() -> None:
    with BackendStderr(max_lines=2) as capture:
        _run_child(capture, "a\nb\n" + "c" * 600 + "\n")
        _wait_for_lines(capture, 3)

        assert capture.lines_between(0) == ["b", "c" * 500]


def test_a_last_line_without_newline_is_kept_once_the_pipe_closes() -> None:
    capture = BackendStderr()
    with capture:
        _run_child(capture, "no newline at the end")
    assert capture.lines_between(0) == ["no newline at the end"]


def test_has_no_sink_before_it_is_opened() -> None:
    with pytest.raises(RuntimeError):
        _ = BackendStderr().sink
