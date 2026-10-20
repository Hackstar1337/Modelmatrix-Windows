"""Platform adapters used by the stdlib RemoteAgent.

The public helpers in this module intentionally have useful POSIX fallbacks so
unit tests and the existing Linux/macOS product keep the same behavior.  On
Windows, process lifetime is backed by Job Objects (when available), listener
inventory uses ``netstat -ano`` without a shell, and host facts use Win32 APIs
with conservative command fallbacks.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import json
import ntpath
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

IS_WINDOWS = os.name == "nt" or platform.system().lower() == "windows"

# Win32 uses pointer-sized handles.  Keeping these declarations in one place
# prevents ctypes from truncating a 64-bit HANDLE to a C int (the old code did
# exactly that for OpenProcess/CreateJobObject and GetTickCount64).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_CREATE_SUSPENDED = 0x00000004
_INFINITE = 0xFFFFFFFF


def _configure_win32_api() -> None:
    if not IS_WINDOWS:
        return
    try:
        k = ctypes.windll.kernel32
        for name, argtypes, restype in (
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            ("QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            ("GetProcessTimes", [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
            ("GetExitCodeProcess", [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            ("CreateJobObjectW", [wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("SetInformationJobObject", [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
            ("GetTickCount64", [], ctypes.c_ulonglong),
        ):
            api = getattr(k, name, None)
            if api is not None:
                api.argtypes = argtypes
                api.restype = restype
        psapi = getattr(ctypes.windll, "psapi", None)
        if psapi is not None and getattr(psapi, "GetProcessMemoryInfo", None) is not None:
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    except (AttributeError, OSError):
        # Importing this module must remain possible under Wine/minimal test
        # doubles where one of the optional DLL entry points is absent.
        return


_configure_win32_api()


def local_appdata() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Local"


def default_agent_root() -> Path:
    if IS_WINDOWS:
        return local_appdata() / "ModelMatrix" / "Agent"
    return Path.home() / ".local" / "share" / "modelmatrix-agent"


def default_profiles_dir() -> Path:
    if IS_WINDOWS:
        return local_appdata() / "ModelMatrix" / "Profiles"
    return Path.home() / "model-profiles"


def default_log_dir(root: Path) -> Path:
    if IS_WINDOWS:
        return local_appdata() / "ModelMatrix" / "logs"
    return root / "logs"


def safe_log_path(root: Path, name: str) -> Path:
    safe = safe_windows_component(name)
    directory = default_log_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe}.log"


_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)


def safe_windows_component(value: str) -> str:
    """Return a filename component safe on Windows and harmless elsewhere.

    Windows rejects device names even when an extension is appended and also
    strips trailing spaces/dots.  Profile names come from user filenames, so a
    single sanitizer must protect log, pid, and other derived paths.
    """
    safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in str(value))
    if IS_WINDOWS:
        safe = safe.rstrip(" .") or "profile"
        stem = safe.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_BASENAMES:
            safe = f"_{safe}"
    return safe


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created_ns: int | None = None
    executable: str | None = None
    fingerprint: str | None = None

    def matches(self, other: "ProcessIdentity | None") -> bool:
        if other is None or other.pid != self.pid:
            return False
        if self.created_ns is not None:
            return other.created_ns is not None and self.created_ns == other.created_ns
        if self.fingerprint and other.fingerprint:
            return self.fingerprint == other.fingerprint
        return bool(self.executable and other.executable and self.executable.casefold() == other.executable.casefold())


def _fingerprint(command: str | None, executable: str | None) -> str | None:
    if not command and not executable:
        return None
    return hashlib.sha256((command or executable or "").encode("utf-8", "replace")).hexdigest()[:32]


def _windows_process_details(pid: int) -> tuple[str | None, int | None]:
    if not IS_WINDOWS:
        return None, None
    try:
        handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None, None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            executable = None
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                executable = buf.value
            created = None
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]
            a, b, c, d = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if ctypes.windll.kernel32.GetProcessTimes(handle, ctypes.byref(a), ctypes.byref(b), ctypes.byref(c), ctypes.byref(d)):
                created = ((a.dwHighDateTime << 32) | a.dwLowDateTime) * 100
            return executable, created
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError):
        return None, None


def _windows_process_details_many(pids: Iterable[int]) -> dict[int, tuple[str | None, int | None]]:
    """Collect native image/time details once per distinct PID.

    CIM is deliberately not used here: it is commonly denied by hardened
    Windows hosts and a PowerShell process per listener made status scale as
    O(listeners).  Image paths and creation FILETIMEs are sufficient for
    ownership and are available through limited process handles.
    """
    return {pid: _windows_process_details(pid) for pid in set(pids) if pid > 0}


def process_identity(pid: int, command: str | None = None) -> ProcessIdentity | None:
    if not pid or pid <= 0:
        return None
    executable = created = None
    if IS_WINDOWS:
        executable, created = _windows_process_details(pid)
    else:
        try:
            executable = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            pass
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[-1].split()
            # Linux starttime is ticks since boot; use it as a stable identity.
            created = int(fields[19]) if len(fields) > 19 else None
        except (OSError, ValueError, IndexError):
            pass
    if executable is None and command is None:
        command = process_command(pid)
    if executable is None and command is None and not _pid_exists(pid):
        return None
    return ProcessIdentity(pid, created, executable, _fingerprint(command, executable))


def _pid_exists(pid: int) -> bool:
    if IS_WINDOWS:
        # os.kill(pid, 0) is NOT a harmless signal probe on Windows: Python
        # maps non-console signals to TerminateProcess. Never call it here.
        try:
            handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def process_command(pid: int | None) -> str | None:
    if not pid:
        return None
    if IS_WINDOWS:
        # Do not invoke CIM/PowerShell here.  Hardened Windows hosts often
        # deny CIM and a per-listener fallback made the dashboard serially
        # wait for every timeout.  Native image identity remains available.
        executable, _ = _windows_process_details(pid)
        return executable
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if raw:
            return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
    except OSError:
        pass
    try:
        result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=3, check=False)
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def process_rss_mb(pid: int | None) -> float | None:
    if not pid:
        return None
    if IS_WINDOWS:
        try:
            handle = ctypes.windll.kernel32.OpenProcess(
                _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid
            )
            if not handle:
                return None
            try:
                class PMC(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD), ("peak_working_set", ctypes.c_size_t), ("working_set", ctypes.c_size_t), ("quota_peak_paged", ctypes.c_size_t), ("quota_paged", ctypes.c_size_t), ("quota_peak_nonpaged", ctypes.c_size_t), ("quota_nonpaged", ctypes.c_size_t), ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t), ("private_usage", ctypes.c_size_t)]
                value = PMC(); value.cb = ctypes.sizeof(value)
                if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(value), value.cb):
                    return round(value.working_set / (1024 * 1024) * 10) / 10
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError):
            return None
        return None
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                return round(float(line.split()[1]) / 1024 * 10) / 10
    except (OSError, ValueError, IndexError):
        pass
    try:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=3, check=False)
        return round(float(result.stdout.strip()) / 1024 * 10) / 10
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def process_is_alive(pid: int | None, identity: ProcessIdentity | None = None) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                alive = code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            alive = _pid_exists(pid)
    else:
        alive = _pid_exists(pid)
    if not alive:
        return False
    return identity.matches(process_identity(pid)) if identity is not None else True


class _JobHandle:
    def __init__(self, handle: int):
        self.handle = handle

    def close(self) -> None:
        if self.handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except (AttributeError, OSError):
                pass
            self.handle = 0


class WindowsProcessAdapter:
    """Own a launched process and all descendants through a Windows Job Object."""
    def __init__(self):
        self._jobs: dict[int, _JobHandle] = {}
        self._identities: dict[int, ProcessIdentity] = {}
        self._processes: dict[int, subprocess.Popen] = {}
        self._lock = threading.RLock()

    def launch(self, argv: list[str], *, cwd: Path, env: dict[str, str], log_handle, shell: str | None = None) -> subprocess.Popen:
        if not argv or not argv[0] or any(not isinstance(x, str) for x in argv):
            raise ValueError("argv must contain a non-empty executable and strings")
        kwargs: dict[str, Any] = dict(stdout=log_handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(cwd), env=env)
        if IS_WINDOWS:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # Attach the suspended child to its Job Object before execution;
            # this closes the descendant escape race during startup.
            flags |= _CREATE_SUSPENDED
            kwargs["creationflags"] = flags
        if shell:
            if shell not in {"powershell", "cmd"}:
                raise ValueError("shell must be powershell or cmd")
            if shell == "powershell":
                # argv[0] is already the explicit command string.  Applying
                # list2cmdline to it changes PowerShell quoting and breaks
                # paths/Unicode; pass it unchanged as the -Command operand.
                command = argv[0] if len(argv) == 1 else " ".join(argv)
                argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]
                kwargs["shell"] = False
            else:
                # cmd.exe parses one command string.  list2cmdline applies the
                # CreateProcess/MSVCRT quoting rules, avoiding path truncation
                # for approved .cmd/.bat launches with spaces or quotes.
                command = argv[0] if len(argv) == 1 else subprocess.list2cmdline(argv)
                argv = ["cmd.exe", "/d", "/s", "/c", command]
                kwargs["shell"] = False
        process = subprocess.Popen(argv, **kwargs)
        identity = process_identity(process.pid, " ".join(argv))
        with self._lock:
            if identity:
                self._identities[process.pid] = identity
            if IS_WINDOWS:
                try:
                    if not self._assign_job(process.pid):
                        raise OSError("Windows denied safe process-tree ownership (Job Object assignment)")
                    self._resume_process(process)
                except BaseException:
                    process.terminate()
                    process.wait(timeout=5)
                    job = self._jobs.pop(process.pid, None)
                    if job:
                        job.close()
                    self._identities.pop(process.pid, None)
                    raise
            self._processes[process.pid] = process
        return process

    def _resume_process(self, process: subprocess.Popen) -> None:
        thread_handle = getattr(process, "_thread", None) or getattr(process, "_thread_handle", None)
        if thread_handle:
            if ctypes.windll.kernel32.ResumeThread(thread_handle) == 0xFFFFFFFF:
                raise OSError("ResumeThread failed")
            return
        # CPython closes the primary thread handle internally.  NtResumeProcess
        # is the safe fallback: it is called only after Job assignment and
        # prevents a suspended child from leaking on supported Windows builds.
        ntdll = ctypes.windll.ntdll
        resume = ntdll.NtResumeProcess
        resume.argtypes = [wintypes.HANDLE]
        resume.restype = wintypes.LONG
        status = resume(getattr(process, "_handle", process._handle))
        if status != 0:
            raise OSError(f"NtResumeProcess failed: 0x{status & 0xffffffff:08x}")

    def _assign_job(self, pid: int) -> bool:
        try:
            k = ctypes.windll.kernel32
            job = k.CreateJobObjectW(None, None)
            if not job:
                return False
            class BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("per_process_user_time_limit", ctypes.c_longlong),
                    ("per_job_user_time_limit", ctypes.c_longlong),
                    ("limit_flags", wintypes.DWORD),
                    ("minimum_working_set", ctypes.c_size_t),
                    ("maximum_working_set", ctypes.c_size_t),
                    ("active_process_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t),
                    ("priority_class", wintypes.DWORD),
                    ("scheduling_class", wintypes.DWORD),
                ]
            class IoCounters(ctypes.Structure):
                _fields_ = [(f"value_{index}", ctypes.c_ulonglong) for index in range(6)]
            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory_limit", ctypes.c_size_t), ("job_memory_limit", ctypes.c_size_t), ("peak_process_memory_used", ctypes.c_size_t), ("peak_job_memory_used", ctypes.c_size_t)]
            limits = ExtendedLimits()
            limits.basic.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                k.CloseHandle(job)
                return False
            process_handle = k.OpenProcess(0x1F0FFF, False, pid)
            if not process_handle:
                k.CloseHandle(job)
                return False
            try:
                if not k.AssignProcessToJobObject(job, process_handle):
                    k.CloseHandle(job)
                    return False
            finally:
                k.CloseHandle(process_handle)
            self._jobs[pid] = _JobHandle(job)
            return True
        except (AttributeError, OSError):
            return False

    def identity(self, pid: int) -> ProcessIdentity | None:
        with self._lock:
            return self._identities.get(pid) or process_identity(pid)

    def stop(self, pid: int, *, force: bool = False, timeout: float = 20.0,
             expected: ProcessIdentity | None = None) -> bool:
        if not pid:
            return True
        with self._lock:
            job = self._jobs.get(pid)
            cached = self._identities.get(pid)
        expected = expected or cached
        # Never taskkill a reused PID.  Re-read creation time immediately
        # before signaling; the cached launch identity is only an expected
        # value, never proof that the current PID is still ours.
        if IS_WINDOWS and expected is not None:
            current = process_identity(pid)
            if current is None or not expected.matches(current):
                return False
        terminated = False
        if IS_WINDOWS and job and job.handle:
            try:
                if force:
                    terminated = bool(ctypes.windll.kernel32.TerminateJobObject(job.handle, 1))
                else:
                    # CTRL_BREAK is cooperative when the child owns a console;
                    # TerminateJobObject below is the reliable bounded fallback.
                    terminated = bool(ctypes.windll.kernel32.TerminateJobObject(job.handle, 0))
            except (AttributeError, OSError):
                terminated = False
        if IS_WINDOWS:
            if not terminated:
                # An untracked PID has no safe ownership proof; callers must first
                # capture an identity and pass it as expected.
                if expected is None:
                    return False
                try:
                    subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=max(1.0, timeout), check=False)
                except (OSError, subprocess.TimeoutExpired):
                    return False
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try: os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
                except OSError: pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process_is_alive(pid):
            time.sleep(0.05)
        with self._lock:
            handle = self._jobs.pop(pid, None)
            self._identities.pop(pid, None)
            process = self._processes.pop(pid, None)
        if handle:
            handle.close()
        if process is not None:
            try:
                process.wait(timeout=max(0.1, timeout))
            except subprocess.TimeoutExpired:
                return False
        return not process_is_alive(pid)


def terminate_process_tree(pid: int, *, force: bool = False, timeout: float = 20.0,
                           identity: ProcessIdentity | None = None) -> None:
    if IS_WINDOWS:
        _WINDOWS_PROCESSES.stop(pid, force=force, timeout=timeout, expected=identity)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try: os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except OSError: pass


_WINDOWS_PROCESSES = WindowsProcessAdapter()


def windows_process_supervisor() -> WindowsProcessAdapter:
    """Shared launch/stop supervisor used by the agent and platform helpers."""
    return _WINDOWS_PROCESSES


class WindowsListenerProvider:
    """Return listener rows matching the agent's stable inventory shape."""
    def list_listening_tcp(self) -> list[dict[str, Any]]:
        if not IS_WINDOWS:
            return []
        rows: dict[int, dict[str, Any]] = {}
        try:
            result = subprocess.run(["netstat.exe", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return []
        parsed: list[tuple[int, str, int, str]] = []
        for raw in result.stdout.splitlines():
            parts = raw.split()
            if len(parts) < 5 or parts[0].upper() not in {"TCP", "TCP6"} or parts[3].upper() != "LISTENING":
                continue
            try:
                local, port = parts[1].rsplit(":", 1); port = int(port); pid = int(parts[4])
            except (ValueError, IndexError):
                continue
            bind = local.strip("[]")
            parsed.append((port, bind, pid, raw))
        details = _windows_process_details_many([item[2] for item in parsed])
        for port, bind, pid, _raw in parsed:
            executable, _created = details.get(pid, (None, None))
            rows.setdefault(port, {"port": port, "pid": pid, "command": executable, "bind": bind})
        return [rows[p] for p in sorted(rows)]


class WindowsMetricsProvider:
    """Host metrics with Win32 APIs and stable agent payload units."""
    def memory(self) -> dict[str, Any]:
        if not IS_WINDOWS: return {"total_mb": None, "used_mb": None, "available_mb": None, "percent": None, "source": "unavailable"}
        try:
            class MS(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("memory_load", wintypes.DWORD), ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong), ("avail_extended", ctypes.c_ulonglong)]
            value = MS(); value.length = ctypes.sizeof(value)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
                total = value.total_phys / 1048576; available = value.avail_phys / 1048576
                return {"total_mb": round(total), "used_mb": round(total - available, 1), "available_mb": round(available, 1), "percent": round(value.memory_load, 1), "source": "GlobalMemoryStatusEx"}
        except (AttributeError, OSError): pass
        return {"total_mb": None, "used_mb": None, "available_mb": None, "percent": None, "source": "unavailable"}

    def uptime(self) -> float | None:
        if not IS_WINDOWS: return None
        try: return ctypes.windll.kernel32.GetTickCount64() / 1000.0
        except (AttributeError, OSError): return None

    def storage(self, path: str) -> dict[str, Any] | None:
        if not IS_WINDOWS: return None
        try:
            total = ctypes.c_ulonglong(); free = ctypes.c_ulonglong()
            if ctypes.windll.kernel32.GetDiskFreeSpaceExW(str(path), None, ctypes.byref(total), ctypes.byref(free)):
                t = float(total.value); f = float(free.value); used = max(0.0, t - f)
                return {"used_mb": round(used / 1048576, 1), "total_mb": round(t / 1048576), "percent": round(used / t * 100, 1) if t else None, "source": "GetDiskFreeSpaceEx"}
        except (AttributeError, OSError): pass
        return None

    def cpu_percent(self) -> float | None:
        # GetSystemTimes is sampled over a short interval, no WMI dependency.
        if not IS_WINDOWS: return None
        try:
            class FT(ctypes.Structure): _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
            def read():
                idle, kernel, user = FT(), FT(), FT()
                if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                    val = lambda x: (x.high << 32) | x.low
                    return val(idle), val(kernel), val(user)
                return None
            before = read(); time.sleep(0.1); after = read()
            if not before or not after: return None
            idle = after[0] - before[0]; total = (after[1] + after[2]) - (before[1] + before[2])
            return round(max(0.0, min(100.0, (1 - idle / total) * 100)) * 10) / 10 if total else None
        except (AttributeError, OSError): return None


def parse_command_json(raw: str | None, *, field: str) -> list[str] | None:
    if not raw: return None
    try: value = json.loads(raw)
    except json.JSONDecodeError as error: raise ValueError(f"{field} must be valid JSON") from error
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a JSON string array")
    if field == "START_COMMAND_JSON" and (not value or any(not item for item in value)):
        raise ValueError(f"{field} must include a non-empty executable")
    if field != "SERVER_ARGS_JSON" and any(not item for item in value):
        raise ValueError(f"{field} cannot contain empty arguments")
    return value


def _is_bare_executable(value: str) -> bool:
    return not ntpath.dirname(value) and not ntpath.splitdrive(value)[0]


def _is_windows_batch(value: str) -> bool:
    return ntpath.splitext(value)[1].lower() in {".cmd", ".bat"}


def _structured_command_result(args: list[str]) -> tuple[list[str], str | None]:
    if IS_WINDOWS and args and _is_windows_batch(args[0]):
        return args, "cmd"
    return args, None


def command_argv(profile_values: dict[str, str], fallback: str) -> tuple[list[str], str | None]:
    """Build argv without parsing user data through POSIX shell syntax."""
    def expand(args: list[str]) -> list[str]:
        result = []
        for argument in args:
            for key, default in (("PORT", None), ("HOST", "127.0.0.1")):
                value = profile_values.get(key, default)
                for token in ("${" + key + "}", "{" + key + "}", "{" + key.lower() + "}"):
                    if token in argument:
                        if value is None:
                            raise ValueError(f"{key} is required by structured command placeholder")
                        argument = argument.replace(token, str(value))
            result.append(argument)
        return result
    raw_start = profile_values.get("START_COMMAND_JSON")
    raw_args = profile_values.get("SERVER_ARGS_JSON")
    executable = profile_values.get("SERVER_BIN")
    if raw_start is not None:
        args = parse_command_json(raw_start, field="START_COMMAND_JSON") or []
        return _structured_command_result(expand(args))
    if raw_args is not None:
        args = parse_command_json(raw_args, field="SERVER_ARGS_JSON") or []
        if not executable:
            raise ValueError("SERVER_ARGS_JSON requires SERVER_BIN")
        return _structured_command_result(expand([executable, *args]))
    if executable:
        extra = profile_values.get("EXTRA_ARGS_JSON")
        extra_args = parse_command_json(extra, field="EXTRA_ARGS_JSON") if extra is not None else []
        # SERVER_BIN overrides only the executable.  Keep flags generated by
        # the runtime adapter instead of silently dropping model/port args.
        try:
            # Runtime adapters produce shell-quoted text with shlex.quote on
            # every OS. Decode that known representation, then launch argv.
            generated = shlex.split(fallback, posix=True)
        except ValueError:
            generated = fallback.split()
        return _structured_command_result([executable, *(generated[1:] if generated else []), *(extra_args or [])])
    shell = (profile_values.get("SHELL") or "").strip().lower() or None
    if shell not in {None, "powershell", "cmd"}:
        raise ValueError("SHELL must be powershell or cmd")
    if shell is None:
        # Explicit command strings remain available on POSIX; Windows requires
        # an explicit shell mode to prevent accidental cmd metacharacter use.
        if IS_WINDOWS: raise ValueError("Windows command strings require SHELL=powershell or SHELL=cmd, or START_COMMAND_JSON")
        return ["/bin/bash", "-lc", fallback], None
    return [fallback], shell


def augment_windows_path(env: dict[str, str], *, venv: str | None = None, dll_dirs: Iterable[str] = ()) -> dict[str, str]:
    result = dict(env)
    paths: list[str] = []
    if venv:
        # This helper is only for Windows launches. Use ntpath rather than
        # pathlib so its contract stays Windows-native in Linux-based tests.
        paths.extend([ntpath.join(str(venv), "Scripts"), ntpath.join(str(venv), "Library", "bin")])
    paths.extend(str(x) for x in dll_dirs if x)
    if paths:
        path_key = next((key for key in result if key.lower() == "path"), "PATH")
        separator = ";"
        result[path_key] = separator.join(paths + [result.get(path_key, "")])
    return result


def config_lock(handle, *, timeout: float = 10.0) -> None:
    if IS_WINDOWS:
        import msvcrt
        handle.seek(0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for config.json.lock")
                time.sleep(0.05)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def config_unlock(handle) -> None:
    if IS_WINDOWS:
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
