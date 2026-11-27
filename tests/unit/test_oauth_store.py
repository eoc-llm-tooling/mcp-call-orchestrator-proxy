"""Unit tests for the OAuth token directory, its lock and its store."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from key_value.aio.errors import BaseKeyValueError

from mcp_call_orchestrator_proxy import oauth_store
from mcp_call_orchestrator_proxy.oauth_store import (
    TokenDirBusyError,
    TokenDirError,
    TokenDirLock,
    default_token_dir,
    open_token_store,
    prepare_token_dir,
    read_encryption_key,
)


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_default_dir_is_under_xdg_state_home_per_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_token_dir("proxy-a") == (
        tmp_path / "mcp-call-orchestrator-proxy" / "proxy-a" / "oauth"
    )
    assert default_token_dir("a/../b").parent.name == "a_.._b"
    assert default_token_dir("..").parent.name == "_"


@pytest.mark.parametrize("value", [None, "relative/state"])
def test_default_dir_falls_back_to_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str | None
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    if value is None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_STATE_HOME", value)
    assert default_token_dir("p") == (
        tmp_path / ".local" / "state" / "mcp-call-orchestrator-proxy" / "p" / "oauth"
    )


def test_prepare_creates_every_missing_level_private(
    tmp_path: Path, permissive_umask: None
) -> None:
    directory = prepare_token_dir(tmp_path / "state" / "instance" / "oauth")
    for path in (directory, directory.parent, directory.parent.parent):
        assert _mode(path) == 0o700


def test_prepare_refuses_a_directory_others_can_reach(tmp_path: Path) -> None:
    directory = tmp_path / "oauth"
    directory.mkdir()
    directory.chmod(0o750)
    with pytest.raises(TokenDirError, match=r"mode 0750.*chmod 700"):
        prepare_token_dir(directory)


def test_prepare_refuses_a_directory_of_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "oauth"
    directory.mkdir(mode=0o700)
    owner = directory.stat().st_uid
    monkeypatch.setattr(oauth_store.os, "geteuid", lambda: owner + 1)
    with pytest.raises(TokenDirError, match=f"belongs to uid {owner}"):
        prepare_token_dir(directory)


def test_prepare_refuses_a_file(tmp_path: Path) -> None:
    path = tmp_path / "oauth"
    path.write_text("")
    with pytest.raises(TokenDirError, match="not a directory"):
        prepare_token_dir(path)


def test_lock_refuses_a_second_holder_and_names_the_first(tmp_path: Path) -> None:
    with (
        TokenDirLock(tmp_path, role="run"),
        pytest.raises(TokenDirBusyError) as refused,
        TokenDirLock(tmp_path, role="login"),
    ):
        pass
    message = str(refused.value)
    assert f"`run` (pid {os.getpid()})" in message
    assert "stop the proxy service first" in message
    assert _mode(tmp_path / "lock") == 0o600
    # Released on exit.
    with TokenDirLock(tmp_path, role="login"):
        pass


def test_run_refused_by_login_is_told_to_start_again_later(tmp_path: Path) -> None:
    with (
        TokenDirLock(tmp_path, role="login"),
        pytest.raises(TokenDirBusyError, match="start the proxy again"),
        TokenDirLock(tmp_path, role="run"),
    ):
        pass


async def test_store_files_and_collections_are_private(
    tmp_path: Path, permissive_umask: None
) -> None:
    directory = prepare_token_dir(tmp_path / "oauth")
    store = open_token_store(directory)
    await store.put("https://mcp.example.com/mcp/tokens", {"a": 1}, collection="c-1")
    paths = list(directory.rglob("*"))
    assert any(path.is_dir() for path in paths)
    for path in paths:
        assert _mode(path) == (0o700 if path.is_dir() else 0o600), path


async def test_a_failed_write_leaves_the_previous_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = prepare_token_dir(tmp_path / "oauth")
    store = open_token_store(directory)
    await store.put("k", {"refresh_token": "old"}, collection="c")

    def _crash(fd: int) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "fsync", _crash)
    with pytest.raises(OSError, match="disk gone"):
        await store.put("k", {"refresh_token": "new"}, collection="c")
    monkeypatch.undo()

    assert await open_token_store(directory).get("k", collection="c") == {
        "refresh_token": "old"
    }
    assert not list(directory.rglob("*.tmp"))


async def test_an_encrypted_store_keeps_no_plaintext_on_disk(tmp_path: Path) -> None:
    directory = prepare_token_dir(tmp_path / "oauth")
    key = Fernet.generate_key()
    await open_token_store(directory, encryption_key=key).put(
        "k", {"refresh_token": "plain-refresh-value"}, collection="c"
    )

    on_disk = "".join(
        path.read_text() for path in directory.rglob("*.json") if path.is_file()
    )
    assert "plain-refresh-value" not in on_disk
    reread = open_token_store(directory, encryption_key=key)
    assert await reread.get("k", collection="c") == {
        "refresh_token": "plain-refresh-value"
    }
    with pytest.raises(BaseKeyValueError):
        await open_token_store(directory, encryption_key=Fernet.generate_key()).get(
            "k", collection="c"
        )


def test_a_key_file_must_hold_a_fernet_key(tmp_path: Path) -> None:
    good = tmp_path / "good"
    key = Fernet.generate_key()
    good.write_bytes(key + b"\n")
    assert read_encryption_key(good) == key

    bad = tmp_path / "bad"
    bad.write_text("not-a-key-but-a-secret")
    with pytest.raises(TokenDirError) as refused:
        read_encryption_key(bad)
    assert "not-a-key-but-a-secret" not in str(refused.value)
    with pytest.raises(TokenDirError, match="cannot be read"):
        read_encryption_key(tmp_path / "missing")
