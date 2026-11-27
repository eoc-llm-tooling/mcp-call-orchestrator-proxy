"""A stdio backend's stderr, passed through to the proxy's own and kept in part.

A stdio backend that fails before it answers - a launcher that cannot resolve
the server, a missing credential - says why only on its stderr, and the proxy
itself sees nothing but the pipe closing. ``BackendStderr`` is the stderr every
backend subprocess is started with: a pipe whose reader thread copies each
chunk to the proxy's own stderr unchanged, so the service log reads as before,
and keeps the most recent lines for ``ResilientBackend`` to report in
``backend_status``.

The subprocess needs a real file descriptor for its stderr, which is why this
is an OS pipe and not an in-memory stream.

What is kept is bounded and scrubbed. A line that never ends is held only as
far as it can matter, since a kept line is cut to 500 characters anyway; a
progress meter that redraws with a carriage return is kept as its last
redraw; and obvious secrets (credentials in a URL, bearer tokens, values
assigned to a token, password or key) are masked, because ``backend_status``
is answered to every client of the proxy. The copy on the proxy's own stderr
is never altered.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from collections import deque
from typing import TextIO

_READ_CHUNK = 65536
_MAX_LINE_CHARS = 500
# Four bytes is the most a UTF-8 character takes, so this many bytes of a line
# always hold the characters that are kept.
_MAX_LINE_BYTES = 4 * _MAX_LINE_CHARS
_MASK = "***"

# Conservative on purpose: each pattern names something that is a secret in
# nearly every context, and a false positive only hides a word.
_URL_CREDENTIALS = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE)
_ASSIGNED_SECRET = re.compile(
    r"\b([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|credential|authorization)"
    r"[\w.-]*[\"']?\s*[=:]\s*[\"']?)"
    r"(?:(?:bearer|basic)\s+)?[^\s\"',;&]+",
    re.IGNORECASE,
)
_AUTH_SCHEME = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+")


def redact(line: str) -> str:
    """Mask the obvious secrets in a line: URL credentials, bearer or basic
    credentials, values assigned to a token, secret, password, key or
    authorization name, and JSON web tokens."""
    line = _URL_CREDENTIALS.sub(rf"\1{_MASK}@", line)
    line = _ASSIGNED_SECRET.sub(rf"\1{_MASK}", line)
    line = _AUTH_SCHEME.sub(rf"\1 {_MASK}", line)
    return _JWT.sub(_MASK, line)


class BackendStderr:
    """The stderr pipe shared by every subprocess of one stdio backend.

    Lines are numbered in the order they are read, across subprocesses: a
    caller takes ``mark()`` before a connection attempt and reads that
    attempt's lines with ``lines_between``. Only the last ``max_lines`` are
    kept, each cut to 500 characters.
    """

    def __init__(self, *, max_lines: int = 20) -> None:
        self._lines: deque[tuple[int, str]] = deque(maxlen=max_lines)
        self._count = 0
        self._partial = b""
        self._lock = threading.Lock()
        self._read_fd: int | None = None
        self._sink: TextIO | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> BackendStderr:
        read_fd, write_fd = os.pipe()
        self._read_fd = read_fd
        self._sink = os.fdopen(write_fd, "w")
        self._thread = threading.Thread(
            target=self._pump, name="backend-stderr", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._sink is not None:
            self._sink.close()
            self._sink = None
        if self._thread is not None:
            # EOF arrives once every subprocess holding the write end has
            # exited; a straggler must not hold up shutdown.
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def sink(self) -> TextIO:
        """The write end, to pass as a subprocess's stderr."""
        if self._sink is None:
            raise RuntimeError("BackendStderr is not open")
        return self._sink

    def mark(self) -> int:
        """The number of lines read so far."""
        with self._lock:
            return self._count

    def lines_between(self, start: int, end: int | None = None) -> list[str]:
        """The kept lines numbered from ``start`` up to ``end`` (or the latest)."""
        with self._lock:
            return [
                line
                for number, line in self._lines
                if number >= start and (end is None or number < end)
            ]

    def _pump(self) -> None:
        assert self._read_fd is not None
        try:
            while chunk := os.read(self._read_fd, _READ_CHUNK):
                _forward(chunk)
                self._keep(chunk)
        finally:
            if self._partial:
                self._keep(b"\n")
            os.close(self._read_fd)
            self._read_fd = None

    def _keep(self, chunk: bytes) -> None:
        *complete, rest = (self._partial + chunk).split(b"\n")
        self._partial = _bounded(rest)
        with self._lock:
            for raw in complete:
                line = redact(
                    _last_redraw(raw)[:_MAX_LINE_BYTES]
                    .decode("utf-8", errors="replace")
                    .rstrip()
                )
                if line:
                    self._lines.append((self._count, line[:_MAX_LINE_CHARS]))
                    self._count += 1


def _last_redraw(raw: bytes) -> bytes:
    """What a terminal would show of a line: the text after its last carriage
    return, which also drops the ``\\r`` of a ``\\r\\n`` ending."""
    return raw.rstrip(b"\r").rsplit(b"\r", 1)[-1]


def _bounded(partial: bytes) -> bytes:
    """The unfinished line, reduced to what can still be reported.

    A carriage return restarts the line, so only the text after the last one
    counts; a trailing one is kept in case a newline follows it. Beyond that,
    only the start of the line is ever reported, so the rest is dropped as it
    arrives rather than held.
    """
    if b"\r" in partial:
        trailing = partial.endswith(b"\r")
        partial = _last_redraw(partial) + (b"\r" if trailing else b"")
    return partial[:_MAX_LINE_BYTES]


def _forward(chunk: bytes) -> None:
    """Copy a chunk to the proxy's current stderr, as the subprocess wrote it."""
    stream = sys.stderr
    try:
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(chunk)
            buffer.flush()
        else:
            stream.write(chunk.decode("utf-8", errors="replace"))
            stream.flush()
    except (OSError, ValueError):
        # A closed or broken stderr must not stop the lines being kept.
        pass
