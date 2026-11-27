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
"""

from __future__ import annotations

import os
import sys
import threading
from collections import deque
from typing import TextIO

_READ_CHUNK = 65536
_MAX_LINE_CHARS = 500


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
        *complete, self._partial = (self._partial + chunk).split(b"\n")
        with self._lock:
            for raw in complete:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._lines.append((self._count, line[:_MAX_LINE_CHARS]))
                    self._count += 1


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
