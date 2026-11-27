"""Where an OAuth backend's tokens live on disk, and who may write them.

One directory per proxy instance holds the tokens and client registration of
its backend, keyed inside by the backend URL. The directory is the boundary:
it is created ``0700``, and refused when group or others can reach it or it
belongs to another user. FileTreeStore writes each entry as JSON through a
temp file, ``fsync`` and ``rename`` (``mkstemp`` makes it ``0600``), but makes
its collection directories with the umask, so they are tightened here.

Many authorization servers rotate the refresh token on every refresh, so two
processes refreshing from one copy invalidate each other. ``TokenDirLock`` is
the one-writer rule: ``run`` holds it for the life of the process, and
``login`` / ``logout`` hold it while they work.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
from pathlib import Path
from types import TracebackType
from typing import override

from cryptography.fernet import Fernet
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

APP_DIR_NAME = "mcp-call-orchestrator-proxy"
_LOCK_FILE = "lock"
_PRIVATE_DIR = 0o700
_PRIVATE_FILE = 0o600


class TokenDirError(ValueError):
    """The token directory cannot be used as configured."""


class TokenDirBusyError(RuntimeError):
    """Another process holds the token directory's lock."""

    def __init__(self, directory: Path, role: str, holder: str) -> None:
        self.directory = directory
        self.holder = holder
        if role == "run":
            advice = "start the proxy again once it has finished"
        else:
            advice = (
                "stop the proxy service first (for example `systemctl --user stop "
                "<unit>`), then run this command again"
            )
        super().__init__(
            f"OAuth token directory {directory} is in use by {holder}: {advice}."
        )


def default_token_dir(instance: str) -> Path:
    """``$XDG_STATE_HOME/mcp-call-orchestrator-proxy/<instance>/oauth``.

    Falls back to ``~/.local/state`` when ``XDG_STATE_HOME`` is unset or not
    absolute, as the XDG base directory specification requires.
    """
    state = os.environ.get("XDG_STATE_HOME", "")
    base = Path(state) if os.path.isabs(state) else Path.home() / ".local" / "state"
    return base / APP_DIR_NAME / _safe_dir_name(instance) / "oauth"


def _safe_dir_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return "_" if safe in {"", ".", ".."} else safe


def prepare_token_dir(directory: Path) -> Path:
    """Create the directory, and each missing parent, ``0700``; then check it.

    Returns the absolute path. Raises ``TokenDirError`` when it exists but is
    not a directory, belongs to another user, or is reachable by group or
    others.
    """
    directory = Path(os.path.abspath(directory.expanduser()))
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        try:
            os.mkdir(path, _PRIVATE_DIR)
        except FileExistsError:
            continue
        # mkdir's mode is filtered by the umask; chmod is not.
        os.chmod(path, _PRIVATE_DIR)
    check_token_dir(directory)
    return directory


def check_token_dir(directory: Path) -> None:
    """Refuse a token directory another user could read or replace."""
    try:
        info = os.stat(directory)
    except FileNotFoundError:
        raise TokenDirError(
            f"OAuth token directory {directory} does not exist"
        ) from None
    if not stat.S_ISDIR(info.st_mode):
        raise TokenDirError(f"OAuth token directory {directory} is not a directory")
    if info.st_uid != os.geteuid():
        raise TokenDirError(
            f"OAuth token directory {directory} belongs to uid {info.st_uid}, not "
            f"to this user (uid {os.geteuid()})"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise TokenDirError(
            f"OAuth token directory {directory} is accessible by group or others "
            f"(mode {mode:04o}); run `chmod 700 {directory}`"
        )


class TokenDirLock:
    """An exclusive, non-blocking ``flock`` on the token directory's lock file.

    The file records the holder's role and pid, so a refused process can say
    who holds it. The kernel drops the lock when the holder exits, however it
    exits, so a crash leaves no stale lock.
    """

    def __init__(self, directory: Path, role: str) -> None:
        self._path = directory / _LOCK_FILE
        self._directory = directory
        self._role = role
        self._fd: int | None = None

    def __enter__(self) -> TokenDirLock:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, _PRIVATE_FILE)
        try:
            os.fchmod(fd, _PRIVATE_FILE)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = os.pread(fd, 200, 0).decode(errors="replace").split()
                described = (
                    f"`{holder[0]}` (pid {holder[1]})"
                    if len(holder) == 2
                    else "another process"
                )
                raise TokenDirBusyError(
                    self._directory, self._role, described
                ) from None
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{self._role} {os.getpid()}\n".encode(), 0)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._fd is None:
            return
        os.ftruncate(self._fd, 0)
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


class _PrivateFileTreeStore(FileTreeStore):
    """A FileTreeStore whose collection directories are ``0700`` too."""

    @override
    async def _setup_collection(self, *, collection: str) -> None:
        await super()._setup_collection(collection=collection)
        os.chmod(self._collection_infos[collection].directory, _PRIVATE_DIR)


def read_encryption_key(path: Path) -> bytes:
    """Read a Fernet key (urlsafe base64 of 32 bytes) from ``path``.

    Raises ``TokenDirError`` naming the file, never its content.
    """
    try:
        key = path.expanduser().read_bytes().strip()
    except OSError as exc:
        raise TokenDirError(
            f"OAuth encryption key file {path} cannot be read: {exc.strerror}"
        ) from None
    try:
        Fernet(key)
    except ValueError:
        raise TokenDirError(
            f"OAuth encryption key file {path} does not hold a Fernet key (32 "
            "url-safe base64-encoded bytes)"
        ) from None
    return key


def open_token_store(
    directory: Path, *, encryption_key: bytes | None = None
) -> AsyncKeyValue:
    """The JSON file tree in ``directory``, encrypted when a key is given.

    ``directory`` must already have passed ``prepare_token_dir``. Not the
    ``disk`` store: it serializes with pickle, so a tampered file there would
    run code.
    """
    store: AsyncKeyValue = _PrivateFileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
            directory
        ),
    )
    if encryption_key is not None:
        store = FernetEncryptionWrapper(store, fernet=Fernet(encryption_key))
    return store
