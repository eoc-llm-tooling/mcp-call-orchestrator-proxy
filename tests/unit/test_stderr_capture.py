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


def _kept(capture: BackendStderr) -> list[str]:
    return capture.lines_between(0)


def test_holds_an_endless_line_only_as_far_as_it_is_reported() -> None:
    capture = BackendStderr()
    for _ in range(200):
        capture._keep(b"x" * 65536)
        assert len(capture._partial) <= 2000

    capture._keep(b"\n")

    assert _kept(capture) == ["x" * 500]


def test_a_line_longer_than_a_read_is_cut_without_losing_the_next_line() -> None:
    capture = BackendStderr()
    capture._keep("é".encode() * 100_000 + b"\nafter\n")

    assert _kept(capture) == ["é" * 500, "after"]


def test_keeps_the_last_redraw_of_a_line_written_with_carriage_returns() -> None:
    capture = BackendStderr()
    capture._keep(b"10%\r20%\r30%\ndone\n")

    assert _kept(capture) == ["30%", "done"]


def test_a_carriage_return_before_the_newline_ends_the_line_cleanly() -> None:
    capture = BackendStderr()
    capture._keep(b"first\r\nsecond\r\n\r\nthird\r\r\n")

    assert _kept(capture) == ["first", "second", "third"]


def test_a_carriage_return_and_its_newline_may_arrive_in_separate_reads() -> None:
    capture = BackendStderr()
    capture._keep(b"downloading 10%\rdownloading 90%\r")
    assert len(capture._partial) < 20
    capture._keep(b"\nnext")
    capture._keep(b" line\n")

    assert _kept(capture) == ["downloading 90%", "next line"]


def test_a_redrawn_line_never_reaches_the_report_with_a_carriage_return() -> None:
    with BackendStderr() as capture:
        _run_child(capture, "a\rb\rc\r\nlast\rline")
    assert _kept(capture) == ["c", "line"]


@pytest.mark.parametrize(
    ("written", "reported"),
    [
        (
            "fetching https://user:hunter2@example.test/simple/x",
            "fetching https://***@example.test/simple/x",
        ),
        ("Authorization: Bearer abcdef0123456789", "Authorization: ***"),
        ("sent Bearer abcdef0123456789 to host", "sent Bearer *** to host"),
        ("API_KEY=sk-live-1234 started", "API_KEY=*** started"),
        ("GET /mcp?access_token=abc123&x=1", "GET /mcp?access_token=***&x=1"),
        ('{"password": "hunter2"}', '{"password": "***"}'),
        (
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl end",
            "jwt *** end",
        ),
        ("no secrets here: port 8080", "no secrets here: port 8080"),
        ("error: could not resolve the server", "error: could not resolve the server"),
    ],
)
def test_masks_obvious_secrets_in_the_kept_lines(written: str, reported: str) -> None:
    capture = BackendStderr()
    capture._keep(written.encode() + b"\n")

    assert _kept(capture) == [reported]


def test_masks_a_secret_in_a_line_cut_to_length() -> None:
    capture = BackendStderr()
    capture._keep(b"x" * 480 + b" token=" + b"s" * 5000 + b"\n")

    (line,) = _kept(capture)
    assert "sss" not in line
    assert len(line) <= 500


def test_forwards_what_it_masks_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    forwarded = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", forwarded)

    with BackendStderr() as capture:
        _run_child(capture, "token=abc123\n")
        _wait_for_lines(capture, 1)

        assert _kept(capture) == ["token=***"]

    assert forwarded.buffer.getvalue() == b"token=abc123\n"
