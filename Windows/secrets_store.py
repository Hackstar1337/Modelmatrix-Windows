"""Per-user protected secret storage for the Windows controller.

Windows values are protected with DPAPI (CRYPTPROTECT_UI_FORBIDDEN) and are
kept in a private directory.  The non-Windows backend exists only for explicit
unit tests: it writes a user-only file when ``test_mode=True`` or
``MODELMATRIX_TEST_SECRET_STORE=1``.  There is deliberately no silent
plaintext fallback in production.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import secrets
import stat
import getpass
import subprocess
import tempfile
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from threading import RLock
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SecretStoreError(RuntimeError):
    """A protected secret operation could not be completed."""


class SecretStore:
    def __init__(self, root: str | os.PathLike[str], *, test_mode: bool = False) -> None:
        self.root = Path(root).expanduser()
        self.directory = self.root / "secrets"
        self.path = self.directory / "store.bin"
        self.lock_path = self.directory / "store.bin.lock"
        self._lock = RLock()
        self._test_mode = bool(test_mode or os.environ.get("MODELMATRIX_TEST_SECRET_STORE") == "1")
        self._windows = os.name == "nt"
        if not self._windows and not self._test_mode:
            raise SecretStoreError("protected secret storage is unavailable on this platform")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if not self._windows:
                os.chmod(self.directory, stat.S_IRWXU)
            else:
                _harden_acl(self.directory, directory=True)
        except OSError as error:
            raise SecretStoreError(f"cannot initialize secret storage: {error}") from error

    @staticmethod
    def _validate(name: str) -> str:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SecretStoreError("invalid secret name")
        return name

    def _load_raw(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = self.path.read_bytes()
            if self._windows:
                data = _unprotect(data)
                value = json.loads(data.decode("utf-8"))
            else:
                value = json.loads(data.decode("utf-8"))
            if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                raise ValueError("invalid store shape")
            return value
        except (OSError, ValueError, UnicodeError, SecretStoreError) as error:
            raise SecretStoreError("protected secret store is unreadable") from error

    def _save_raw(self, values: dict[str, str]) -> None:
        encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if self._windows:
            encoded = _protect(encoded)
        fd = -1
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if not self._windows:
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, self.path)
            if self._windows:
                _harden_acl(self.path, directory=False)
        except OSError as error:
            raise SecretStoreError("cannot commit protected secret store") from error
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @contextmanager
    def _process_lock(self):
        """Serialize store read/modify/write operations across instances/processes."""
        handle = None
        try:
            handle = self.lock_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if self._windows:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if handle is not None:
                try:
                    if self._windows:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (OSError, ValueError):
                    pass
                handle.close()

    def get(self, name: str) -> str | None:
        name = self._validate(name)
        with self._lock, self._process_lock():
            return self._load_raw().get(name)

    def set(self, name: str, value: str) -> None:
        name = self._validate(name)
        if not isinstance(value, str) or not value:
            raise SecretStoreError("secret value must be a non-empty string")
        with self._lock, self._process_lock():
            values = self._load_raw()
            values[name] = value
            self._save_raw(values)

    def delete(self, name: str) -> None:
        name = self._validate(name)
        with self._lock, self._process_lock():
            values = self._load_raw()
            if name in values:
                del values[name]
                if values:
                    self._save_raw(values)
                else:
                    try:
                        self.path.unlink(missing_ok=True)
                    except OSError as error:
                        raise SecretStoreError("cannot remove protected secret store") from error

    def has(self, name: str) -> bool:
        return self.get(name) is not None


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    return _crypt(data, protect=True)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    return _crypt(data, protect=False)


def _crypt(data: bytes, *, protect: bool) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = Blob(len(data), source)
    outgoing = Blob()
    crypt = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    crypt.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), wintypes.LPVOID, ctypes.POINTER(Blob), wintypes.DWORD, ctypes.POINTER(Blob)] if protect else [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.restype = wintypes.BOOL
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt(ctypes.byref(incoming), "ModelMatrix", None, None, None, flags, ctypes.byref(outgoing))
    else:
        ok = crypt(ctypes.byref(incoming), None, None, None, None, flags, ctypes.byref(outgoing))
    if not ok:
        raise SecretStoreError("DPAPI operation failed")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [wintypes.HLOCAL]
        local_free.restype = wintypes.HLOCAL
        local_free(outgoing.pbData)


def _harden_acl(path: Path, *, directory: bool) -> None:
    """Remove inherited ACLs and grant the current user full access only."""
    if os.name != "nt":
        return
    user = getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    principal = f"{domain}\\{user}" if domain else user
    permission = "(OI)(CI)F" if directory else "F"
    try:
        completed = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:{permission}"],
            check=False,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as error:
        raise SecretStoreError("Windows ACL hardening is unavailable") from error
    if completed.returncode != 0:
        raise SecretStoreError("Windows ACL hardening failed")
